"""Lightweight PostToolUse hook for non-emerge CC tools.

Fires for every Bash, Read, Grep, Glob, Edit, Write, Agent, and third-party MCP
tool call. Records a minimal tool-event to the session-scoped tool-events.jsonl
(read by the Cockpit Audit tab) and, when a span is active, appends to the span
action WAL.

Intentionally avoids importing GoalControlPlane / StateTracker — those are only
needed for emerge-specific icc_* tools and carry non-trivial I/O overhead.
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

from scripts.policy_config import default_exec_root, default_hook_state_root, derive_session_id  # noqa: E402
from scripts.span_tracker import is_read_only_tool  # noqa: E402


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

    # ── Write tool-event to session-scoped file ────────────────────────────
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
        with (session_dir / "tool-events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=True) + "\n")
    except Exception:
        pass

    # ── Record span action if a span is active ─────────────────────────────
    # Skip icc_exec: its code paths are captured in ExecSession WAL.
    if not tool_name.endswith("__icc_exec"):
        state_root = Path(default_hook_state_root())
        state_path = state_root / "state.json"
        try:
            raw_state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
            active_span_id = str(raw_state.get("active_span_id", "") or "")
            if active_span_id:
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

    print(json.dumps({}))


if __name__ == "__main__":
    main()
