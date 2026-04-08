from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.hub_config import (
    consume_sync_events,
    hub_worktree_path,
    is_configured,
    load_hub_config,
    load_pending_conflicts,
    new_conflict_id,
    save_hub_config,
    save_pending_conflicts,
)

logger = logging.getLogger(__name__)


def _connectors_root() -> Path:
    override = os.environ.get("EMERGE_CONNECTOR_ROOT")
    if override:
        return Path(override)
    return Path.home() / ".emerge" / "connectors"


def _file_to_intent_sig(connector: str, rel: Path) -> str:
    """Convert pipelines/read/foo.py → connector.read.foo"""
    parts = rel.parts
    if len(parts) == 2:
        mode = parts[0]
        name = Path(parts[1]).stem
        return f"{connector}.{mode}.{name}"
    return ""


def _load_candidate_timestamps(connector_dir: Path) -> dict[str, int]:
    """Return {intent_sig: last_ts_ms} for stable entries in span-candidates.json."""
    p = connector_dir / "span-candidates.json"
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return {
            k: int(v.get("last_ts_ms", 0))
            for k, v in raw.get("candidates", {}).items()
            if isinstance(v, dict) and v.get("status") == "stable"
        }
    except Exception:
        return {}


def _load_spans_timestamps(worktree_connector_dir: Path) -> dict[str, int]:
    """Return {intent_sig: last_ts_ms} from spans.json in the hub worktree connector dir."""
    p = worktree_connector_dir / "spans.json"
    if not p.exists():
        return {}
    try:
        spans = json.loads(p.read_text(encoding="utf-8")).get("spans", {})
        return {k: int(v.get("last_ts_ms", 0)) for k, v in spans.items() if isinstance(v, dict)}
    except Exception:
        return {}


# ── Export ──────────────────────────────────────────────────────────────────

def export_vertical(
    connector: str,
    *,
    connectors_root: Path | None = None,
    hub_worktree: Path | None = None,
) -> None:
    """Copy connector assets from local connectors dir into the hub worktree.

    Per-pipeline additive export: only overwrites a worktree pipeline when the
    local candidate has a higher (or equal) last_ts_ms than the remote version.
    Remote-only pipelines are left untouched — they remain in the worktree from
    the preceding git_merge_remote call in push_flow.
    """
    src = (connectors_root or _connectors_root()) / connector
    dst = (hub_worktree or hub_worktree_path()) / "connectors" / connector
    dst.mkdir(parents=True, exist_ok=True)

    src_pipelines = src / "pipelines"
    dst_pipelines = dst / "pipelines"

    if src_pipelines.exists():
        local_ts = _load_candidate_timestamps(src)
        remote_ts = _load_spans_timestamps(dst)

        for py_file in src_pipelines.rglob("*.py"):
            rel = py_file.relative_to(src_pipelines)
            intent_sig = _file_to_intent_sig(connector, rel)
            if not intent_sig or intent_sig not in local_ts:
                continue
            l_ts = local_ts[intent_sig]
            r_ts = remote_ts.get(intent_sig, 0)
            if l_ts >= r_ts:
                dst_file = dst_pipelines / rel
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(py_file, dst_file)
                yaml_src = py_file.with_suffix(".yaml")
                dst_yaml = dst_file.with_suffix(".yaml")
                if yaml_src.exists():
                    shutil.copy2(yaml_src, dst_yaml)
                elif dst_yaml.exists():
                    dst_yaml.unlink()

    notes_src = src / "NOTES.md"
    if notes_src.exists():
        shutil.copy2(notes_src, dst / "NOTES.md")

    _export_spans_json(src, dst)


def _export_spans_json(src: Path, dst: Path) -> None:
    """Merge local stable spans into the worktree spans.json. Remote-only spans are preserved."""
    candidates_path = src / "span-candidates.json"
    if not candidates_path.exists():
        return
    try:
        raw = json.loads(candidates_path.read_text(encoding="utf-8"))
        candidates = raw.get("candidates", {})
    except Exception:
        return

    local_spans: dict[str, Any] = {}
    for key, entry in candidates.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("status") != "stable":
            continue
        local_spans[key] = {
            "intent_signature": entry.get("intent_signature", key),
            "status": "stable",
            "last_ts_ms": entry.get("last_ts_ms", 0),
        }

    # Load existing worktree spans so we don't erase other members' entries
    existing_path = dst / "spans.json"
    existing_spans: dict[str, Any] = {}
    if existing_path.exists():
        try:
            existing_spans = json.loads(existing_path.read_text(encoding="utf-8")).get("spans", {})
        except Exception:
            pass

    merged = dict(existing_spans)
    for key, entry in local_spans.items():
        existing = merged.get(key)
        if not isinstance(existing, dict) or entry.get("last_ts_ms", 0) >= existing.get("last_ts_ms", 0):
            merged[key] = entry

    dst.mkdir(parents=True, exist_ok=True)
    _write_json(dst / "spans.json", {"spans": merged})


# ── Import ──────────────────────────────────────────────────────────────────

def import_vertical(
    connector: str,
    *,
    connectors_root: Path | None = None,
    hub_worktree: Path | None = None,
) -> None:
    """Copy connector assets from hub worktree into local connectors dir."""
    src = (hub_worktree or hub_worktree_path()) / "connectors" / connector
    dst = (connectors_root or _connectors_root()) / connector

    if not src.exists():
        return

    dst.mkdir(parents=True, exist_ok=True)

    src_pipelines = src / "pipelines"
    if src_pipelines.exists():
        dst_pipelines = dst / "pipelines"
        if dst_pipelines.exists():
            shutil.rmtree(dst_pipelines)
        shutil.copytree(src_pipelines, dst_pipelines)

    notes_src = src / "NOTES.md"
    if notes_src.exists():
        shutil.copy2(notes_src, dst / "NOTES.md")

    _import_spans_json(src, dst)


def _import_spans_json(src: Path, dst: Path) -> None:
    """Merge remote spans.json into local spans.json. Remote wins on newer last_ts_ms."""
    remote_path = src / "spans.json"
    if not remote_path.exists():
        return
    try:
        remote_spans = json.loads(remote_path.read_text(encoding="utf-8")).get("spans", {})
    except Exception:
        return

    local_path = dst / "spans.json"
    try:
        local_spans = json.loads(local_path.read_text(encoding="utf-8")).get("spans", {}) if local_path.exists() else {}
    except Exception:
        local_spans = {}

    merged = dict(local_spans)
    for key, entry in remote_spans.items():
        if not isinstance(entry, dict):
            continue
        local_entry = merged.get(key)
        if local_entry is None or entry.get("last_ts_ms", 0) > local_entry.get("last_ts_ms", 0):
            merged[key] = entry

    _write_json(local_path, {"spans": merged})


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".hub-import-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2, ensure_ascii=False))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ── Git operations ──────────────────────────────────────────────────────────

def _git(args: list[str], *, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a git command in cwd. Raises CalledProcessError on failure (when check=True)."""
    return subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
    )


def git_has_remote_changes(worktree: Path, branch: str) -> bool:
    """Return True if origin/<branch> is ahead of HEAD after fetching."""
    try:
        _git(["fetch", "origin", branch], cwd=worktree)
    except subprocess.CalledProcessError:
        return False
    result = _git(
        ["rev-list", "--count", f"HEAD..origin/{branch}"],
        cwd=worktree,
        check=False,
    )
    if result.returncode != 0:
        return False
    return int(result.stdout.strip() or "0") > 0


def git_merge_remote(
    worktree: Path,
    branch: str,
    *,
    author: str,
) -> dict[str, Any]:
    """Fetch + merge origin/<branch>. Returns {"ok": True} or {"conflict": True, "files": [...]}."""
    try:
        _git(["fetch", "origin", branch], cwd=worktree)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip()
        if "Permission denied" in stderr or "Authentication failed" in stderr:
            return {"ok": False, "error": f"auth_failed: {stderr}"}
        return {"ok": False, "error": f"fetch failed: {stderr}"}

    merge = _git(
        ["merge", f"origin/{branch}", "--no-edit"],
        cwd=worktree,
        check=False,
    )
    if merge.returncode == 0:
        return {"ok": True}

    # Collect unresolved paths; if empty this is a non-conflict failure.
    status = _git(["diff", "--name-only", "--diff-filter=U"], cwd=worktree, check=False)
    conflict_files = [f.strip() for f in status.stdout.splitlines() if f.strip()]
    _git(["merge", "--abort"], cwd=worktree, check=False)
    if not conflict_files:
        return {"ok": False, "error": f"merge failed: {merge.stderr.strip()}"}
    return {"conflict": True, "files": conflict_files}


def git_push(
    worktree: Path,
    branch: str,
    *,
    connector: str,
    author: str,
) -> dict[str, Any]:
    """Stage all changes, commit, and push. Returns {"ok": True, "pushed": bool}."""
    status = _git(["status", "--porcelain"], cwd=worktree, check=False)
    if not status.stdout.strip():
        return {"ok": True, "pushed": False, "reason": "nothing_to_commit"}

    _git(["add", "-A"], cwd=worktree)
    ts_ms = int(time.time() * 1000)
    _git(
        ["commit", "-m", f"hub: sync {connector} pipelines [ts={ts_ms}]",
         "--author", author],
        cwd=worktree,
    )

    push_result = _git(["push", "origin", branch], cwd=worktree, check=False)
    if push_result.returncode == 0:
        return {"ok": True, "pushed": True}

    # Non-fast-forward: fetch + rebase and retry once
    _git(["fetch", "origin", branch], cwd=worktree, check=False)
    rebase = _git(["rebase", f"origin/{branch}"], cwd=worktree, check=False)
    if rebase.returncode != 0:
        _git(["rebase", "--abort"], cwd=worktree, check=False)
        return {"ok": False, "error": "push rejected and rebase failed"}
    retry = _git(["push", "origin", branch], cwd=worktree, check=False)
    if retry.returncode == 0:
        return {"ok": True, "pushed": True}
    return {"ok": False, "error": push_result.stderr.strip()}


def git_setup_worktree(worktree: Path, remote: str, branch: str, author: str) -> dict[str, Any]:
    """Clone existing remote branch or create orphan branch and push."""
    if worktree.exists() and (worktree / ".git").exists():
        return {"ok": True, "action": "already_exists"}

    worktree.mkdir(parents=True, exist_ok=True)
    _git(["init"], cwd=worktree)
    # Configure git identity in worktree so merge commits have correct author
    _m = re.match(r'^(.+?)\s*<(.+?)>', author)
    if _m:
        _git(["config", "user.name", _m.group(1).strip()], cwd=worktree)
        _git(["config", "user.email", _m.group(2).strip()], cwd=worktree)
    _git(["remote", "add", "origin", remote], cwd=worktree)

    # Try fetching existing branch first
    fetch = _git(["fetch", "origin", branch], cwd=worktree, check=False)
    if fetch.returncode == 0:
        _git(["checkout", "-b", branch, f"origin/{branch}"], cwd=worktree)
        return {"ok": True, "action": "cloned"}

    # Branch doesn't exist: create orphan + push
    _git(["checkout", "--orphan", branch], cwd=worktree)
    _git(
        ["commit", "--allow-empty", "-m", "chore: init emerge-hub", "--author", author],
        cwd=worktree,
    )
    _git(["push", "-u", "origin", branch], cwd=worktree)
    return {"ok": True, "action": "created"}


# ── Conflict detection ──────────────────────────────────────────────────────

def _build_conflict_entries(
    conflict_files: list[str],
    connector: str,
    pipelines_registry: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build structured conflict entries with metadata for AI analysis."""
    entries = []
    for file_path in conflict_files:
        cid = new_conflict_id()
        entry: dict[str, Any] = {
            "conflict_id": cid,
            "connector": connector,
            "file": file_path,
            "status": "pending",
            "resolution": None,
            "ours_ts_ms": int(time.time() * 1000),
            "theirs_ts_ms": 0,
        }
        if pipelines_registry:
            mode = "write" if "/write/" in file_path else "read"
            pipeline_key = f"{connector}.{mode}.{Path(file_path).stem}"
            reg_entry = pipelines_registry.get("pipelines", {}).get(pipeline_key)
            if isinstance(reg_entry, dict):
                entry["ours_success_rate"] = reg_entry.get("success_rate", 0.0)
                entry["ours_attempts"] = reg_entry.get("attempts", 0)
        entries.append(entry)
    return entries


def record_conflicts(connector: str, conflict_files: list[str]) -> None:
    """Write conflict entries to pending-conflicts.json."""
    data = load_pending_conflicts()
    new_entries = _build_conflict_entries(conflict_files, connector)
    data["conflicts"].extend(new_entries)
    save_pending_conflicts(data)
    logger.warning(
        "Hub sync: %d conflict(s) for connector '%s' — resolve via icc_hub(action='status')",
        len(new_entries), connector,
    )


# ── Resolution application ──────────────────────────────────────────────────

def _apply_pending_resolutions(worktree: Path) -> bool:
    """Apply any resolved conflicts.

    The merge that produced the conflict was already aborted, so the worktree
    is at HEAD (our version).  Resolution is applied as follows:
      - "ours":   file is already at our HEAD version — mark applied, no git op.
      - "theirs": read the remote version via `git show origin/<branch>:<file>`
                  and write it to disk so it can be staged and committed.

    Returns True if any resolutions were applied.
    """
    cfg = load_hub_config()
    branch = cfg.get("branch", "emerge-hub")
    data = load_pending_conflicts()
    resolved = [
        c for c in data.get("conflicts", [])
        if c.get("status") == "resolved" and c.get("resolution") in ("ours", "theirs", "skip")
    ]
    if not resolved:
        return False

    any_applied = False
    for conflict in resolved:
        file_path = conflict["file"]
        resolution = conflict["resolution"]
        if resolution in ("ours", "skip"):
            # "ours": merge was aborted — file is already at our HEAD version.
            # "skip": user chose to ignore this conflict; leave the file as-is.
            conflict["status"] = "applied"
            any_applied = True
        elif resolution == "theirs":
            # Retrieve the remote version and write it to the worktree.
            result = _git(
                ["show", f"origin/{branch}:{file_path}"],
                cwd=worktree,
                check=False,
            )
            if result.returncode == 0:
                target = worktree / file_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(result.stdout, encoding="utf-8")
                _git(["add", file_path], cwd=worktree, check=False)
                conflict["status"] = "applied"
                any_applied = True
            else:
                logger.warning(
                    "Failed to get remote version for %s: %s",
                    file_path, result.stderr.strip(),
                )

    save_pending_conflicts(data)

    # Commit only if there are staged changes.
    diff = _git(["diff", "--cached", "--quiet"], cwd=worktree, check=False)
    if diff.returncode != 0:
        _git(["commit", "-m", "hub: apply conflict resolutions"], cwd=worktree, check=False)

    return any_applied


# ── Push and pull flows ─────────────────────────────────────────────────────

def push_flow(
    connector: str,
    *,
    connectors_root: Path | None = None,
    hub_worktree: Path | None = None,
) -> dict[str, Any]:
    """Full push flow: merge remote → export local → git commit+push."""
    cfg = load_hub_config()
    worktree = hub_worktree or hub_worktree_path()
    conns_root = connectors_root or _connectors_root()
    author = cfg.get("author", "emerge-sync <emerge-sync@local>")
    branch = cfg.get("branch", "emerge-hub")

    merge_result = git_merge_remote(worktree, branch, author=author)
    if merge_result.get("conflict"):
        record_conflicts(connector, merge_result["files"])
        return {"ok": False, "conflict": True, "files": merge_result["files"]}
    if not merge_result.get("ok"):
        return {"ok": False, "error": merge_result.get("error", "merge failed")}

    export_vertical(connector, connectors_root=conns_root, hub_worktree=worktree)

    return git_push(worktree, branch, connector=connector, author=author)


def pull_flow(
    connector: str,
    *,
    connectors_root: Path | None = None,
    hub_worktree: Path | None = None,
) -> dict[str, Any]:
    """Full pull flow: fetch → check for changes → merge → import."""
    cfg = load_hub_config()
    worktree = hub_worktree or hub_worktree_path()
    conns_root = connectors_root or _connectors_root()
    author = cfg.get("author", "emerge-sync <emerge-sync@local>")
    branch = cfg.get("branch", "emerge-hub")

    if not git_has_remote_changes(worktree, branch):
        return {"ok": True, "action": "up_to_date"}

    merge_result = git_merge_remote(worktree, branch, author=author)
    if merge_result.get("conflict"):
        record_conflicts(connector, merge_result["files"])
        return {"ok": False, "conflict": True, "files": merge_result["files"]}
    if not merge_result.get("ok"):
        return {"ok": False, "error": merge_result.get("error", "merge failed")}

    import_vertical(connector, connectors_root=conns_root, hub_worktree=worktree)
    return {"ok": True, "action": "imported"}


# ── Poll loop ──────────────────────────────────────────────────────────────

def _run_stable_events() -> None:
    """Consume 'stable' and 'pull_requested' events from sync-queue."""
    cfg = load_hub_config()
    worktree = hub_worktree_path()
    if worktree.exists():
        _apply_pending_resolutions(worktree)
    selected = set(cfg.get("selected_verticals", []))
    events = consume_sync_events(
        lambda e: e.get("event") in ("stable", "pull_requested") and e.get("connector") in selected
    )
    push_processed: set[str] = set()
    push_conflicts: set[str] = set()
    pull_requested: set[str] = set()
    for event in events:
        connector = event["connector"]
        if event.get("event") == "pull_requested":
            pull_requested.add(connector)
            continue
        if connector in push_processed:
            continue
        push_processed.add(connector)
        try:
            result = push_flow(connector)
            if result.get("ok"):
                logger.info("Hub push OK for %s", connector)
            elif result.get("conflict"):
                push_conflicts.add(connector)
                logger.warning("Hub push conflict for %s — %d file(s)", connector, len(result.get("files", [])))
            else:
                logger.error("Hub push failed for %s: %s", connector, result.get("error", "unknown"))
        except Exception as exc:
            logger.error("Hub push exception for %s: %s", connector, exc)

    for connector in pull_requested:
        if connector in push_conflicts:
            # Push already recorded conflicts for this connector; pulling against the same
            # remote would trigger another conflicting merge and duplicate conflict entries.
            logger.info("Hub pull skipped for %s — unresolved push conflict pending", connector)
            continue
        try:
            result = pull_flow(connector)
            if result.get("action") == "imported":
                logger.info("Hub pull OK for %s (manual sync)", connector)
        except Exception as exc:
            logger.error("Hub pull exception for %s: %s", connector, exc)


def _run_pull_cycle() -> None:
    """Pull updates for all selected verticals."""
    cfg = load_hub_config()
    for connector in cfg.get("selected_verticals", []):
        try:
            result = pull_flow(connector)
            if result.get("action") == "imported":
                logger.info("Hub pull: imported updates for %s", connector)
        except Exception as exc:
            logger.error("Hub pull exception for %s: %s", connector, exc)


def run_poll_loop(stop_event: threading.Event | None = None) -> None:
    """Main sync agent loop. Polls stable events and runs periodic pull.

    Note: poll_interval is read once at startup. Restart the agent to pick up
    config changes made via icc_hub add/remove.
    """
    cfg = load_hub_config()
    poll_interval = int(cfg.get("poll_interval_seconds", 300))
    last_pull = 0.0
    logger.info("emerge_sync: poll loop started (interval=%ds)", poll_interval)
    while True:
        if stop_event and stop_event.is_set():
            break
        try:
            _run_stable_events()
            now = time.time()
            if now - last_pull >= poll_interval:
                _run_pull_cycle()
                last_pull = now
        except Exception as exc:
            logger.error("emerge_sync poll loop error: %s", exc)
        time.sleep(10)


# ── CLI entry point ─────────────────────────────────────────────────────────

def cmd_setup() -> None:
    """Interactive setup wizard."""
    print("emerge_sync setup")
    remote = input("Remote URL (e.g. git@quasar:team/hub.git): ").strip()
    branch = input("Branch name [emerge-hub]: ").strip() or "emerge-hub"
    author = input("Author (e.g. alice <alice@team.com>): ").strip()

    conns_root = _connectors_root()
    available: list[str] = []
    if conns_root.exists():
        available = [d.name for d in conns_root.iterdir() if d.is_dir()]
    if not available:
        print("No local connectors found. Add connectors first.")
        return
    print(f"Available connectors: {', '.join(available)}")
    selected_input = input("Select connectors (comma-separated): ").strip()
    selected = [s.strip() for s in selected_input.split(",") if s.strip() in available]

    cfg = {
        "remote": remote,
        "branch": branch,
        "poll_interval_seconds": 300,
        "selected_verticals": selected,
        "author": author,
    }
    save_hub_config(cfg)

    worktree = hub_worktree_path()
    print(f"Setting up hub worktree at {worktree}...")
    result = git_setup_worktree(worktree, remote, branch, author)
    print(f"Worktree ready: {result['action']}")
    print("Running initial pull...")
    for connector in selected:
        pull_flow(connector)
    print("Setup complete.")


def cmd_sync(connector: str | None = None) -> None:
    cfg = load_hub_config()
    verticals = [connector] if connector else cfg.get("selected_verticals", [])
    for c in verticals:
        result = push_flow(c)
        if result.get("ok"):
            print(f"sync {c}: ok (push)")
        elif result.get("conflict"):
            print(f"sync {c}: conflict — resolve via icc_hub(action='status')")
            continue  # skip pull: would re-merge against the same conflicting remote
        else:
            print(f"sync {c}: error — {result.get('error', 'unknown')}")
        pull_result = pull_flow(c)
        if pull_result.get("action") == "imported":
            print(f"sync {c}: ok (pull — imported updates)")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = sys.argv[1:]
    if not args or args[0] == "run":
        if not is_configured():
            print("Not configured. Run: python scripts/emerge_sync.py setup")
            sys.exit(1)
        run_poll_loop()
    elif args[0] == "setup":
        cmd_setup()
    elif args[0] == "sync":
        connector_arg = args[1] if len(args) > 1 else None
        cmd_sync(connector_arg)
    else:
        print(f"Unknown command: {args[0]}")
        print("Usage: emerge_sync.py [run|setup|sync [connector]]")
        sys.exit(1)
