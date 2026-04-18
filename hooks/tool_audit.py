"""Lightweight PostToolUse hook for non-emerge CC tools.

Fires for every Bash, Read, Grep, Glob, Edit, Write, Agent, and third-party MCP
tool call.

Behaviour:
- Always: append to session-scoped tool-events.jsonl (Audit tab)
- When a span is active: ALSO append a peripheral delta to tool-deltas.jsonl
  (concurrent-safe, fcntl LOCK_EX) — visible in State tab via state://deltas
  merge, linked to the span's intent cluster, usable for crystallization review.
- Always (when span active): append to active-span-actions.jsonl (span WAL)
- No span, first non-trivial tool: inject one-shot span nudge via flag file.

state.json is only READ here (to check active_span_id). All writes go to
separate files so there is no TOCTOU race with StateTracker.save_state().
"""
from __future__ import annotations

import fcntl
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

_MAX_TOOL_DELTAS = 500


def _maybe_span_nudge(tool_name: str, state_dir: Path) -> str:
    """Return nudge text the first time a non-trivial tool runs without an active span.

    Uses an atomic flag file (exclusive create) — no lock needed, no state.json
    modification.  Returns "" if the nudge was already sent this session.
    """
    flag = state_dir / "span-nudge-sent"
    try:
        flag.open("x").close()
    except FileExistsError:
        return ""
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
    state_dir: Path,
    active_span_intent: str,
) -> None:
    """Append a peripheral delta to tool-deltas.jsonl for an in-span tool call.

    Uses fcntl LOCK_EX so concurrent hook processes don't interleave writes.
    Does NOT touch state.json — no TOCTOU race with StateTracker.
    The state://deltas resource merges this file at read time.
    """
    short = _short_tool_name(tool_name)
    args = _args_summary(tool_input)
    message = f"{short}: {args}" if args else short
    ts_ms = int(time.time() * 1000)
    entry: dict = {
        "id": f"d-{ts_ms}-{short}",
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
    try:
        deltas_path = state_dir / "tool-deltas.jsonl"
        with deltas_path.open("a", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        truncate_jsonl_if_needed(deltas_path, max_lines=_MAX_TOOL_DELTAS)
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
        # Read-only access to state.json — no write, no race.
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
                    fcntl.flock(f, fcntl.LOCK_EX)
                    try:
                        f.write(json.dumps(action, ensure_ascii=False) + "\n")
                    finally:
                        fcntl.flock(f, fcntl.LOCK_UN)
            except Exception:
                pass

            # Peripheral delta — goes to tool-deltas.jsonl, NOT state.json
            _write_span_delta(tool_name, tool_input, state_root, active_span_intent)

        elif not is_read_only_tool(tool_name):
            # No active span + non-trivial tool: inject a one-shot nudge so CC
            # learns to open spans. Fire only once per session to avoid noise.
            nudge_text = _maybe_span_nudge(tool_name, state_root)
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
