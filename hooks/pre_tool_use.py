from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Compiled once at module load — shared across all validator functions.
_SIG_RE = re.compile(r'^[a-z][a-z0-9_-]*\.(read|write)\.[a-z][a-z0-9_./-]*$')
_SAFE_SEG_RE = re.compile(r'^[a-z0-9][a-z0-9_-]*$')
_VAR_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


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
    _sig_normalized_from: str | None = None
    _sig_normalized_to: str | None = None

    if tool_name.endswith("__icc_exec"):
        mode = str(arguments.get("mode", "inline_code")).strip()
        _sig_raw = str(arguments.get("intent_signature", "")).strip()
        intent_signature = _sig_raw.lower()
        if intent_signature != _sig_raw:
            _sig_normalized_from = _sig_raw
            _sig_normalized_to = intent_signature
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
            # Check for common 2-part mistake (connector omitted)
            if len(intent_signature.split(".")) == 2:
                error_msg = (
                    f"icc_exec: intent_signature {intent_signature!r} has only 2 parts. "
                    "Required format: connector.mode.name (e.g. 'zwcad.read.layers'). "
                    "Add the connector name as the first part."
                )
            else:
                # Must be <connector>.(read|write).<name> — middle segment must be read or write
                if not _SIG_RE.match(intent_signature):
                    error_msg = (
                        f"icc_exec: intent_signature {intent_signature!r} is invalid. "
                        "Must be <connector>.(read|write).<name> — e.g. 'zwcad.read.state', "
                        "'hypermesh.write.apply-change'. Middle segment must be 'read' or 'write'. "
                        "Check connector://notes to see existing intents for this connector."
                    )
                else:
                    result_var = str(arguments.get("result_var", "")).strip()
                    if result_var:
                        if not _VAR_RE.match(result_var):
                            error_msg = (
                                f"icc_exec: result_var {result_var!r} is invalid. "
                                "Must be a Python identifier, e.g. '__result' or 'output_rows'."
                            )

    elif tool_name.endswith("__icc_reconcile"):
        delta_id = str(arguments.get("delta_id", "")).strip()
        outcome = str(arguments.get("outcome", "")).strip()
        if not delta_id:
            error_msg = "icc_reconcile: 'delta_id' is required"
        elif outcome not in ("confirm", "correct", "retract"):
            error_msg = f"icc_reconcile: 'outcome' must be confirm/correct/retract, got {outcome!r}"

    elif tool_name.endswith("__icc_crystallize"):
        _sig_raw = str(arguments.get("intent_signature", "")).strip()
        intent_signature = _sig_raw.lower()
        if intent_signature != _sig_raw:
            _sig_normalized_from = _sig_raw
            _sig_normalized_to = intent_signature
        connector = str(arguments.get("connector", "")).strip()
        pipeline_name = str(arguments.get("pipeline_name", "")).strip()
        mode = str(arguments.get("mode", "")).strip()
        if not intent_signature:
            error_msg = "icc_crystallize: 'intent_signature' is required"
        elif not connector:
            error_msg = "icc_crystallize: 'connector' is required"
        elif not _SAFE_SEG_RE.match(connector):
            error_msg = "icc_crystallize: 'connector' must be lowercase alphanumeric/underscore/dash, no path separators"
        elif not pipeline_name:
            error_msg = "icc_crystallize: 'pipeline_name' is required"
        elif ".." in pipeline_name or "/" in pipeline_name or "\\" in pipeline_name:
            error_msg = "icc_crystallize: 'pipeline_name' cannot contain '..', '/', or '\\'"
        elif mode not in ("read", "write"):
            error_msg = f"icc_crystallize: 'mode' must be read or write, got {mode!r}"

    elif tool_name.endswith("__icc_span_open"):
        _sig_raw = str(arguments.get("intent_signature", "")).strip()
        intent_signature = _sig_raw.lower()
        if intent_signature != _sig_raw:
            _sig_normalized_from = _sig_raw
            _sig_normalized_to = intent_signature
        if not intent_signature:
            error_msg = (
                "icc_span_open: 'intent_signature' is required "
                "(e.g. 'lark.read.get-doc'). "
                "Format: <connector>.(read|write).<name>"
            )
        elif not _SIG_RE.match(intent_signature):
            error_msg = (
                f"icc_span_open: intent_signature {intent_signature!r} is invalid. "
                "Must be <connector>.(read|write).<name> — e.g. 'lark.read.get-doc'."
            )

    elif tool_name.endswith("__icc_span_close"):
        outcome = str(arguments.get("outcome", "")).strip()
        if outcome not in ("success", "failure", "aborted"):
            error_msg = (
                f"icc_span_close: 'outcome' must be success/failure/aborted, got {outcome!r}"
            )

    elif tool_name.endswith("__icc_span_approve"):
        _sig_raw = str(arguments.get("intent_signature", "")).strip()
        intent_signature = _sig_raw.lower()
        if intent_signature != _sig_raw:
            _sig_normalized_from = _sig_raw
            _sig_normalized_to = intent_signature
        if not intent_signature:
            error_msg = "icc_span_approve: 'intent_signature' is required"
        elif not _SIG_RE.match(intent_signature):
            error_msg = (
                f"icc_span_approve: intent_signature {intent_signature!r} is invalid. "
                "Must be <connector>.(read|write).<name> — e.g. 'lark.read.get-doc'."
            )

    elif tool_name.endswith("__icc_goal_rollback"):
        target_event_id = str(arguments.get("target_event_id", "")).strip()
        if not target_event_id:
            error_msg = "icc_goal_rollback: 'target_event_id' is required"

    if error_msg:
        out = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": error_msg,
            },
            "systemMessage": f"Tool call blocked by emerge PreToolUse validator: {error_msg}",
        }
    elif tool_name.endswith("__icc_goal_rollback"):
        target_event_id = str(arguments.get("target_event_id", "")).strip()
        out = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
            },
            "systemMessage": (
                f"emerge: icc_goal_rollback to target_event_id={target_event_id!r}. "
                "This is irreversible — it will overwrite the active goal state. "
                "Confirm only if the user explicitly requested this rollback."
            ),
        }
    elif tool_name.endswith("__icc_span_approve"):
        out = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
            },
            "systemMessage": (
                "icc_span_approve 将把 span skeleton 移动到正式 pipeline 目录并激活自动化执行路径。"
                "请确认批准此操作？"
            ),
        }
    elif tool_name.endswith("__icc_hub") and isinstance(arguments, dict) and arguments.get("action") == "resolve":
        out = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
            },
            "systemMessage": (
                "icc_hub resolve 将应用冲突解决方案，此操作不可撤销。请确认继续？"
            ),
        }
    elif _sig_normalized_to is not None:
        out = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "updatedInput": {"intent_signature": _sig_normalized_to},
            },
            "systemMessage": (
                f"pre_tool_use: normalized intent_signature "
                f"from {_sig_normalized_from!r} to {_sig_normalized_to!r}"
            ),
        }
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
