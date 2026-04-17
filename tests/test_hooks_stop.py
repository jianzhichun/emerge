"""Tests for stop.py span sentinel hook."""
from __future__ import annotations
import json
import subprocess
import sys
import tempfile
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STOP_HOOK = ROOT / "hooks" / "stop.py"


def _run_stop_hook(state: dict | None = None) -> dict:
    """Run stop.py with a temporary state.json via EMERGE_HOOK_STATE_ROOT."""
    with tempfile.TemporaryDirectory() as tmpdir:
        env = {**os.environ, "EMERGE_HOOK_STATE_ROOT": tmpdir}
        if state is not None:
            state_path = Path(tmpdir) / "state.json"
            state_path.write_text(json.dumps(state), encoding="utf-8")
        payload = {"hook_event_name": "Stop", "session_id": "test-session"}
        result = subprocess.run(
            [sys.executable, str(STOP_HOOK)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, f"stop.py exited non-zero: {result.stderr}"
        return json.loads(result.stdout) if result.stdout.strip() else {}


def test_stop_blocks_when_span_open():
    """Stop hook must block when active_span_id is in state.json."""
    state = {
        "active_span_id": "span-abc123",
        "active_span_intent": "zwcad.read.state",
    }
    out = _run_stop_hook(state=state)
    assert out.get("decision") == "block", f"Expected block, got: {out}"
    assert "zwcad.read.state" in out.get("reason", "")
    assert "icc_span_close" in out.get("reason", "")


def test_stop_allows_when_no_span():
    """Stop hook must not block when active_span_id is falsy."""
    out = _run_stop_hook(state={"active_span_id": None})
    assert out.get("decision") != "block"


def test_stop_allows_when_no_state_file():
    """Stop hook must not block when state.json doesn't exist."""
    out = _run_stop_hook(state=None)
    assert out.get("decision") != "block"


def test_stop_uses_intent_in_reason():
    """Block reason must include the intent signature, not just span_id."""
    state = {
        "active_span_id": "span-xyz",
        "active_span_intent": "hypermesh.write.apply-change",
    }
    out = _run_stop_hook(state=state)
    assert "hypermesh.write.apply-change" in out.get("reason", "")
