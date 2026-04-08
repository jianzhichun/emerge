from __future__ import annotations

import json
import logging
import os
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
    append_sync_event,
    consume_sync_events,
    hub_worktree_path,
    is_configured,
    load_hub_config,
    load_pending_conflicts,
    new_conflict_id,
    save_hub_config,
    save_pending_conflicts,
    sync_queue_path,
)

logger = logging.getLogger(__name__)

_PIPELINE_EXTENSIONS = (".py", ".yaml")
_PRIVATE_DIRS = {"operator-events", "credentials"}


def _connectors_root() -> Path:
    override = os.environ.get("EMERGE_CONNECTOR_ROOT")
    if override:
        return Path(override)
    return Path.home() / ".emerge" / "connectors"


# ── Export ──────────────────────────────────────────────────────────────────

def export_vertical(
    connector: str,
    *,
    connectors_root: Path | None = None,
    hub_worktree: Path | None = None,
) -> None:
    """Copy connector assets from local connectors dir into the hub worktree."""
    src = (connectors_root or _connectors_root()) / connector
    dst = (hub_worktree or hub_worktree_path()) / "connectors" / connector

    src_pipelines = src / "pipelines"
    dst_pipelines = dst / "pipelines"
    if src_pipelines.exists():
        if dst_pipelines.exists():
            shutil.rmtree(dst_pipelines)
        shutil.copytree(src_pipelines, dst_pipelines)

    notes_src = src / "NOTES.md"
    if notes_src.exists():
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copy2(notes_src, dst / "NOTES.md")

    _export_spans_json(src, dst)


def _export_spans_json(src: Path, dst: Path) -> None:
    """Generate spans.json from span-candidates.json (stable entries only, stripped)."""
    candidates_path = src / "span-candidates.json"
    if not candidates_path.exists():
        return
    try:
        raw = json.loads(candidates_path.read_text(encoding="utf-8"))
        candidates = raw.get("candidates", {})
    except Exception:
        return
    spans: dict[str, Any] = {}
    for key, entry in candidates.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("status") != "stable":
            continue
        spans[key] = {
            "intent_signature": entry.get("intent_signature", key),
            "status": "stable",
            "last_ts_ms": entry.get("last_ts_ms", 0),
        }
    dst.mkdir(parents=True, exist_ok=True)
    (dst / "spans.json").write_text(
        json.dumps({"spans": spans}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


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

    # Conflict: collect conflicting file paths
    status = _git(["diff", "--name-only", "--diff-filter=U"], cwd=worktree, check=False)
    conflict_files = [f.strip() for f in status.stdout.splitlines() if f.strip()]
    _git(["merge", "--abort"], cwd=worktree, check=False)
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
    import re as _re
    _m = _re.match(r'^(.+?)\s*<(.+?)>', author)
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
