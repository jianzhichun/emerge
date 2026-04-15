from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Compiled once at module load — shared across all validator functions.
_SIG_RE = re.compile(r'^[a-z][a-z0-9_-]*\.(read|write)\.[a-z][a-z0-9_./-]*$')
_SAFE_SEG_RE = re.compile(r'^[a-z0-9][a-z0-9_-]*$')
_VAR_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def _normalize_sig(raw: str) -> tuple[str, str | None, str | None]:
    """Lowercase-normalize intent_signature.

    Returns (normalized, from_raw, to_norm).
    from_raw and to_norm are None if no change was needed.
    """
    normalized = raw.lower()
    if normalized != raw:
        return normalized, raw, normalized
    return raw, None, None


def _validate_icc_exec(args: dict, sig: str) -> str | None:
    mode = str(args.get("mode", "inline_code")).strip()
    if mode not in ("inline_code", "script_ref"):
        return f"icc_exec: 'mode' must be inline_code or script_ref, got {mode!r}"
    if mode == "inline_code" and not str(args.get("code", "")).strip():
        return "icc_exec (mode=inline_code): 'code' argument is required"
    if mode == "script_ref" and not str(args.get("script_ref", "")).strip():
        return "icc_exec (mode=script_ref): 'script_ref' argument is required"
    if not sig:
        return (
            "icc_exec: 'intent_signature' is required (e.g. 'zwcad.read.state'). "
            "Read tasks must set __result=[{...}] in code. "
            "Write tasks must set __action={'ok': True, ...} in code. "
            "Side-effectful calls (COM, file writes, network) must use no_replay=True. "
            "State setup calls (imports, object creation) must NOT use no_replay."
        )
    if len(sig.split(".")) == 2:
        return (
            f"icc_exec: intent_signature {sig!r} has only 2 parts. "
            "Required format: connector.mode.name (e.g. 'zwcad.read.layers'). "
            "Add the connector name as the first part."
        )
    if not _SIG_RE.match(sig):
        return (
            f"icc_exec: intent_signature {sig!r} is invalid. "
            "Must be <connector>.(read|write).<name> — e.g. 'zwcad.read.state', "
            "'hypermesh.write.apply-change'. Middle segment must be 'read' or 'write'. "
            "Check connector://notes to see existing intents for this connector."
        )
    result_var = str(args.get("result_var", "")).strip()
    if result_var and not _VAR_RE.match(result_var):
        return (
            f"icc_exec: result_var {result_var!r} is invalid. "
            "Must be a Python identifier, e.g. '__result' or 'output_rows'."
        )
    return None


def _validate_icc_reconcile(args: dict) -> str | None:
    delta_id = str(args.get("delta_id", "")).strip()
    outcome = str(args.get("outcome", "")).strip()
    if not delta_id:
        return "icc_reconcile: 'delta_id' is required"
    if outcome not in ("confirm", "correct", "retract"):
        return f"icc_reconcile: 'outcome' must be confirm/correct/retract, got {outcome!r}"
    return None


def _validate_icc_crystallize(args: dict, sig: str) -> str | None:
    connector = str(args.get("connector", "")).strip()
    pipeline_name = str(args.get("pipeline_name", "")).strip()
    mode = str(args.get("mode", "")).strip()
    if not sig:
        return "icc_crystallize: 'intent_signature' is required"
    if not connector:
        return "icc_crystallize: 'connector' is required"
    if not _SAFE_SEG_RE.match(connector):
        return "icc_crystallize: 'connector' must be lowercase alphanumeric/underscore/dash, no path separators"
    if not pipeline_name:
        return "icc_crystallize: 'pipeline_name' is required"
    if ".." in pipeline_name or "/" in pipeline_name or "\\" in pipeline_name:
        return "icc_crystallize: 'pipeline_name' cannot contain '..', '/', or '\\'"
    if mode not in ("read", "write"):
        return f"icc_crystallize: 'mode' must be read or write, got {mode!r}"
    return None


def _validate_icc_span_open(args: dict, sig: str) -> str | None:
    if not sig:
        return (
            "icc_span_open: 'intent_signature' is required "
            "(e.g. 'lark.read.get-doc'). "
            "Format: <connector>.(read|write).<name>"
        )
    if not _SIG_RE.match(sig):
        return (
            f"icc_span_open: intent_signature {sig!r} is invalid. "
            "Must be <connector>.(read|write).<name> — e.g. 'lark.read.get-doc'."
        )
    return None


def _validate_icc_span_close(args: dict) -> str | None:
    outcome = str(args.get("outcome", "")).strip()
    if outcome not in ("success", "failure", "aborted"):
        return f"icc_span_close: 'outcome' must be success/failure/aborted, got {outcome!r}"
    return None


def _validate_icc_span_approve(args: dict, sig: str) -> str | None:
    if not sig:
        return "icc_span_approve: 'intent_signature' is required"
    if not _SIG_RE.match(sig):
        return (
            f"icc_span_approve: intent_signature {sig!r} is invalid. "
            "Must be <connector>.(read|write).<name> — e.g. 'lark.read.get-doc'."
        )
    return None


def _validate_icc_goal_rollback(args: dict) -> str | None:
    if not str(args.get("target_event_id", "")).strip():
        return "icc_goal_rollback: 'target_event_id' is required"
    return None


# Tools whose intent_signature must be normalized and validated.
_SIG_TOOLS: frozenset[str] = frozenset({
    "__icc_exec", "__icc_crystallize", "__icc_span_open", "__icc_span_approve",
})

_SIG_VALIDATORS: dict[str, Callable[[dict, str], str | None]] = {
    "__icc_exec":         _validate_icc_exec,
    "__icc_crystallize":  _validate_icc_crystallize,
    "__icc_span_open":    _validate_icc_span_open,
    "__icc_span_approve": _validate_icc_span_approve,
}
_PLAIN_VALIDATORS: dict[str, Callable[[dict], str | None]] = {
    "__icc_reconcile":     _validate_icc_reconcile,
    "__icc_span_close":    _validate_icc_span_close,
    "__icc_goal_rollback": _validate_icc_goal_rollback,
}


def _build_output(
    tool_name: str,
    suffix: str,
    arguments: dict,
    sig: str,
    sig_from: str | None,
    sig_to: str | None,
    error_msg: str | None,
) -> dict:
    """Build the hook JSON output given validation results."""
    if error_msg:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": error_msg,
            },
            "systemMessage": f"Tool call blocked by emerge PreToolUse validator: {error_msg}",
        }
    if suffix == "__icc_goal_rollback":
        target_event_id = str(arguments.get("target_event_id", "")).strip()
        return {
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
    if suffix == "__icc_span_approve":
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
            },
            "systemMessage": (
                "icc_span_approve 将把 span skeleton 移动到正式 pipeline 目录并激活自动化执行路径。"
                "请确认批准此操作？"
            ),
        }
    if suffix == "__icc_hub" and arguments.get("action") == "resolve":
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
            },
            "systemMessage": "icc_hub resolve 将应用冲突解决方案，此操作不可撤销。请确认继续？",
        }
    if sig_to is not None:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "updatedInput": {"intent_signature": sig_to},
            },
            "systemMessage": (
                f"pre_tool_use: normalized intent_signature "
                f"from {sig_from!r} to {sig_to!r}"
            ),
        }
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": f"pre_tool_use: {tool_name} approved",
        }
    }


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

    suffix = f"__{tool_name.rsplit('__', 1)[-1]}" if "__" in tool_name else ""

    sig = ""
    sig_from: str | None = None
    sig_to: str | None = None
    if suffix in _SIG_TOOLS:
        raw_sig = str(arguments.get("intent_signature", "")).strip()
        sig, sig_from, sig_to = _normalize_sig(raw_sig)

    error_msg: str | None = None
    if suffix in _SIG_VALIDATORS:
        error_msg = _SIG_VALIDATORS[suffix](arguments, sig)
    elif suffix in _PLAIN_VALIDATORS:
        error_msg = _PLAIN_VALIDATORS[suffix](arguments)

    if error_msg is not None:
        sig_to = None

    print(json.dumps(_build_output(tool_name, suffix, arguments, sig, sig_from, sig_to, error_msg)))


if __name__ == "__main__":
    main()
