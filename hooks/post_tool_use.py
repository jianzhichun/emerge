"""PostToolUse hook for emerge MCP tools (icc_read/write/exec/reconcile/crystallize).

General CC tool calls (Bash, Read, Grep, etc.) are handled by tool_audit.py via
a separate PostToolUse matcher. This script only runs for emerge-specific tools.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.goal_control_plane import GoalControlPlane  # noqa: E402
from scripts.policy_config import default_hook_state_root  # noqa: E402
from scripts.span_tracker import is_read_only_tool  # noqa: E402
from scripts.state_tracker import (  # noqa: E402
    LEVEL_CORE_CRITICAL,
    LEVEL_CORE_SECONDARY,
    LEVEL_PERIPHERAL,
    load_tracker,
    save_tracker,
)


def _classify_level(tool_name: str) -> str:
    if tool_name.endswith("__icc_write"):
        return LEVEL_CORE_CRITICAL
    if tool_name.endswith("__icc_read"):
        return LEVEL_CORE_SECONDARY
    return LEVEL_PERIPHERAL


def _record_span_action(
    tool_name: str, payload: dict, state_root: Path, state_path: Path
) -> tuple[str, str]:
    """Record tool invocation to the active span WAL.

    Returns (active_span_id, active_span_intent) — both empty strings when no span is active.
    Skips icc_exec: its code paths are captured in ExecSession WAL already.
    """
    if tool_name.endswith("__icc_exec") or not tool_name:
        return "", ""
    _active_span_id = ""
    _active_span_intent = ""
    try:
        _raw_state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        _active_span_id = str(_raw_state.get("active_span_id", "") or "")
        _active_span_intent = str(_raw_state.get("active_span_intent", "") or "")
    except Exception:
        return "", ""
    if not _active_span_id:
        return "", ""
    _has_se = not is_read_only_tool(tool_name)
    _args_raw = json.dumps(payload.get("tool_input", {}), sort_keys=True, ensure_ascii=True)
    _args_hash = hashlib.sha256(_args_raw.encode()).hexdigest()[:16]
    _action = {
        "tool_name": tool_name,
        "args_hash": _args_hash,
        "has_side_effects": _has_se,
        "ts_ms": int(time.time() * 1000),
    }
    _buf = state_root / "active-span-actions.jsonl"
    try:
        with _buf.open("a", encoding="utf-8") as _f:
            _f.write(json.dumps(_action, ensure_ascii=True) + "\n")
    except Exception:
        pass
    return _active_span_id, _active_span_intent


def main() -> None:
    payload_text = sys.stdin.read().strip()
    try:
        payload = json.loads(payload_text) if payload_text else {}
    except Exception:
        payload = {}

    tool_name = payload.get("tool_name", "")
    raw_result = payload.get("tool_result", {})
    result = raw_result if isinstance(raw_result, dict) else {}
    state_root = Path(default_hook_state_root())
    state_path = state_root / "state.json"
    tracker = load_tracker(state_path)
    goal_cp = GoalControlPlane(state_root)
    goal_cp.ensure_initialized()
    goal_cp.migrate_legacy_goal(
        legacy_goal=str(tracker.to_dict().get("goal", "")),
        legacy_source=str(tracker.to_dict().get("goal_source", "legacy")),
    )

    message = payload.get("delta_message") or f"Tool used: {tool_name or 'unknown'}"
    level = _classify_level(tool_name)
    provisional = bool(payload.get("provisional", False))

    tool_input = payload.get("tool_input", {}) or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    intent_signature = str(tool_input.get("intent_signature", "")).strip() or None

    # verification_state lives inside content[0]["text"] (serialized JSON), not in the
    # outer MCP wrapper. Parse it out; fall back to "verified" if absent or unparseable.
    verification_state = "verified"
    try:
        content = result.get("content", [])
        if content and isinstance(content[0], dict):
            inner = json.loads(content[0].get("text", "{}"))
            if isinstance(inner, dict) and "verification_state" in inner:
                verification_state = str(inner["verification_state"])
    except Exception:
        pass

    tracker.add_delta(
        message=message,
        level=level,
        verification_state=verification_state,
        provisional=provisional,
        intent_signature=intent_signature,
        tool_name=tool_name,
    )

    # Propagate degraded pipeline verification as an open risk
    if verification_state == "degraded" and not payload.get("mismatch_reason"):
        tracker.mark_degraded(f"pipeline verification failed: {tool_name}")

    if payload.get("mismatch_reason"):
        tracker.mark_degraded(str(payload["mismatch_reason"]))

    reconcile = payload.get("reconcile")
    if isinstance(reconcile, dict) and "delta_id" in reconcile and "outcome" in reconcile:
        tracker.reconcile_delta(str(reconcile["delta_id"]), str(reconcile["outcome"]))

    raw_budget = payload.get("budget_chars", 0)
    try:
        budget_chars = int(raw_budget)
        if budget_chars <= 0:
            budget_chars = None
    except Exception:
        budget_chars = None
    snap = goal_cp.read_snapshot()
    context_text = tracker.format_additional_context(
        budget_chars=budget_chars,
        goal_override=str(snap.get("text", "")),
        goal_source_override=str(snap.get("source", "unset")),
    )

    _active_span_id, _active_span_intent = _record_span_action(
        tool_name, payload, state_root, state_path
    )

    save_tracker(state_path, tracker)

    # Re-persist active span fields: save_tracker only writes known StateTracker
    # fields and drops active_span_id. Re-merge so subsequent PostToolUse calls
    # can still see the active span.
    if _active_span_id:
        try:
            _state_now = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
            _state_now["active_span_id"] = _active_span_id
            _state_now["active_span_intent"] = _active_span_intent
            _tmp_state = state_path.with_suffix(".tmp")
            _tmp_state.write_text(json.dumps(_state_now, ensure_ascii=False), encoding="utf-8")
            os.replace(_tmp_state, state_path)
        except Exception:
            pass

    # Do NOT echo updatedMCPToolOutput — we have no modifications to make,
    # and echoing the full result back wastes bandwidth on every tool call.
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": context_text,
        }
    }
    print(json.dumps(output))


if __name__ == "__main__":
    main()
