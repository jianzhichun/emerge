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


def _extract_l15_token(additional_context: str) -> dict:
    marker = "L1_5_TOKEN\n"
    assert marker in additional_context
    token_text = additional_context.rsplit(marker, 1)[1].strip()
    return json.loads(token_text)


def test_session_start_and_user_prompt_submit_output_parseable(tmp_path: Path):
    s_out = _run("session_start.py", {"goal": "Test goal"}, tmp_path)
    s_json = json.loads(s_out)
    assert s_json["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "additionalContext" in s_json["hookSpecificOutput"]

    u_out = _run("user_prompt_submit.py", {"budget_chars": 120}, tmp_path)
    u_json = json.loads(u_out)
    assert u_json["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "Goal" in u_json["hookSpecificOutput"]["additionalContext"]
    token = _extract_l15_token(u_json["hookSpecificOutput"]["additionalContext"])
    assert token["schema_version"] == "flywheel.v1"
    assert "deltas" in token
    assert token["goal_source"] in {"unset", "hook_payload"}


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
    assert p_json["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "additionalContext" in p_json["hookSpecificOutput"]
    token = _extract_l15_token(p_json["hookSpecificOutput"]["additionalContext"])
    assert token["schema_version"] == "flywheel.v1"
    assert token["deltas"]

    import os
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_DATA"] = str(tmp_path)
    proc = subprocess.run(
        ["python3", str(ROOT / "hooks" / "pre_compact.py")],
        input="{}",
        capture_output=True, text=True, env=env, check=True,
    )
    out = json.loads(proc.stdout.strip())
    assert out["hookSpecificOutput"]["hookEventName"] == "PreCompact"


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
    assert parsed["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert (tmp_path / ".emerge" / "hook-state" / "state.json").exists()


def test_hooks_tolerate_invalid_json_and_budget(tmp_path: Path):
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_DATA"] = str(tmp_path)
    bad = subprocess.run(
        ["python3", str(ROOT / "hooks" / "session_start.py")],
        input="{not json",
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    parsed_bad = json.loads(bad.stdout.strip())
    assert parsed_bad["hookSpecificOutput"]["hookEventName"] == "SessionStart"

    weird_budget = subprocess.run(
        ["python3", str(ROOT / "hooks" / "user_prompt_submit.py")],
        input=json.dumps({"budget_chars": None}),
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    parsed_budget = json.loads(weird_budget.stdout.strip())
    assert parsed_budget["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"


def test_post_tool_use_tolerates_non_object_tool_result(tmp_path: Path):
    out = _run(
        "post_tool_use.py",
        {
            "tool_name": "mcp__plugin_emerge__icc_write",
            "tool_result": "not-a-dict",
            "delta_message": "write attempted",
        },
        tmp_path,
    )
    parsed = json.loads(out)
    assert parsed["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "additionalContext" in parsed["hookSpecificOutput"]


def test_session_start_without_goal_does_not_write_default_goal(tmp_path: Path):
    out = _run("session_start.py", {}, tmp_path)
    parsed = json.loads(out)
    token = _extract_l15_token(parsed["hookSpecificOutput"]["additionalContext"])
    assert token["goal"] == ""
    assert token["goal_source"] == "unset"


def test_goal_is_capped_and_source_marked(tmp_path: Path):
    long_goal = "g" * 500
    _run("session_start.py", {"goal": long_goal}, tmp_path)
    out = _run("user_prompt_submit.py", {}, tmp_path)
    parsed = json.loads(out)
    token = _extract_l15_token(parsed["hookSpecificOutput"]["additionalContext"])
    assert len(token["goal"]) == 120
    assert token["goal_source"] == "hook_payload"


def test_pre_compact_emits_recovery_token(tmp_path: Path):
    # First seed some state via post_tool_use
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_DATA"] = str(tmp_path)
    subprocess.run(
        ["python3", str(ROOT / "hooks" / "post_tool_use.py")],
        input=json.dumps({
            "tool_name": "mcp__plugin_emerge__icc_write",
            "tool_result": {"verification_state": "verified"},
            "delta_message": "Wrote layer to ZWCAD",
        }),
        capture_output=True, text=True, env=env, check=True,
    )
    # Now run pre_compact with the seeded state
    proc = subprocess.run(
        ["python3", str(ROOT / "hooks" / "pre_compact.py")],
        input="{}",
        capture_output=True, text=True, env=env, check=True,
    )
    out = json.loads(proc.stdout.strip())
    assert out["hookSpecificOutput"]["hookEventName"] == "PreCompact"
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "L1_5_TOKEN" in ctx
    token_text = ctx.rsplit("L1_5_TOKEN\n", 1)[1].strip()
    token = json.loads(token_text)
    assert token["schema_version"] == "flywheel.v1"
    assert isinstance(token["deltas"], list)
    assert len(ctx) <= 900  # budget enforced
