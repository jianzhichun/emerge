"""Watcher heartbeat + event-stream SLO helpers.

Each long-running ``watch_emerge.py`` instance writes a JSON heartbeat under
``state/events/watchers/<watcher_id>.json``. The cockpit reads these files to
expose a "watcher is alive / <N>s since last loop / <M> events delivered"
signal, so operators can see when a Monitor subagent is stuck or has crashed.

This module intentionally depends on nothing heavier than the stdlib + the
``policy_config`` helpers, so it can be imported both from the watcher process
and from the cockpit HTTP server without creating circular import risk.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from scripts.policy_config import atomic_write_json, events_root


def watcher_stale_after_s() -> int:
    """Seconds after which a watcher is considered stale.

    Overridable via ``EMERGE_WATCHER_STALE_S``. Values ``<= 0`` fall back to
    the default of 60 s so a misconfigured env var never silently disables
    health reporting.
    """
    raw = os.environ.get("EMERGE_WATCHER_STALE_S", "").strip()
    if not raw:
        return 60
    try:
        value = int(raw)
    except ValueError:
        return 60
    return value if value > 0 else 60


def watchers_dir(state_root: Path) -> Path:
    """Return ``state/events/watchers`` for *state_root*."""
    return events_root(state_root) / "watchers"


def watcher_heartbeat_path(state_root: Path, watcher_id: str) -> Path:
    """Return the heartbeat file path for *watcher_id* under *state_root*."""
    safe = watcher_id.strip().replace("/", "_").replace("\\", "_")
    if not safe:
        safe = "unknown"
    return watchers_dir(state_root) / f"{safe}.json"


def write_heartbeat(state_root: Path, record: dict[str, Any]) -> Path:
    """Atomically persist *record* as the current heartbeat.

    The caller is responsible for supplying a ``watcher_id`` key; all other
    fields are forwarded as-is. A best-effort ``updated_at_ms`` stamp is
    injected if missing so downstream consumers always see a monotonic
    timestamp.
    """
    watcher_id = str(record.get("watcher_id", "")).strip()
    if not watcher_id:
        raise ValueError("heartbeat record must include 'watcher_id'")
    record = dict(record)
    record.setdefault("updated_at_ms", int(time.time() * 1000))
    path = watcher_heartbeat_path(state_root, watcher_id)
    atomic_write_json(path, record)
    return path


def read_all_heartbeats(state_root: Path) -> list[dict[str, Any]]:
    """Return every heartbeat file's parsed contents (most-recent first)."""
    directory = watchers_dir(state_root)
    if not directory.exists():
        return []
    records: list[dict[str, Any]] = []
    for child in sorted(directory.glob("*.json")):
        try:
            data = json.loads(child.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            records.append(data)
    records.sort(key=lambda r: int(r.get("last_loop_ts_ms", r.get("updated_at_ms", 0)) or 0), reverse=True)
    return records


def compute_watcher_status(
    record: dict[str, Any], *, now_ms: int | None = None, stale_after_s: int | None = None
) -> dict[str, Any]:
    """Return a summary dict enriched with ``alive`` / ``lag_ms`` fields.

    ``record`` is expected to be a parsed heartbeat file. We do not mutate it
    in place — the caller gets a fresh dict that is safe to return as JSON.
    """
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    threshold_ms = (stale_after_s if stale_after_s is not None else watcher_stale_after_s()) * 1000
    last_ts = int(record.get("last_loop_ts_ms", 0) or record.get("updated_at_ms", 0) or 0)
    lag_ms = max(0, now - last_ts) if last_ts else None
    stopped_at = record.get("stopped_at_ms")
    alive = bool(last_ts) and (lag_ms is not None and lag_ms < threshold_ms) and not stopped_at
    return {
        "watcher_id": record.get("watcher_id"),
        "pid": record.get("pid"),
        "target": record.get("target"),
        "state_root": record.get("state_root"),
        "started_at_ms": record.get("started_at_ms"),
        "last_loop_ts_ms": last_ts or None,
        "updated_at_ms": record.get("updated_at_ms"),
        "stopped_at_ms": stopped_at,
        "events_read": int(record.get("events_read", 0) or 0),
        "events_delivered": int(record.get("events_delivered", 0) or 0),
        "last_event_id": record.get("last_event_id"),
        "last_error": record.get("last_error"),
        "consecutive_errors": int(record.get("consecutive_errors", 0) or 0),
        "alive": alive,
        "lag_ms": lag_ms,
        "stale_threshold_ms": threshold_ms,
    }


def watcher_health_summary(state_root: Path) -> dict[str, Any]:
    """Aggregate all heartbeats into a {healthy, alive, total, stale, watchers} payload."""
    now = int(time.time() * 1000)
    records = read_all_heartbeats(state_root)
    watchers = [compute_watcher_status(r, now_ms=now) for r in records]
    alive = [w for w in watchers if w["alive"]]
    stale = [w for w in watchers if not w["alive"]]
    return {
        "ok": True,
        "total": len(watchers),
        "alive_count": len(alive),
        "stale_count": len(stale),
        # healthy = no watchers registered, or every registered watcher alive.
        "healthy": len(stale) == 0,
        "stale_watcher_ids": [w["watcher_id"] for w in stale if w.get("watcher_id")],
        "watchers": watchers,
        "stale_threshold_s": watcher_stale_after_s(),
    }
