"""Git worktree operations for Memory Hub.

All subprocess git calls live here. No connector asset logic — see asset_ops.py.
"""
from __future__ import annotations

import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from scripts.hub_config import (
    load_hub_config,
    load_pending_conflicts,
    new_conflict_id,
    save_pending_conflicts,
)

logger = logging.getLogger(__name__)


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
    _m = re.match(r'^(.+?)\s*<(.+?)>', author)
    if _m:
        _git(["config", "user.name", _m.group(1).strip()], cwd=worktree)
        _git(["config", "user.email", _m.group(2).strip()], cwd=worktree)
    _git(["remote", "add", "origin", remote], cwd=worktree)

    fetch = _git(["fetch", "origin", branch], cwd=worktree, check=False)
    if fetch.returncode == 0:
        _git(["checkout", "-b", branch, f"origin/{branch}"], cwd=worktree)
        return {"ok": True, "action": "cloned"}

    _git(["checkout", "--orphan", branch], cwd=worktree)
    _git(
        ["commit", "--allow-empty", "-m", "chore: init emerge-hub", "--author", author],
        cwd=worktree,
    )
    _git(["push", "-u", "origin", branch], cwd=worktree)
    return {"ok": True, "action": "created"}


# ── Conflict detection ──────────────────────────────────────────────────────

def build_conflict_entries(
    conflict_files: list[str],
    connector: str,
    intents_registry: dict[str, Any] | None = None,
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
        if intents_registry:
            mode = "write" if "/write/" in file_path else "read"
            pipeline_key = f"{connector}.{mode}.{Path(file_path).stem}"
            reg_entry = intents_registry.get("intents", {}).get(pipeline_key)
            if isinstance(reg_entry, dict):
                entry["ours_success_rate"] = reg_entry.get("success_rate", 0.0)
                entry["ours_attempts"] = reg_entry.get("attempts", 0)
        entries.append(entry)
    return entries


def record_conflicts(connector: str, conflict_files: list[str]) -> None:
    """Write conflict entries to pending-conflicts.json."""
    data = load_pending_conflicts()
    new_entries = build_conflict_entries(conflict_files, connector)
    data["conflicts"].extend(new_entries)
    save_pending_conflicts(data)
    logger.warning(
        "Hub sync: %d conflict(s) for connector '%s' — resolve via icc_hub(action='status')",
        len(new_entries), connector,
    )


# ── Resolution application ──────────────────────────────────────────────────

def apply_pending_resolutions(worktree: Path) -> bool:
    """Apply any resolved conflicts. Returns True if any resolutions were applied."""
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
            conflict["status"] = "applied"
            any_applied = True
        elif resolution == "theirs":
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

    diff = _git(["diff", "--cached", "--quiet"], cwd=worktree, check=False)
    if diff.returncode != 0:
        _git(["commit", "-m", "hub: apply conflict resolutions"], cwd=worktree, check=False)

    return any_applied
