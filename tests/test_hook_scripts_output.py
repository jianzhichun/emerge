import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _run(script: str, payload: dict, data_dir: Path) -> str:
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_DATA"] = str(data_dir)
    proc = subprocess.run(
        ["python3", str(ROOT / "hooks" / script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return proc.stdout.strip()


def test_session_start_and_user_prompt_submit_output_parseable(tmp_path: Path):
    s_out = _run("session_start.py", {"goal": "Test goal"}, tmp_path)
    s_json = json.loads(s_out)
    assert s_json["hookEventName"] == "SessionStart"
    assert "additionalContext" in s_json["hookSpecificOutput"]

    u_out = _run("user_prompt_submit.py", {"budget_chars": 120}, tmp_path)
    u_json = json.loads(u_out)
    assert u_json["hookEventName"] == "UserPromptSubmit"
    assert "Goal" in u_json["hookSpecificOutput"]["additionalContext"]


def test_post_tool_use_and_pre_compact_contract(tmp_path: Path):
    p_out = _run(
        "post_tool_use.py",
        {
            "tool_name": "mcp__plugin_emerge__icc_read",
            "tool_result": {"verification_state": "verified"},
            "delta_message": "Read layer snapshot",
        },
        tmp_path,
    )
    p_json = json.loads(p_out)
    assert p_json["hookEventName"] == "PostToolUse"
    assert "additionalContext" in p_json["hookSpecificOutput"]

    proc = subprocess.run(
        ["python3", str(ROOT / "hooks" / "pre_compact.py")],
        capture_output=True,
        text=True,
        check=True,
    )
    text = proc.stdout.strip()
    assert text.startswith("Keep only Goal")


def test_hook_default_state_dir_uses_home_emerge(tmp_path: Path):
    env = os.environ.copy()
    env.pop("CLAUDE_PLUGIN_DATA", None)
    env["HOME"] = str(tmp_path)
    proc = subprocess.run(
        ["python3", str(ROOT / "hooks" / "session_start.py")],
        input=json.dumps({"goal": "home default"}),
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    parsed = json.loads(proc.stdout.strip())
    assert parsed["hookEventName"] == "SessionStart"
    assert (tmp_path / ".emerge" / "hook-state" / "state.json").exists()
