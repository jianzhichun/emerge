"""Tests for post_tool_use.py updatedMCPToolOutput injection."""
from __future__ import annotations
import json
import subprocess
import sys
import tempfile
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POST_HOOK = ROOT / "hooks" / "post_tool_use.py"


def _run_post_hook(payload: dict, state: dict | None = None) -> dict:
    with tempfile.TemporaryDirectory() as tmpdir:
        env = {**os.environ, "EMERGE_HOOK_STATE_ROOT": tmpdir}
        if state is not None:
            (Path(tmpdir) / "state.json").write_text(json.dumps(state), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(POST_HOOK)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, f"post_tool_use.py stderr: {result.stderr}"
        return json.loads(result.stdout) if result.stdout.strip() else {}


def test_icc_exec_with_active_span_injects_span_context():
    """When icc_exec runs inside an active span, hook injects span_id into updatedMCPToolOutput."""
    state = {
        "active_span_id": "span-abc",
        "active_span_intent": "zwcad.read.state",
    }
    tool_result = {
        "isError": False,
        "content": [{"type": "text", "text": '{"result": [{"layers": 3}]}'}],
        "structuredContent": {"result": [{"layers": 3}]},
    }
    payload = {
        "tool_name": "mcp__plugin_emerge_emerge__icc_exec",
        "tool_input": {"intent_signature": "zwcad.read.state", "code": "x=1"},
        "tool_response": tool_result,
    }
    out = _run_post_hook(payload, state=state)
    assert "hookSpecificOutput" in out
    hook_out = out["hookSpecificOutput"]
    assert "updatedMCPToolOutput" in hook_out, "Missing updatedMCPToolOutput for icc_exec with active span"
    updated = hook_out["updatedMCPToolOutput"]
    sc = updated.get("structuredContent", {})
    assert sc.get("_span_id") == "span-abc"
    assert sc.get("_span_intent") == "zwcad.read.state"


def test_icc_exec_without_active_span_no_injection():
    """When no span is active, icc_exec hook must NOT add updatedMCPToolOutput."""
    payload = {
        "tool_name": "mcp__plugin_emerge_emerge__icc_exec",
        "tool_input": {"intent_signature": "zwcad.read.state", "code": "x=1"},
        "tool_response": {"isError": False, "content": [{"type": "text", "text": "ok"}]},
    }
    out = _run_post_hook(payload, state={"active_span_id": None})
    hook_out = out.get("hookSpecificOutput", {})
    assert "updatedMCPToolOutput" not in hook_out


def test_non_exec_tool_never_injects():
    """icc_span_close must never emit updatedMCPToolOutput."""
    state = {"active_span_id": "span-xyz", "active_span_intent": "foo.read.bar"}
    payload = {
        "tool_name": "mcp__plugin_emerge_emerge__icc_span_close",
        "tool_input": {"outcome": "success"},
        "tool_response": {"isError": False, "content": [{"type": "text", "text": "ok"}]},
    }
    out = _run_post_hook(payload, state=state)
    hook_out = out.get("hookSpecificOutput", {})
    assert "updatedMCPToolOutput" not in hook_out


def test_verification_state_reads_tool_response_not_tool_result():
    """Hook must read verification_state from payload.tool_response."""
    with tempfile.TemporaryDirectory() as tmpdir:
        env = {**os.environ, "EMERGE_HOOK_STATE_ROOT": tmpdir}
        payload = {
            "tool_name": "mcp__plugin_emerge_emerge__icc_span_close",
            "tool_input": {"outcome": "failure"},
            "tool_response": {
                "content": [{"type": "text", "text": '{"verification_state":"degraded"}'}]
            },
        }
        result = subprocess.run(
            [sys.executable, str(POST_HOOK)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, f"post_tool_use.py stderr: {result.stderr}"
        state = json.loads((Path(tmpdir) / "state.json").read_text(encoding="utf-8"))
        assert state.get("verification_state") == "degraded"
