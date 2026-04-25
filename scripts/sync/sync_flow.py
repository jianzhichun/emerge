"""High-level sync flows and event/poll loops for Memory Hub.

push_flow / pull_flow orchestrate git_ops + asset_ops.
run_event_loop is the background agent entry point.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from scripts.hub_config import (
    consume_sync_events,
    hub_worktree_path,
    load_hub_config,
    sync_queue_path,
)
from scripts.sync.asset_ops import connectors_root, export_vertical, import_vertical
from scripts.sync.git_ops import (
    apply_pending_resolutions,
    git_has_remote_changes,
    git_merge_remote,
    git_push,
    record_conflicts,
)

logger = logging.getLogger(__name__)


def sync_mode() -> str:
    raw = os.environ.get("EMERGE_SYNC_MODE", "").strip().lower()
    if raw in {"read-only", "readonly", "pull-only", "runner"}:
        return "read-only"
    return "read-write"


def sync_connector(
    connector: str,
    *,
    connectors_root_path: Path | None = None,
    hub_worktree: Path | None = None,
) -> dict[str, Any]:
    mode = sync_mode()
    if mode == "read-only":
        pull = pull_flow(connector, connectors_root_path=connectors_root_path, hub_worktree=hub_worktree)
        return {"ok": bool(pull.get("ok")), "mode": mode, "pull": pull}
    push = push_flow(connector, connectors_root_path=connectors_root_path, hub_worktree=hub_worktree)
    result: dict[str, Any] = {"ok": bool(push.get("ok")), "mode": mode, "push": push}
    if push.get("ok"):
        result["pull"] = pull_flow(connector, connectors_root_path=connectors_root_path, hub_worktree=hub_worktree)
    return result


def push_flow(
    connector: str,
    *,
    connectors_root_path: Path | None = None,
    hub_worktree: Path | None = None,
) -> dict[str, Any]:
    """Full push flow: merge remote → export local → git commit+push."""
    cfg = load_hub_config()
    worktree = hub_worktree or hub_worktree_path()
    conns_root = connectors_root_path or connectors_root()
    author = cfg.get("author", "emerge-sync <emerge-sync@local>")
    branch = cfg.get("branch", "emerge-hub")

    merge_result = git_merge_remote(worktree, branch, author=author)
    if merge_result.get("conflict"):
        record_conflicts(connector, merge_result["files"])
        return {"ok": False, "conflict": True, "files": merge_result["files"]}
    if not merge_result.get("ok"):
        return {"ok": False, "error": merge_result.get("error", "merge failed")}

    export_vertical(connector, connectors_root_path=conns_root, hub_worktree=worktree)
    return git_push(worktree, branch, connector=connector, author=author)


def pull_flow(
    connector: str,
    *,
    connectors_root_path: Path | None = None,
    hub_worktree: Path | None = None,
) -> dict[str, Any]:
    """Full pull flow: fetch → check for changes → merge → import."""
    cfg = load_hub_config()
    worktree = hub_worktree or hub_worktree_path()
    conns_root = connectors_root_path or connectors_root()
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

    import_vertical(connector, connectors_root_path=conns_root, hub_worktree=worktree)
    return {"ok": True, "action": "imported"}


def _run_stable_events() -> None:
    """Consume 'stable' and 'pull_requested' events from sync-queue."""
    cfg = load_hub_config()
    worktree = hub_worktree_path()
    if worktree.exists():
        apply_pending_resolutions(worktree)
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
        if sync_mode() == "read-only":
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


def run_event_loop(stop_event: threading.Event | None = None) -> None:
    """Event-driven sync agent. Watches sync-queue.jsonl via EventRouter."""
    from scripts.event_router import EventRouter

    logger.info("emerge_sync: event loop started")

    _timer: list[threading.Timer] = []

    def _schedule_pull() -> None:
        cfg2 = load_hub_config()
        interval = int(cfg2.get("poll_interval_seconds", 300))
        try:
            _run_pull_cycle()
        except Exception as exc:
            logger.error("emerge_sync pull cycle error: %s", exc)
        if stop_event is None or not stop_event.is_set():
            t = threading.Timer(interval, _schedule_pull)
            t.daemon = True
            _timer.clear()
            _timer.append(t)
            t.start()

    def _on_queue_change(_path: Path) -> None:
        try:
            _run_stable_events()
        except Exception as exc:
            logger.error("emerge_sync stable events error: %s", exc)

    router = EventRouter({sync_queue_path(): _on_queue_change})
    router.start()
    _schedule_pull()

    try:
        while True:
            if stop_event:
                if stop_event.wait(timeout=1.0):
                    break
            else:
                time.sleep(1.0)
    finally:
        router.stop()
        for t in _timer:
            t.cancel()
