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
        intent_signature = str(arguments.get("intent_signature", "")).strip()
        if mode not in ("inline_code", "script_ref"):
            error_msg = f"icc_exec: 'mode' must be inline_code or script_ref, got {mode!r}"
        elif mode == "inline_code" and not str(arguments.get("code", "")).strip():
            error_msg = "icc_exec (mode=inline_code): 'code' argument is required"
        elif mode == "script_ref" and not str(arguments.get("script_ref", "")).strip():
            error_msg = "icc_exec (mode=script_ref): 'script_ref' argument is required"
        elif not intent_signature:
            error_msg = (
                "icc_exec: 'intent_signature' is required (e.g. 'zwcad.read.state'). "
                "Read tasks must set __result=[{...}] in code. "
                "Write tasks must set __action={'ok': True, ...} in code. "
                "Side-effectful calls (COM, file writes, network) must use no_replay=True. "
                "State setup calls (imports, object creation) must NOT use no_replay."
            )
        else:
            import re as _re
            # Must be <connector>.(read|write).<name> — middle segment must be read or write
            _sig_pattern = _re.compile(r'^[a-z][a-z0-9_-]*\.(read|write)\.[a-z][a-z0-9_./-]*$')
            if not _sig_pattern.match(intent_signature):
                error_msg = (
                    f"icc_exec: intent_signature {intent_signature!r} is invalid. "
                    "Must be <connector>.(read|write).<name> — e.g. 'zwcad.read.state', "
                    "'hypermesh.write.apply-change'. Middle segment must be 'read' or 'write'. "
                    "Check connector://notes to see existing intents for this connector."
                )

    if tool_name.endswith("__icc_reconcile"):
        delta_id = str(arguments.get("delta_id", "")).strip()
        outcome = str(arguments.get("outcome", "")).strip()
        if not delta_id:
            error_msg = "icc_reconcile: 'delta_id' is required"
        elif outcome not in ("confirm", "correct", "retract"):
            error_msg = f"icc_reconcile: 'outcome' must be confirm/correct/retract, got {outcome!r}"

    if tool_name.endswith("__icc_crystallize"):
        intent_signature = str(arguments.get("intent_signature", "")).strip()
        connector = str(arguments.get("connector", "")).strip()
        pipeline_name = str(arguments.get("pipeline_name", "")).strip()
        mode = str(arguments.get("mode", "")).strip()
        _safe_seg = __import__("re").compile(r"^[a-z0-9][a-z0-9_-]*$")
        if not intent_signature:
            error_msg = "icc_crystallize: 'intent_signature' is required"
        elif not connector:
            error_msg = "icc_crystallize: 'connector' is required"
        elif not _safe_seg.match(connector):
            error_msg = "icc_crystallize: 'connector' must be lowercase alphanumeric/underscore/dash, no path separators"
        elif not pipeline_name:
            error_msg = "icc_crystallize: 'pipeline_name' is required"
        elif ".." in pipeline_name or "/" in pipeline_name or "\\" in pipeline_name:
            error_msg = "icc_crystallize: 'pipeline_name' cannot contain '..', '/', or '\\'"
        elif mode not in ("read", "write"):
            error_msg = f"icc_crystallize: 'mode' must be read or write, got {mode!r}"

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
