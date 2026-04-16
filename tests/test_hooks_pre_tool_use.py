"""Tests for pre_tool_use.py hook output format."""
from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run_hook(payload: dict) -> dict:
    result = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "pre_tool_use.py")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_block_uses_permission_decision_format():
    """Blocking output must use permissionDecision, not legacy 'decision: block'."""
    payload = {
        "tool_name": "mcp__plugin_emerge_emerge__icc_exec",
        "tool_input": {"mode": "inline_code", "code": "x=1"},
        # missing intent_signature → triggers block
    }
    out = _run_hook(payload)
    assert "decision" not in out, "legacy 'decision' key must not be used"
    assert "hookSpecificOutput" in out
    hook_out = out["hookSpecificOutput"]
    assert hook_out.get("hookEventName") == "PreToolUse"
    assert hook_out.get("permissionDecision") == "deny"
    assert "permissionDecisionReason" in hook_out
    assert "systemMessage" in out
    assert len(out["systemMessage"]) > 0


def test_approve_format_unchanged():
    """Successful calls still use additionalContext format."""
    payload = {
        "tool_name": "mcp__plugin_emerge_emerge__icc_exec",
        "tool_input": {
            "mode": "inline_code",
            "code": "x=1",
            "intent_signature": "zwcad.read.state",
        },
    }
    out = _run_hook(payload)
    assert "hookSpecificOutput" in out
    hook_out = out["hookSpecificOutput"]
    assert hook_out.get("hookEventName") == "PreToolUse"
    assert "additionalContext" in hook_out


def test_icc_reconcile_block_uses_permission_decision():
    """Reconcile validation errors also use permissionDecision format."""
    payload = {
        "tool_name": "mcp__plugin_emerge_emerge__icc_reconcile",
        "tool_input": {"delta_id": "", "outcome": "confirm"},
    }
    out = _run_hook(payload)
    assert "decision" not in out
    hook_out = out["hookSpecificOutput"]
    assert hook_out.get("permissionDecision") == "deny"
    assert "delta_id" in hook_out["permissionDecisionReason"].lower()


def test_intent_signature_uppercase_normalized():
    """Uppercase intent_signature is auto-normalized via updatedInput, not blocked."""
    payload = {
        "tool_name": "mcp__plugin_emerge_emerge__icc_exec",
        "tool_input": {
            "mode": "inline_code",
            "code": "x=1",
            "intent_signature": "ZWCAD.READ.State",
        },
    }
    out = _run_hook(payload)
    hook_out = out.get("hookSpecificOutput", {})
    assert hook_out.get("permissionDecision") != "deny", \
        f"Should normalize not block uppercase sig, got: {out}"
    assert "updatedInput" in hook_out, f"Expected updatedInput, got: {hook_out}"
    assert hook_out["updatedInput"]["intent_signature"] == "zwcad.read.state"


def test_intent_signature_mixed_case_normalized():
    """Mixed case intent_signature is auto-normalized for icc_span_open."""
    payload = {
        "tool_name": "mcp__plugin_emerge_emerge__icc_span_open",
        "tool_input": {"intent_signature": "Lark.Read.Get-Doc"},
    }
    out = _run_hook(payload)
    hook_out = out.get("hookSpecificOutput", {})
    assert hook_out.get("permissionDecision") != "deny"
    assert hook_out.get("updatedInput", {}).get("intent_signature") == "lark.read.get-doc"


def test_intent_signature_already_lowercase_no_updated_input():
    """Already-lowercase intent_signature must NOT produce updatedInput."""
    payload = {
        "tool_name": "mcp__plugin_emerge_emerge__icc_exec",
        "tool_input": {
            "mode": "inline_code",
            "code": "x=1",
            "intent_signature": "zwcad.read.state",
        },
    }
    out = _run_hook(payload)
    hook_out = out.get("hookSpecificOutput", {})
    assert hook_out.get("permissionDecision") != "deny"
    assert "updatedInput" not in hook_out, "No updatedInput needed when already correct"


def test_intent_signature_uppercase_invalid_structure_still_blocks():
    """Uppercased sig with wrong structure (2 parts) still blocks."""
    payload = {
        "tool_name": "mcp__plugin_emerge_emerge__icc_exec",
        "tool_input": {
            "mode": "inline_code",
            "code": "x=1",
            "intent_signature": "ZWCAD.STATE",  # only 2 parts even after lowercase
        },
    }
    out = _run_hook(payload)
    hook_out = out.get("hookSpecificOutput", {})
    assert hook_out.get("permissionDecision") == "deny"


def test_icc_span_approve_invalid_intent_signature_blocks():
    """icc_span_approve must enforce connector.read|write.name format."""
    payload = {
        "tool_name": "mcp__plugin_emerge_emerge__icc_span_approve",
        "tool_input": {"intent_signature": "lark.read"},
    }
    out = _run_hook(payload)
    hook_out = out.get("hookSpecificOutput", {})
    assert hook_out.get("permissionDecision") == "deny"
    reason = hook_out.get("permissionDecisionReason", "")
    assert "icc_span_approve" in reason
    assert "invalid" in reason


# ---------------------------------------------------------------------------
# Unit tests for extracted per-tool validator functions
# ---------------------------------------------------------------------------

def test_validate_icc_exec_valid():
    from hooks.pre_tool_use import _validate_icc_exec
    assert _validate_icc_exec({"mode": "inline_code", "code": "x=1"}, "zwcad.read.state") is None

def test_validate_icc_exec_missing_code():
    from hooks.pre_tool_use import _validate_icc_exec
    err = _validate_icc_exec({"mode": "inline_code", "code": ""}, "zwcad.read.state")
    assert err is not None and "code" in err

def test_validate_icc_exec_missing_sig():
    from hooks.pre_tool_use import _validate_icc_exec
    err = _validate_icc_exec({"mode": "inline_code", "code": "x=1"}, "")
    assert err is not None and "intent_signature" in err

def test_validate_icc_exec_two_part_sig():
    from hooks.pre_tool_use import _validate_icc_exec
    err = _validate_icc_exec({"mode": "inline_code", "code": "x=1"}, "zwcad.state")
    assert err is not None and "2 parts" in err

def test_validate_icc_exec_invalid_mode():
    from hooks.pre_tool_use import _validate_icc_exec
    err = _validate_icc_exec({"mode": "bad_mode", "code": "x=1"}, "zwcad.read.state")
    assert err is not None and "mode" in err

def test_validate_icc_exec_invalid_result_var():
    from hooks.pre_tool_use import _validate_icc_exec
    err = _validate_icc_exec({"mode": "inline_code", "code": "x=1", "result_var": "123bad"}, "zwcad.read.state")
    assert err is not None and "result_var" in err

def test_validate_icc_exec_valid_script_ref():
    from hooks.pre_tool_use import _validate_icc_exec
    assert _validate_icc_exec({"mode": "script_ref", "script_ref": "my_script.py"}, "zwcad.read.state") is None

def test_validate_icc_reconcile_valid():
    from hooks.pre_tool_use import _validate_icc_reconcile
    assert _validate_icc_reconcile({"delta_id": "d-1", "outcome": "confirm"}) is None

def test_validate_icc_reconcile_missing_delta_id():
    from hooks.pre_tool_use import _validate_icc_reconcile
    err = _validate_icc_reconcile({"delta_id": "", "outcome": "confirm"})
    assert err is not None and "delta_id" in err

def test_validate_icc_reconcile_bad_outcome():
    from hooks.pre_tool_use import _validate_icc_reconcile
    err = _validate_icc_reconcile({"delta_id": "d-1", "outcome": "wrong"})
    assert err is not None and "outcome" in err

def test_validate_icc_crystallize_valid():
    from hooks.pre_tool_use import _validate_icc_crystallize
    assert _validate_icc_crystallize(
        {"connector": "zwcad", "pipeline_name": "my-pipe", "mode": "read"},
        "zwcad.read.my-pipe",
    ) is None

def test_validate_icc_crystallize_unsafe_connector():
    from hooks.pre_tool_use import _validate_icc_crystallize
    err = _validate_icc_crystallize(
        {"connector": "ZWCAD", "pipeline_name": "p", "mode": "read"}, "zwcad.read.p"
    )
    assert err is not None and "connector" in err

def test_validate_icc_crystallize_path_traversal():
    from hooks.pre_tool_use import _validate_icc_crystallize
    err = _validate_icc_crystallize(
        {"connector": "zwcad", "pipeline_name": "../evil", "mode": "read"}, "zwcad.read.x"
    )
    assert err is not None and "pipeline_name" in err

def test_validate_icc_span_open_valid():
    from hooks.pre_tool_use import _validate_icc_span_open
    assert _validate_icc_span_open({}, "lark.read.get-doc") is None

def test_validate_icc_span_open_missing_sig():
    from hooks.pre_tool_use import _validate_icc_span_open
    err = _validate_icc_span_open({}, "")
    assert err is not None and "intent_signature" in err

def test_validate_icc_span_close_valid():
    from hooks.pre_tool_use import _validate_icc_span_close
    for outcome in ("success", "failure", "aborted"):
        assert _validate_icc_span_close({"outcome": outcome}) is None

def test_validate_icc_span_close_bad_outcome():
    from hooks.pre_tool_use import _validate_icc_span_close
    err = _validate_icc_span_close({"outcome": "done"})
    assert err is not None and "outcome" in err

def test_validate_icc_span_approve_valid():
    from hooks.pre_tool_use import _validate_icc_span_approve
    assert _validate_icc_span_approve({}, "zwcad.write.apply") is None

def test_validate_icc_span_approve_missing_sig():
    from hooks.pre_tool_use import _validate_icc_span_approve
    err = _validate_icc_span_approve({}, "")
    assert err is not None and "intent_signature" in err

def test_normalize_sig_no_change():
    from hooks.pre_tool_use import _normalize_sig
    sig, frm, to = _normalize_sig("zwcad.read.state")
    assert sig == "zwcad.read.state"
    assert frm is None and to is None

def test_normalize_sig_lowercases():
    from hooks.pre_tool_use import _normalize_sig
    sig, frm, to = _normalize_sig("ZWCAD.READ.State")
    assert sig == "zwcad.read.state"
    assert frm == "ZWCAD.READ.State"
    assert to == "zwcad.read.state"
