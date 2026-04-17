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

Intentionally avoids importing StateTracker to keep the
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

from scripts.policy_config import (
    default_state_root,
    default_hook_state_root,
    derive_session_id,
    truncate_jsonl_if_needed,
    sessions_root,
)  # noqa: E402
from scripts.span_tracker import is_read_only_tool  # noqa: E402

_MAX_DELTAS = 500
_SPAN_NUDGE_FLAG = "_span_nudge_sent"


def _maybe_span_nudge(tool_name: str, state_path: Path) -> str:
    """Return a nudge string the first time a non-trivial tool runs without a span.

    Writes a flag to state.json so the nudge fires at most once per session.
    Returns empty string if the nudge has already been sent or state is unreadable.
    """
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    except Exception:
        return ""
    if raw.get(_SPAN_NUDGE_FLAG):
        return ""
    # Mark sent before returning so concurrent calls don't double-fire
    raw[_SPAN_NUDGE_FLAG] = True
    try:
        tmp = state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        tmp.replace(state_path)
    except Exception:
        return ""
    short = _short_tool_name(tool_name)
    return (
        f"[Span nudge] You used `{short}` without an active span. "
        "Wrapping reusable tool sequences in a span turns them into zero-LLM pipelines. "
        "Example: icc_span_open(intent_signature='lark.read.get-doc') "
        f"→ {short}/other tools → icc_span_close(outcome='success'). "
        "Format: <connector>.(read|write).<name>"
    )


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
    session_dir = sessions_root(default_state_root()) / session_id
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
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
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
                import fcntl
                args_raw = json.dumps(tool_input, sort_keys=True, ensure_ascii=True)
                args_hash = hashlib.sha256(args_raw.encode()).hexdigest()[:16]
                action = {
                    "tool_name": tool_name,
                    "args_hash": args_hash,
                    "has_side_effects": not is_read_only_tool(tool_name),
                    "ts_ms": int(time.time() * 1000),
                }
                with (state_root / "active-span-actions.jsonl").open("a", encoding="utf-8") as f:
                    fcntl.flock(f, fcntl.LOCK_EX)
                    try:
                        f.write(json.dumps(action, ensure_ascii=False) + "\n")
                    finally:
                        fcntl.flock(f, fcntl.LOCK_UN)
            except Exception:
                pass

            # Delta with intent — makes in-span work visible in State tab
            _write_span_delta(tool_name, tool_input, state_path, active_span_intent)

        elif not is_read_only_tool(tool_name):
            # No active span + non-trivial tool: inject a one-shot nudge so CC
            # learns to open spans. Fire only once per session to avoid noise.
            nudge_text = _maybe_span_nudge(tool_name, state_path)
            if nudge_text:
                print(json.dumps({
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": nudge_text,
                    }
                }))
                return

    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse"}}))


if __name__ == "__main__":
    main()
