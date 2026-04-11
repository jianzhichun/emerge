"""Tests for post_tool_use_failure.py behavior."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks" / "post_tool_use_failure.py"


def _run_failure_hook(payload: dict, tmpdir: str) -> tuple[dict, dict]:
    env = {**os.environ, "CLAUDE_PLUGIN_DATA": tmpdir}
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, f"post_tool_use_failure.py stderr: {result.stderr}"
    output = json.loads(result.stdout) if result.stdout.strip() else {}
    state = json.loads((Path(tmpdir) / "state.json").read_text(encoding="utf-8"))
    return output, state


def test_post_tool_use_failure_marks_degraded_for_real_failure():
    with tempfile.TemporaryDirectory() as tmpdir:
        payload = {
            "tool_name": "mcp__plugin_emerge_emerge__icc_exec",
            "error": "boom",
            "is_interrupt": False,
        }
        _out, state = _run_failure_hook(payload, tmpdir)
        assert state.get("verification_state") == "degraded"
        assert state.get("open_risks"), "Expected risk entry for real tool failure"


def test_post_tool_use_failure_interrupt_does_not_mark_degraded():
    with tempfile.TemporaryDirectory() as tmpdir:
        payload = {
            "tool_name": "mcp__plugin_emerge_emerge__icc_exec",
            "error": "interrupted by user",
            "is_interrupt": True,
        }
        _out, state = _run_failure_hook(payload, tmpdir)
        assert state.get("verification_state") == "verified"
        assert state.get("open_risks") == []
