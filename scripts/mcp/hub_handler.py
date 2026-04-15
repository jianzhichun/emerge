"""MCP icc_hub tool handler for EmergeDaemon.

handle_icc_hub() is a pure function — no daemon state access. It receives
three callables (tool_error, tool_ok_json, elicit) so it can be tested
independently and called from any context.
"""
from __future__ import annotations

import time as _time
from typing import Any, Callable


def handle_icc_hub(
    arguments: dict[str, Any],
    *,
    tool_error: Callable[[str], dict[str, Any]],
    tool_ok_json: Callable[[dict[str, Any]], dict[str, Any]],
    elicit: Callable[..., dict[str, Any] | None],
    http_mode: bool = False,
) -> dict[str, Any]:
    """Dispatch an icc_hub action and return a MCP tool response dict."""
    from scripts.hub_config import (
        append_sync_event,
        is_configured,
        load_hub_config,
        load_pending_conflicts,
        save_hub_config,
        save_pending_conflicts,
        sync_queue_path,
    )

    action = str(arguments.get("action", "")).strip()

    if action == "list":
        cfg = load_hub_config()
        return tool_ok_json({
            "remote": cfg.get("remote", ""),
            "branch": cfg.get("branch", "emerge-hub"),
            "selected_verticals": cfg.get("selected_verticals", []),
            "poll_interval_seconds": cfg.get("poll_interval_seconds", 300),
            "configured": is_configured(),
        })

    if action == "add":
        connector = str(arguments.get("connector", "")).strip()
        if not connector:
            return tool_error("icc_hub add: 'connector' is required")
        cfg = load_hub_config()
        selected = list(cfg.get("selected_verticals", []))
        if connector not in selected:
            selected.append(connector)
            cfg["selected_verticals"] = selected
            save_hub_config(cfg)
        return tool_ok_json({"ok": True, "selected_verticals": selected})

    if action == "remove":
        connector = str(arguments.get("connector", "")).strip()
        if not connector:
            return tool_error("icc_hub remove: 'connector' is required")
        cfg = load_hub_config()
        selected = [v for v in cfg.get("selected_verticals", []) if v != connector]
        cfg["selected_verticals"] = selected
        save_hub_config(cfg)
        return tool_ok_json({"ok": True, "selected_verticals": selected})

    if action == "status":
        cfg = load_hub_config()
        pending = load_pending_conflicts()
        all_conflicts = pending.get("conflicts", [])
        unresolved = [c for c in all_conflicts if c.get("status") == "pending"]
        awaiting_apply = [c for c in all_conflicts if c.get("status") == "resolved"]
        queue_depth = 0
        qp = sync_queue_path()
        if qp.exists():
            queue_depth = sum(1 for line in qp.read_text(encoding="utf-8").splitlines() if line.strip())
        return tool_ok_json({
            "configured": is_configured(),
            "remote": cfg.get("remote", ""),
            "selected_verticals": cfg.get("selected_verticals", []),
            "pending_conflicts": len(unresolved),
            "conflicts": unresolved,
            "awaiting_application": len(awaiting_apply),
            "queue_depth": queue_depth,
        })

    if action == "sync":
        connector = str(arguments.get("connector", "")).strip() or None
        cfg = load_hub_config()
        verticals = [connector] if connector else cfg.get("selected_verticals", [])
        ts = int(_time.time() * 1000)
        for c in verticals:
            append_sync_event({"event": "stable", "connector": c, "pipeline": "__manual__", "ts_ms": ts})
            append_sync_event({"event": "pull_requested", "connector": c, "ts_ms": ts})
        return tool_ok_json({"ok": True, "triggered": verticals})

    if action == "configure":
        remote = str(arguments.get("remote", "")).strip()
        if not remote:
            return tool_error("icc_hub configure: 'remote' is required (e.g. user@host:repos/hub.git)")
        branch = str(arguments.get("branch", "emerge-hub")).strip() or "emerge-hub"
        author = str(arguments.get("author", "")).strip()
        if not author:
            return tool_error(
                "icc_hub configure: 'author' is required (e.g. 'Alice <alice@team.com>')"
            )
        poll_interval = int(arguments.get("poll_interval_seconds", 300))
        new_verticals = arguments.get("selected_verticals")
        if isinstance(new_verticals, str):
            new_verticals = [v.strip() for v in new_verticals.split(",") if v.strip()]
        if not isinstance(new_verticals, list):
            new_verticals = []

        cfg = load_hub_config()
        cfg.update({
            "remote": remote,
            "branch": branch,
            "author": author,
            "poll_interval_seconds": poll_interval,
        })
        if new_verticals:
            cfg["selected_verticals"] = new_verticals
        elif "selected_verticals" not in cfg:
            cfg["selected_verticals"] = []
        save_hub_config(cfg)

        try:
            from scripts.emerge_sync import git_setup_worktree
            from scripts.hub_config import hub_worktree_path
            worktree = hub_worktree_path()
            result = git_setup_worktree(worktree, remote, branch, author)
            action_taken = result.get("action", "unknown")

            if action_taken == "cloned" and cfg.get("selected_verticals"):
                import logging as _logging

                from scripts.emerge_sync import import_vertical as _import_vertical

                _log = _logging.getLogger(__name__)
                for _connector in cfg["selected_verticals"]:
                    try:
                        _import_vertical(_connector, hub_worktree=worktree)
                    except Exception as _exc:
                        _log.warning(
                            "icc_hub configure: initial import failed for %s: %s",
                            _connector,
                            _exc,
                        )
        except Exception as exc:
            return tool_error(
                f"icc_hub configure: git worktree init failed — {exc}. "
                "Check that the remote URL is reachable and SSH keys are in place."
            )

        return tool_ok_json({
            "ok": True,
            "action": action_taken,  # "created" | "cloned" | "already_exists"
            "remote": remote,
            "branch": branch,
            "selected_verticals": cfg["selected_verticals"],
            "worktree": str(hub_worktree_path()),
            "next": (
                "Hub configured. Start the sync agent in a terminal: "
                "python scripts/emerge_sync.py run"
            ),
        })

    if action == "setup":
        return tool_ok_json({
            "ok": True,
            "message": (
                "Use icc_hub(action='configure', remote='user@host:repos/hub.git', "
                "author='Name <email>', selected_verticals=['connector1']) to configure "
                "the hub directly from Claude Code. "
                "Or run the interactive CLI wizard: python scripts/emerge_sync.py setup"
            ),
        })

    if action == "resolve":
        conflict_id = str(arguments.get("conflict_id", "")).strip()
        resolution = str(arguments.get("resolution", "")).strip()
        if not conflict_id:
            return tool_error("icc_hub resolve: 'conflict_id' is required")
        if resolution not in ("ours", "theirs", "skip"):
            if http_mode:
                return tool_error(
                    "icc_hub resolve: 'resolution' is required (ours/theirs/skip). "
                    "Example: icc_hub(action='resolve', conflict_id='...', resolution='ours')"
                )
            elicit_resp = elicit(
                f"Choose the resolution strategy for conflict `{conflict_id}`:",
                {
                    "type": "object",
                    "properties": {
                        "resolution": {
                            "type": "string",
                            "enum": ["ours", "theirs", "skip"],
                            "title": "Resolution strategy",
                        }
                    },
                    "required": ["resolution"],
                },
            )
            if elicit_resp is None:
                return tool_error(
                    "icc_hub resolve: elicitation declined or timed out — operation cancelled"
                )
            resolution = str(elicit_resp.get("resolution", "")).strip()
            if resolution not in ("ours", "theirs", "skip"):
                return tool_error(
                    f"icc_hub resolve: invalid resolution from elicitation: {resolution!r}"
                )
        data = load_pending_conflicts()
        matched = False
        for conflict in data.get("conflicts", []):
            if conflict.get("conflict_id") == conflict_id:
                conflict["resolution"] = resolution
                conflict["status"] = "resolved"
                matched = True
                break
        if not matched:
            return tool_error(f"icc_hub resolve: conflict_id '{conflict_id}' not found")
        save_pending_conflicts(data)
        return tool_ok_json({"ok": True, "conflict_id": conflict_id, "resolution": resolution})

    return tool_error(
        f"icc_hub: unknown action '{action}'. Valid: configure|list|add|remove|sync|status|resolve|setup"
    )
