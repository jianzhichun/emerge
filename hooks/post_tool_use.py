"""PostToolUse hook for emerge MCP tools (icc_exec/reconcile/crystallize/span/hub).

General CC tool calls (Bash, Read, Grep, etc.) are handled by tool_audit.py via
a separate PostToolUse matcher. This script only runs for emerge-specific tools.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.policy_config import default_hook_state_root  # noqa: E402
from scripts.span_tracker import is_read_only_tool  # noqa: E402
from scripts.state_tracker import (  # noqa: E402
    LEVEL_CORE_CRITICAL,
    LEVEL_CORE_SECONDARY,
    LEVEL_PERIPHERAL,
    load_tracker,
    save_tracker,
)

_CRITICAL_TOOLS = frozenset({"icc_exec", "icc_span_open", "icc_span_close"})
_SECONDARY_TOOLS = frozenset({"icc_reconcile", "icc_crystallize", "icc_span_approve"})


def _classify_level(tool_name: str) -> str:
    short = _short_tool_name(tool_name)
    if short in _CRITICAL_TOOLS:
        return LEVEL_CORE_CRITICAL
    if short in _SECONDARY_TOOLS:
        return LEVEL_CORE_SECONDARY
    return LEVEL_PERIPHERAL


def _short_tool_name(tool_name: str) -> str:
    """Strip plugin prefix: mcp__plugin_emerge__icc_exec → icc_exec"""
    return tool_name.rsplit("__", 1)[-1] if "__" in tool_name else tool_name


def _build_delta_message(tool_name: str, tool_input: dict, payload: dict) -> str:
    """Build a human-readable delta message that includes key context."""
    if payload.get("delta_message"):
        return str(payload["delta_message"])
    short = _short_tool_name(tool_name)
    intent = str(tool_input.get("intent_signature", "")).strip()
    if intent:
        return f"{short}: {intent}"
    for key in ("pipeline_key", "delta_id", "description"):
        val = str(tool_input.get(key, "")).strip()
        if val:
            return f"{short}: {val[:80]}"
    return short


def _build_args_summary(tool_input: dict) -> str:
    """Compact key=value summary for Cockpit detail view (not injected into prompts)."""
    if not tool_input:
        return ""
    parts = []
    for key in ("intent_signature", "pipeline_key", "delta_id", "outcome",
                "description", "script_ref", "base_pipeline_id"):
        val = str(tool_input.get(key, "")).strip()
        if val:
            parts.append(f"{key}={val[:60]}")
        if len(parts) >= 3:
            break
    return ", ".join(parts)


_ICC_TOOL_SUFFIXES = frozenset({
    "icc_exec", "icc_span_open", "icc_span_close", "icc_crystallize",
    "icc_reconcile", "icc_span_approve", "icc_hub", "icc_compose",
    "runner_notify",
})


def _is_icc_tool(tool_name: str) -> bool:
    return _short_tool_name(tool_name) in _ICC_TOOL_SUFFIXES


def _build_args_snapshot(tool_input: dict, tool_name: str) -> dict:
    """Capture key intent/pipeline args for ICC tools. Capped at 2 KB."""
    if not _is_icc_tool(tool_name):
        return {}
    snapshot: dict = {}
    for key in ("intent_signature", "pipeline", "connector", "outcome", "span_id", "mode"):
        val = tool_input.get(key)
        if val is not None:
            snapshot[key] = val
    if len(json.dumps(snapshot, ensure_ascii=False)) > 2048:
        snapshot = {
            "intent_signature": snapshot.get("intent_signature", ""),
            "_truncated": True,
        }
    return snapshot


def _build_result_summary(raw_result: dict, tool_name: str) -> dict:
    """Summarize top-level result keys for ICC tools (enough for LLM crystallization)."""
    if not _is_icc_tool(tool_name):
        return {}
    inner: dict = {}
    try:
        content = raw_result.get("content", [])
        if content and isinstance(content[0], dict):
            inner = json.loads(content[0].get("text", "{}"))
    except Exception:
        pass
    if not isinstance(inner, dict):
        inner = {}
    summary: dict = {}
    for key, val in inner.items():
        if isinstance(val, (str, int, float, bool)):
            summary[key] = str(val)[:200]
        elif key == "rows" and isinstance(val, list):
            summary["rows_count"] = len(val)
            if val and isinstance(val[0], dict):
                summary["row_keys"] = list(val[0].keys())[:5]
    return summary


def _record_span_action(
    tool_name: str, payload: dict, state_root: Path, state_path: Path
) -> tuple[str, str]:
    """Record tool invocation to the active span WAL.

    Returns (active_span_id, active_span_intent) — both empty strings when no span is active.
    ICC tools (icc_exec, icc_span_open/close, etc.) are enriched with args_snapshot and
    result_summary so the crystallizer has structured intent context alongside the WAL code.
    Non-ICC tools (Read, Bash, Grep, etc.) retain the hash-only format.
    """
    if not tool_name:
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
    if _is_icc_tool(tool_name):
        _snap = _build_args_snapshot(payload.get("tool_input", {}), tool_name)
        if _snap:
            _action["args_snapshot"] = _snap
        _raw_resp = payload.get("tool_response", payload.get("tool_result", {}))
        _resp = _raw_resp if isinstance(_raw_resp, dict) else {}
        _rsum = _build_result_summary(_resp, tool_name)
        if _rsum:
            _action["result_summary"] = _rsum
    _buf = state_root / "active-span-actions.jsonl"
    try:
        with _buf.open("a", encoding="utf-8") as _f:
            fcntl.flock(_f, fcntl.LOCK_EX)
            try:
                _f.write(json.dumps(_action, ensure_ascii=False) + "\n")
            finally:
                fcntl.flock(_f, fcntl.LOCK_UN)
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
    raw_result = payload.get("tool_response", payload.get("tool_result", {}))
    result = raw_result if isinstance(raw_result, dict) else {}
    state_root = Path(default_hook_state_root())
    state_path = state_root / "state.json"
    tracker = load_tracker(state_path)

    tool_input = payload.get("tool_input", {}) or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    intent_signature = str(tool_input.get("intent_signature", "")).strip() or None
    message = _build_delta_message(tool_name, tool_input, payload)
    level = _classify_level(tool_name)
    provisional = bool(payload.get("provisional", False))

    # verification_state lives inside content[0]["text"] (serialized JSON), not in the
    # outer MCP wrapper. Parse it out; fall back to "verified" if absent or unparseable.
    verification_state = "verified"
    inner = {}
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
        args_summary=_build_args_summary(tool_input),
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
    context_text = tracker.format_additional_context(budget_chars=budget_chars)

    # Detect span skeleton ready — inject reminder so CC reviews it
    _short = _short_tool_name(tool_name)
    if _short == "icc_span_close" and isinstance(inner, dict) and inner.get("skeleton_path"):
        sk_path = inner["skeleton_path"]
        context_text = (
            f"[Span] Pipeline skeleton ready at {sk_path}. "
            "Review and call icc_span_approve to activate the bridge.\n\n"
            + context_text
        )

    _active_span_id, _active_span_intent = _record_span_action(
        tool_name, payload, state_root, state_path
    )

    save_tracker(state_path, tracker)

    hook_specific: dict = {
        "hookEventName": "PostToolUse",
        "additionalContext": context_text,
    }

    # For icc_exec with an active span: inject _span_id/_span_intent into
    # structuredContent so CC can correlate the exec result with the flywheel
    # span without a separate state read.
    if _short == "icc_exec" and _active_span_id:
        _tool_resp = payload.get("tool_response") or {}
        _sc = dict(_tool_resp.get("structuredContent") or {})
        _sc["_span_id"] = _active_span_id
        _sc["_span_intent"] = _active_span_intent
        _updated_resp = dict(_tool_resp)
        _updated_resp["structuredContent"] = _sc
        hook_specific["updatedMCPToolOutput"] = _updated_resp

    output = {"hookSpecificOutput": hook_specific}
    print(json.dumps(output))


if __name__ == "__main__":
    main()
