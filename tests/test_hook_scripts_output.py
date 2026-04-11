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


def _extract_flywheel_token(additional_context: str) -> dict:
    marker = "FLYWHEEL_TOKEN\n"
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
    token = _extract_flywheel_token(u_json["hookSpecificOutput"]["additionalContext"])
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
    token = _extract_flywheel_token(p_json["hookSpecificOutput"]["additionalContext"])
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
    assert "hookSpecificOutput" not in out
    assert "systemMessage" in out
    assert "FLYWHEEL_TOKEN" in out["systemMessage"]
    pc_token = _extract_flywheel_token(out["systemMessage"])
    assert pc_token["schema_version"] == "flywheel.v1"


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
    token = _extract_flywheel_token(parsed["hookSpecificOutput"]["additionalContext"])
    assert token["goal"] == ""
    assert token["goal_source"] == "unset"


def test_goal_is_capped_and_source_marked(tmp_path: Path):
    long_goal = "g" * 500
    _run("session_start.py", {"goal": long_goal}, tmp_path)
    out = _run("user_prompt_submit.py", {}, tmp_path)
    parsed = json.loads(out)
    token = _extract_flywheel_token(parsed["hookSpecificOutput"]["additionalContext"])
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
    assert "hookSpecificOutput" not in out
    assert "systemMessage" in out
    ctx = out["systemMessage"]
    assert "FLYWHEEL_TOKEN" in ctx
    token_text = ctx.rsplit("FLYWHEEL_TOKEN\n", 1)[1].strip()
    token = json.loads(token_text)
    assert token["schema_version"] == "flywheel.v1"
    assert isinstance(token["deltas"], list)
    assert len(ctx) <= 900  # budget enforced


def test_post_tool_use_reads_verification_state_from_content_json(tmp_path: Path):
    """verification_state must be parsed from content[0]["text"] JSON, not the outer wrapper."""
    inner = json.dumps({
        "rows": [{"id": "L0"}],
        "verification_state": "degraded",
        "verify_result": {"ok": False},
    })
    out = _run(
        "post_tool_use.py",
        {
            "tool_name": "mcp__plugin_emerge__icc_read",
            "tool_result": {
                "isError": False,
                "content": [{"type": "text", "text": inner}],
            },
            "delta_message": "Read layers — verification failed",
        },
        tmp_path,
    )
    parsed = json.loads(out)
    token = _extract_flywheel_token(parsed["hookSpecificOutput"]["additionalContext"])
    # degraded verification_state in content must propagate to the tracker
    assert token["verification_state"] == "degraded", (
        "verification_state inside content[0]['text'] must be read, not the outer wrapper"
    )
    assert any("pipeline verification failed" in r for r in token.get("open_risks", [])), (
        "degraded verification must add an open_risk entry"
    )


def test_post_tool_use_no_longer_echoes_updated_mcp_tool_output(tmp_path: Path):
    """PostToolUse hook must NOT include updatedMCPToolOutput (no-op echo wastes bandwidth)."""
    out = _run(
        "post_tool_use.py",
        {
            "tool_name": "mcp__plugin_emerge__icc_write",
            "tool_result": {"isError": False, "content": [{"type": "text", "text": "ok"}]},
            "delta_message": "Write applied",
        },
        tmp_path,
    )
    parsed = json.loads(out)
    assert "updatedMCPToolOutput" not in parsed.get("hookSpecificOutput", {}), (
        "PostToolUse must not echo updatedMCPToolOutput when result is unchanged"
    )


def test_pre_compact_resets_tracker_state_and_keeps_goal_in_snapshot(tmp_path: Path):
    """After PreCompact, state.json resets while active goal remains in goal-snapshot."""
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_DATA"] = str(tmp_path)

    # Seed goal + some deltas + a risk
    subprocess.run(
        ["python3", str(ROOT / "hooks" / "session_start.py")],
        input=json.dumps({"goal": "deploy hypermesh pipeline"}),
        capture_output=True, text=True, env=env, check=True,
    )
    subprocess.run(
        ["python3", str(ROOT / "hooks" / "post_tool_use.py")],
        input=json.dumps({
            "tool_name": "mcp__plugin_emerge__icc_write",
            "tool_result": {"isError": False, "content": [{"type": "text", "text": "ok"}]},
            "delta_message": "Wrote mesh to HyperMesh",
        }),
        capture_output=True, text=True, env=env, check=True,
    )

    # Verify state has deltas before compaction
    state_path = tmp_path / "state.json"
    before = json.loads(state_path.read_text())
    assert before["deltas"], "must have deltas before compaction"

    # Run pre_compact
    subprocess.run(
        ["python3", str(ROOT / "hooks" / "pre_compact.py")],
        input="{}",
        capture_output=True, text=True, env=env, check=True,
    )

    # After compaction: deltas and risks must be cleared in state.json
    after = json.loads(state_path.read_text())
    assert after["deltas"] == [], "deltas must be cleared after PreCompact"
    assert after["open_risks"] == [], "open_risks must be cleared after PreCompact"
    # Goal ownership moved to goal-snapshot.json
    snap = json.loads((tmp_path / "goal-snapshot.json").read_text())
    assert snap["text"] == "deploy hypermesh pipeline"
    assert snap["source"] == "hook_payload"


def test_session_start_clears_stale_active_span(tmp_path, monkeypatch):
    """SessionStart must clear active_span_id left by a crashed previous session."""
    import json, subprocess, sys
    hook_state = tmp_path / "hook-state"
    hook_state.mkdir()
    stale_state = {
        "active_span_id": "stale-uuid",
        "active_span_intent": "lark.read.get-doc",
        "goal": "",
        "goal_source": "unset",
        "deltas": [],
    }
    (hook_state / "state.json").write_text(json.dumps(stale_state), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(hook_state))

    result = subprocess.run(
        [sys.executable, "hooks/session_start.py"],
        input=json.dumps({}),
        capture_output=True, text=True,
        cwd=str(Path(__file__).parents[1]),
    )
    assert result.returncode == 0
    state = json.loads((hook_state / "state.json").read_text())
    assert "active_span_id" not in state
    assert "active_span_intent" not in state


def _run_post_hook(payload: dict, hook_state: Path) -> dict:
    import json, subprocess, sys, os
    env = {**os.environ, "CLAUDE_PLUGIN_DATA": str(hook_state)}
    result = subprocess.run(
        [sys.executable, "hooks/post_tool_use.py"],
        input=json.dumps(payload),
        capture_output=True, text=True,
        cwd=str(Path(__file__).parents[1]),
        env=env,
    )
    return json.loads(result.stdout) if result.stdout.strip() else {}


def test_post_tool_use_records_action_when_span_active(tmp_path):
    import json
    hook_state = tmp_path / "hook-state"
    hook_state.mkdir()
    state = {"active_span_id": "span-123", "active_span_intent": "lark.read.get-doc",
             "goal": "", "goal_source": "unset", "deltas": []}
    (hook_state / "state.json").write_text(json.dumps(state), encoding="utf-8")
    _run_post_hook({"tool_name": "mcp__lark_doc__get",
                    "tool_result": {"content": [{"type": "text", "text": "{}"}]}},
                   hook_state)
    buf = hook_state / "active-span-actions.jsonl"
    assert buf.exists()
    rec = json.loads(buf.read_text().strip())
    assert rec["tool_name"] == "mcp__lark_doc__get"
    assert rec["has_side_effects"] is False  # __get is read-only


def test_post_tool_use_excludes_icc_exec_from_span(tmp_path):
    import json
    hook_state = tmp_path / "hook-state"
    hook_state.mkdir()
    state = {"active_span_id": "span-123", "active_span_intent": "lark.read.get-doc",
             "goal": "", "goal_source": "unset", "deltas": []}
    (hook_state / "state.json").write_text(json.dumps(state), encoding="utf-8")
    _run_post_hook({"tool_name": "emerge__icc_exec",
                    "tool_result": {"content": [{"type": "text", "text": "{}"}]}},
                   hook_state)
    buf = hook_state / "active-span-actions.jsonl"
    assert not buf.exists() or buf.read_text().strip() == "", \
        "icc_exec must not be recorded as a span action"


def test_post_tool_use_no_recording_without_active_span(tmp_path):
    hook_state = tmp_path / "hook-state"
    hook_state.mkdir()
    (hook_state / "state.json").write_text("{}", encoding="utf-8")
    _run_post_hook({"tool_name": "mcp__lark_doc__get",
                    "tool_result": {"content": [{"type": "text", "text": "{}"}]}},
                   hook_state)
    buf = hook_state / "active-span-actions.jsonl"
    assert not buf.exists() or buf.read_text().strip() == ""


def test_post_tool_use_preserves_active_span_id_across_calls(tmp_path):
    """active_span_id must survive in state.json after post_tool_use (so next call still records)."""
    import json
    hook_state = tmp_path / "hook-state"
    hook_state.mkdir()
    state = {"active_span_id": "span-xyz", "active_span_intent": "lark.read.get-doc",
             "goal": "", "goal_source": "unset", "deltas": []}
    (hook_state / "state.json").write_text(json.dumps(state), encoding="utf-8")
    _run_post_hook({"tool_name": "mcp__lark_doc__get",
                    "tool_result": {"content": [{"type": "text", "text": "{}"}]}},
                   hook_state)
    after = json.loads((hook_state / "state.json").read_text())
    assert after.get("active_span_id") == "span-xyz", \
        "active_span_id must persist in state.json after post_tool_use"


def _run_pre_hook(payload: dict) -> dict:
    import json, subprocess, sys, os
    from pathlib import Path
    result = subprocess.run(
        [sys.executable, "hooks/pre_tool_use.py"],
        input=json.dumps(payload),
        capture_output=True, text=True,
        cwd=str(Path(__file__).parents[1]),
    )
    return json.loads(result.stdout) if result.stdout.strip() else {}


def test_pre_hook_blocks_span_open_missing_intent():
    out = _run_pre_hook({"tool_name": "emerge__icc_span_open", "tool_input": {}})
    hook_out = out.get("hookSpecificOutput", {})
    assert hook_out.get("permissionDecision") == "deny"

def test_pre_hook_blocks_span_open_invalid_intent():
    out = _run_pre_hook({"tool_name": "emerge__icc_span_open",
                         "tool_input": {"intent_signature": "no_mode_segment"}})
    hook_out = out.get("hookSpecificOutput", {})
    assert hook_out.get("permissionDecision") == "deny"

def test_pre_hook_allows_valid_span_open():
    out = _run_pre_hook({"tool_name": "emerge__icc_span_open",
                         "tool_input": {"intent_signature": "lark.read.get-doc"}})
    hook_out = out.get("hookSpecificOutput", {})
    assert hook_out.get("permissionDecision") != "deny"

def test_pre_hook_blocks_span_close_bad_outcome():
    out = _run_pre_hook({"tool_name": "emerge__icc_span_close",
                         "tool_input": {"outcome": "done"}})
    hook_out = out.get("hookSpecificOutput", {})
    assert hook_out.get("permissionDecision") == "deny"

def test_pre_hook_allows_valid_span_close():
    out = _run_pre_hook({"tool_name": "emerge__icc_span_close",
                         "tool_input": {"outcome": "success"}})
    hook_out = out.get("hookSpecificOutput", {})
    assert hook_out.get("permissionDecision") != "deny"

def test_pre_hook_blocks_span_approve_missing_intent():
    out = _run_pre_hook({"tool_name": "emerge__icc_span_approve", "tool_input": {}})
    hook_out = out.get("hookSpecificOutput", {})
    assert hook_out.get("permissionDecision") == "deny"

def test_pre_hook_allows_valid_span_approve():
    out = _run_pre_hook({"tool_name": "emerge__icc_span_approve",
                         "tool_input": {"intent_signature": "lark.write.create-doc"}})
    hook_out = out.get("hookSpecificOutput", {})
    assert hook_out.get("permissionDecision") != "deny"
