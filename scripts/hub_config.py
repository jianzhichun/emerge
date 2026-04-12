from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable


def _hub_home() -> Path:
    """Base directory for all hub state files. Override with EMERGE_HUB_HOME for tests."""
    override = os.environ.get("EMERGE_HUB_HOME")
    if override:
        return Path(override)
    return Path.home() / ".emerge"


def hub_config_path() -> Path:
    return _hub_home() / "hub-config.json"


def hub_worktree_path() -> Path:
    return _hub_home() / "hub-worktree"


def sync_queue_path() -> Path:
    return _hub_home() / "sync-queue.jsonl"


def pending_conflicts_path() -> Path:
    return _hub_home() / "pending-conflicts.json"

from scripts.policy_config import atomic_write_json  # noqa: E402


# ── Config ──────────────────────────────────────────────────────────────────

def load_hub_config() -> dict[str, Any]:
    """Load hub-config.json. Returns {} if missing or corrupt."""
    p = hub_config_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_hub_config(config: dict[str, Any]) -> None:
    """Atomically write hub-config.json."""
    atomic_write_json(hub_config_path(), config)


def is_configured() -> bool:
    cfg = load_hub_config()
    return bool(cfg.get("remote") and cfg.get("selected_verticals"))


# ── Sync queue ──────────────────────────────────────────────────────────────

def append_sync_event(event: dict[str, Any]) -> None:
    """Append a single event to sync-queue.jsonl."""
    p = sync_queue_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(str(p), "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")
        f.flush()
        os.fsync(f.fileno())


def consume_sync_events(predicate: Callable[[dict[str, Any]], bool]) -> list[dict[str, Any]]:
    """Read sync-queue.jsonl, return events matching predicate, rewrite queue with non-matching events."""
    p = sync_queue_path()
    if not p.exists():
        return []

    lines = p.read_text(encoding="utf-8").splitlines()
    matched: list[dict[str, Any]] = []
    remaining: list[str] = []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except Exception:
            remaining.append(line)
            continue
        if predicate(event):
            matched.append(event)
        else:
            remaining.append(line)

    if remaining:
        _atomic_write_json_lines(p, remaining)
    else:
        try:
            p.unlink()
        except OSError:
            pass

    return matched


def _atomic_write_json_lines(path: Path, lines: list[str]) -> None:
    """Atomically rewrite a JSONL file with the given lines."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for line in lines:
                f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── Pending conflicts ────────────────────────────────────────────────────────

def load_pending_conflicts() -> dict[str, Any]:
    """Load pending-conflicts.json. Returns {"conflicts": []} if missing or corrupt."""
    p = pending_conflicts_path()
    if not p.exists():
        return {"conflicts": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if "conflicts" not in data:
            data["conflicts"] = []
        return data
    except Exception:
        return {"conflicts": []}


def save_pending_conflicts(data: dict[str, Any]) -> None:
    """Atomically write pending-conflicts.json."""
    atomic_write_json(pending_conflicts_path(), data)


def new_conflict_id() -> str:
    return uuid.uuid4().hex[:12]
