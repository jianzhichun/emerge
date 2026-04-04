from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> None:
    payload_text = sys.stdin.read().strip()
    try:
        payload = json.loads(payload_text) if payload_text else {}
    except Exception:
        payload = {}

    tool_name = payload.get("tool_name", "")
    arguments = payload.get("tool_input", {}) or {}
    if not isinstance(arguments, dict):
        arguments = {}

    # Validation rules per tool
    error_msg: str | None = None

    if tool_name.endswith("__icc_read") or tool_name.endswith("__icc_write"):
        connector = str(arguments.get("connector", "")).strip()
        pipeline = str(arguments.get("pipeline", "")).strip()
        if not connector:
            error_msg = "icc_read/icc_write: 'connector' argument is required"
        elif not pipeline:
            error_msg = "icc_read/icc_write: 'pipeline' argument is required"

    if tool_name.endswith("__icc_exec"):
        mode = str(arguments.get("mode", "inline_code")).strip()
        if mode not in ("inline_code", "script_ref"):
            error_msg = f"icc_exec: 'mode' must be inline_code or script_ref, got {mode!r}"
        elif mode == "inline_code" and not str(arguments.get("code", "")).strip():
            error_msg = "icc_exec (mode=inline_code): 'code' argument is required"
        elif mode == "script_ref" and not str(arguments.get("script_ref", "")).strip():
            error_msg = "icc_exec (mode=script_ref): 'script_ref' argument is required"

    if tool_name.endswith("__icc_reconcile"):
        delta_id = str(arguments.get("delta_id", "")).strip()
        outcome = str(arguments.get("outcome", "")).strip()
        if not delta_id:
            error_msg = "icc_reconcile: 'delta_id' is required"
        elif outcome not in ("confirm", "correct", "retract"):
            error_msg = f"icc_reconcile: 'outcome' must be confirm/correct/retract, got {outcome!r}"

    if error_msg:
        # Return a block decision to reject the tool call
        out = {"decision": "block", "reason": error_msg}
    else:
        out = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": f"pre_tool_use: {tool_name} approved",
            }
        }
    print(json.dumps(out))


if __name__ == "__main__":
    main()
