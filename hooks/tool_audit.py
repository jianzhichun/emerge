"""Lightweight PostToolUse hook for non-emerge CC tools.

Fires for every Bash, Read, Grep, Glob, Edit, Write, Agent, and third-party MCP
tool call.

Behaviour:
- Always: append to session-scoped tool-events.jsonl (Audit tab)
- When a span is active: ALSO append a delta to state.json with intent_signature
  from the active span. This gives every in-span tool call "muscle memory" —
  visible in State tab, linked to the span's intent cluster, usable for
  crystallization review.
- Always (when span active): append to active-span-actions.jsonl (span WAL)

Intentionally avoids importing GoalControlPlane / StateTracker to keep the
no-span path lightweight. The span-delta path does a single raw JSON read+write.
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

from scripts.policy_config import default_exec_root, default_hook_state_root, derive_session_id, truncate_jsonl_if_needed  # noqa: E402
from scripts.span_tracker import is_read_only_tool  # noqa: E402

_MAX_DELTAS = 500


def _args_summary(tool_input: dict) -> str:
    if not tool_input:
        return ""
    for key in ("file_path", "pattern", "command", "query", "path", "old_string", "content"):
        if key in tool_input:
            val = str(tool_input[key])
            return val[:120] if len(val) > 120 else val
    for k, v in tool_input.items():
        return f"{k}: {str(v)[:80]}"
    return ""


def _short_tool_name(tool_name: str) -> str:
    return tool_name.rsplit("__", 1)[-1] if "__" in tool_name else tool_name


def _write_span_delta(
    tool_name: str,
    tool_input: dict,
    state_path: Path,
    active_span_intent: str,
) -> None:
    """Append a peripheral delta to state.json for an in-span tool call.

    Uses raw JSON manipulation (no StateTracker import) to keep overhead low.
    Preserves all existing fields in state.json (including active_span_id).
    """
    short = _short_tool_name(tool_name)
    args = _args_summary(tool_input)
    message = f"{short}: {args}" if args else short

    try:
        raw = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        deltas = list(raw.get("deltas", []))
        ts_ms = int(time.time() * 1000)
        entry: dict = {
            "id": f"d-{ts_ms}-{len(deltas)}",
            "message": message,
            "level": "peripheral",
            "verification_state": "verified",
            "provisional": False,
            "intent_signature": active_span_intent or None,
            "tool_name": tool_name,
            "ts_ms": ts_ms,
        }
        if args:
            entry["args_summary"] = args[:200]
        deltas.append(entry)
        if len(deltas) > _MAX_DELTAS:
            deltas = deltas[-_MAX_DELTAS:]
        raw["deltas"] = deltas
        tmp = state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        tmp.replace(state_path)
    except Exception:
        pass


def main() -> None:
    payload_text = sys.stdin.read().strip()
    try:
        payload = json.loads(payload_text) if payload_text else {}
    except Exception:
        payload = {}

    tool_name = payload.get("tool_name", "")
    if not tool_name:
        print(json.dumps({}))
        return

    tool_input = payload.get("tool_input", {}) or {}
    if not isinstance(tool_input, dict):
        tool_input = {}

    # ── Always: write tool-event to session-scoped file (Audit tab) ───────
    session_id = derive_session_id(os.environ.get("EMERGE_SESSION_ID"), Path.cwd())
    session_dir = default_exec_root() / session_id
    event = {
        "tool_name": tool_name,
        "ts_ms": int(time.time() * 1000),
        "args_summary": _args_summary(tool_input),
        "has_side_effects": not is_read_only_tool(tool_name),
    }
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
        events_path = session_dir / "tool-events.jsonl"
        with events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=True) + "\n")
        truncate_jsonl_if_needed(events_path, max_lines=5_000)
    except Exception:
        pass

    # ── When span active: record span action + write delta with intent ────
    # Skip icc_exec: its code paths are captured in ExecSession WAL.
    if not tool_name.endswith("__icc_exec"):
        state_root = Path(default_hook_state_root())
        state_path = state_root / "state.json"
        try:
            raw_state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
            active_span_id = str(raw_state.get("active_span_id", "") or "")
            active_span_intent = str(raw_state.get("active_span_intent", "") or "")
        except Exception:
            active_span_id = ""
            active_span_intent = ""

        if active_span_id:
            # Span action WAL (used for span close + crystallization)
            try:
                args_raw = json.dumps(tool_input, sort_keys=True, ensure_ascii=True)
                args_hash = hashlib.sha256(args_raw.encode()).hexdigest()[:16]
                action = {
                    "tool_name": tool_name,
                    "args_hash": args_hash,
                    "has_side_effects": not is_read_only_tool(tool_name),
                    "ts_ms": int(time.time() * 1000),
                }
                with (state_root / "active-span-actions.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps(action, ensure_ascii=True) + "\n")
            except Exception:
                pass

            # Delta with intent — makes in-span work visible in State tab
            _write_span_delta(tool_name, tool_input, state_path, active_span_intent)

    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse"}}))


if __name__ == "__main__":
    main()
