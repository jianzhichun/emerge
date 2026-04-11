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


def test_icc_goal_rollback_returns_ask():
    """icc_goal_rollback must return permissionDecision: ask for user confirmation."""
    payload = {
        "tool_name": "mcp__plugin_emerge_emerge__icc_goal_rollback",
        "tool_input": {"target_event_id": "evt-abc123", "actor": "claude"},
    }
    out = _run_hook(payload)
    hook_out = out.get("hookSpecificOutput", {})
    assert hook_out.get("hookEventName") == "PreToolUse"
    assert hook_out.get("permissionDecision") == "ask", \
        f"icc_goal_rollback must ask for confirmation, got: {out}"
    assert "systemMessage" in out
    assert "rollback" in out["systemMessage"].lower() or "evt-abc123" in out["systemMessage"]


def test_icc_goal_rollback_missing_target_blocks():
    """icc_goal_rollback without target_event_id should deny (schema enforcement)."""
    payload = {
        "tool_name": "mcp__plugin_emerge_emerge__icc_goal_rollback",
        "tool_input": {"actor": "claude"},
    }
    out = _run_hook(payload)
    hook_out = out.get("hookSpecificOutput", {})
    assert hook_out.get("permissionDecision") == "deny"
    assert "target_event_id" in hook_out.get("permissionDecisionReason", "").lower()
