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
        cwd=str(ROOT),
    )
    assert result.returncode == 0, f"hook exited {result.returncode}: {result.stderr}"
    return json.loads(result.stdout)


def test_pre_tool_use_blocks_missing_intent_with_correction_hint():
    """PreToolUse for icc_exec missing intent_signature must block with correction hint."""
    out = _run_hook({
        "tool_name": "emerge__icc_exec",
        "tool_input": {"code": "result = {}", "mode": "inline_code"},
    })
    assert out.get("decision") == "block"
    assert "intent_signature" in out.get("reason", "").lower()


def test_pre_tool_use_approves_valid_intent_signature():
    """PreToolUse for icc_exec with valid intent_signature must approve."""
    out = _run_hook({
        "tool_name": "emerge__icc_exec",
        "tool_input": {
            "intent_signature": "zwcad.read.layers",
            "code": "result = {}",
            "mode": "inline_code",
        },
    })
    assert "decision" not in out  # no block
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"


def test_pre_tool_use_blocks_two_part_intent_with_fix_hint():
    """When intent_signature has 2 parts (missing connector), block and explain required format."""
    out = _run_hook({
        "tool_name": "emerge__icc_exec",
        "tool_input": {
            "intent_signature": "read.layers",   # 2 parts — missing connector
            "code": "result = {}",
            "mode": "inline_code",
        },
    })
    assert out.get("decision") == "block"
    reason = out.get("reason", "")
    # Must explain correct format with 3 parts
    assert (
        "connector.mode.name" in reason
        or "zwcad.read.layers" in reason
        or "3 parts" in reason.lower()
        or "2 parts" in reason.lower()
    )
