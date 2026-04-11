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
