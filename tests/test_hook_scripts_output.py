import json
import os
import subprocess
import sys
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


def test_post_compact_emits_fresh_flywheel_token(tmp_path: Path):
    """PostCompact must output a systemMessage with a clean FLYWHEEL_TOKEN."""
    out = _run(
        "post_compact.py",
        {
            "hook_event_name": "PostCompact",
            "trigger": "manual",
            "compact_summary": "Session was compacted. Goal: test goal.",
        },
        tmp_path,
    )
    result = json.loads(out)
    # PostCompact uses top-level systemMessage (not hookSpecificOutput)
    assert "systemMessage" in result
    assert "hookSpecificOutput" not in result
    msg = result["systemMessage"]
    assert "FLYWHEEL_TOKEN" in msg
    token = json.loads(msg.split("FLYWHEEL_TOKEN\n")[1].strip().split("\n")[0])
    assert token["schema_version"] == "flywheel.v1"
    # After compaction, state was reset by PreCompact — token must show empty deltas/risks
    assert token["deltas"] == []
    assert token["open_risks"] == []


def test_post_compact_includes_span_protocol(tmp_path: Path):
    """PostCompact systemMessage must include Span Protocol directive."""
    out = _run("post_compact.py", {"hook_event_name": "PostCompact", "compact_summary": ""}, tmp_path)
    result = json.loads(out)
    assert "Span Protocol" in result["systemMessage"]


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


def test_user_prompt_submit_drains_pending_actions(tmp_path: Path):
    """UserPromptSubmit injects cockpit pending actions into additionalContext."""
    processed = tmp_path / "pending-actions.processed.json"
    processed.write_text(json.dumps({
        "submitted_at": 1000,
        "actions": [
            {"type": "tool-call", "call": {"tool": "icc_exec", "arguments": {"intent_signature": "a.read.b"}}, "meta": {}},
            {"type": "notes-comment", "connector": "myconn", "comment": "operator note"},
        ],
    }))
    out = _run("user_prompt_submit.py", {}, tmp_path)
    parsed = json.loads(out)
    ctx = parsed["hookSpecificOutput"]["additionalContext"]
    assert "[Cockpit]" in ctx
    assert "icc_exec" in ctx
    assert "Append comment" in ctx
    # File renamed to .delivered.json
    assert not processed.exists()
    assert (tmp_path / "pending-actions.delivered.json").exists()


def test_user_prompt_submit_drains_unprocessed_pending_actions(tmp_path: Path):
    """UserPromptSubmit also picks up pending-actions.json (not yet processed by daemon)."""
    pending = tmp_path / "pending-actions.json"
    pending.write_text(json.dumps({
        "submitted_at": 2000,
        "actions": [{"type": "pipeline-delete", "key": "x.read.y"}],
    }))
    out = _run("user_prompt_submit.py", {}, tmp_path)
    parsed = json.loads(out)
    ctx = parsed["hookSpecificOutput"]["additionalContext"]
    assert "pipeline-delete" in ctx
    assert not pending.exists()
    assert (tmp_path / "pending-actions.delivered.json").exists()


def test_watch_pending_emits_and_renames(tmp_path: Path):
    """watch_pending.py prints actions to stdout and renames file."""
    import subprocess, time, signal
    pending = tmp_path / "pending-actions.json"
    pending.write_text(json.dumps({
        "submitted_at": int(time.time() * 1000),
        "actions": [{"type": "tool-call", "call": {"tool": "icc_exec", "arguments": {"intent_signature": "a.read.b"}}, "meta": {}}],
    }))
    env = os.environ.copy()
    env["EMERGE_STATE_ROOT"] = str(tmp_path)
    proc = subprocess.Popen(
        ["python3", str(ROOT / "scripts" / "watch_pending.py")],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    deadline = time.time() + 3.0
    while time.time() < deadline and pending.exists():
        time.sleep(0.1)
    proc.send_signal(signal.SIGTERM)
    stdout, _ = proc.communicate(timeout=3)
    assert "[Cockpit]" in stdout
    assert "icc_exec" in stdout
    assert (tmp_path / "pending-actions.processed.json").exists()
    assert not pending.exists()


def test_save_tracker_preserves_span_fields(tmp_path):
    """save_tracker must preserve active_span_id and active_span_intent fields."""
    from scripts.state_tracker import load_tracker, save_tracker
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps({
            "active_span_id": "span-abc",
            "active_span_intent": "test.read.pipe",
            "goal": "test goal",
        }),
        encoding="utf-8",
    )
    tracker = load_tracker(state_path)
    save_tracker(state_path, tracker)
    result = json.loads(state_path.read_text(encoding="utf-8"))
    assert result["active_span_id"] == "span-abc"
    assert result["active_span_intent"] == "test.read.pipe"


def test_post_tool_use_preserves_span_id(tmp_path):
    """post_tool_use hook must preserve active_span_id and active_span_intent via save_tracker alone."""
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps({
            "active_span_id": "span-xyz",
            "active_span_intent": "test.read.op",
            "goal": "",
        }),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_DATA"] = str(tmp_path)
    payload = {
        "tool_name": "mcp__plugin_emerge__icc_exec",
        "tool_response": {"content": [{"type": "text", "text": "{}"}]},
        "tool_input": {"intent_signature": "test.read.op"},
    }
    proc = subprocess.run(
        ["python3", str(ROOT / "hooks" / "post_tool_use.py")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    result = json.loads(state_path.read_text(encoding="utf-8"))
    assert result.get("active_span_id") == "span-xyz"
    assert result.get("active_span_intent") == "test.read.op"


def test_format_pending_actions_tool_call():
    from scripts.pending_actions import format_pending_actions
    actions = [
        {
            "type": "tool-call",
            "call": {"tool": "icc_exec", "arguments": {"intent_signature": "test.read.pipe"}},
            "meta": {"scope": "connector"},
        }
    ]
    result = format_pending_actions(actions)
    assert result.startswith("[Cockpit]")
    assert "icc_exec" in result
    assert "scope=connector" in result


def test_format_pending_actions_pipeline_set():
    from scripts.pending_actions import format_pending_actions
    actions = [{"type": "pipeline-set", "key": "foo", "fields": {"stable": True}}]
    result = format_pending_actions(actions)
    assert "pipeline-set foo" in result


def test_format_pending_actions_notes_edit():
    from scripts.pending_actions import format_pending_actions
    actions = [{"type": "notes-edit", "connector": "gmail"}]
    result = format_pending_actions(actions)
    assert "gmail" in result
    assert "NOTES.md" in result


def test_stop_failure_clears_active_span(tmp_path: Path):
    """StopFailure hook must clear active_span_id from state.json and emit systemMessage."""
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps({
            "active_span_id": "span-err-1",
            "active_span_intent": "test.read.op",
            "goal": "",
        }),
        encoding="utf-8",
    )
    out = _run("stop_failure.py", {"error": "rate_limit"}, tmp_path)
    result = json.loads(out)
    assert "systemMessage" in result
    assert "span-err-1" in result["systemMessage"]
    assert "rate_limit" in result["systemMessage"]
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert "active_span_id" not in state or not state["active_span_id"]


def test_stop_failure_no_span_emits_empty(tmp_path: Path):
    """StopFailure hook must emit {} when no active span is present."""
    out = _run("stop_failure.py", {"error": "rate_limit"}, tmp_path)
    assert json.loads(out) == {}


def test_task_completed_blocks_when_span_open(tmp_path: Path):
    """TaskCompleted hook must exit 2 with stderr message when active span is open."""
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps({
            "active_span_id": "span-task-1",
            "active_span_intent": "test.write.cmd",
        }),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_DATA"] = str(tmp_path)
    proc = subprocess.run(
        ["python3", str(ROOT / "hooks" / "task_completed.py")],
        input="{}",
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 2
    assert "span-task-1" in proc.stderr
    assert "icc_span_close" in proc.stderr


def test_task_completed_passes_when_no_span(tmp_path: Path):
    """TaskCompleted hook must exit 0 and print {} when no active span."""
    out = _run("task_completed.py", {}, tmp_path)
    assert json.loads(out) == {}


def test_subagent_start_injects_span_guardrail(tmp_path: Path):
    """SubagentStart must emit systemMessage with span ID and ownership rule."""
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps({
            "active_span_id": "span-parent-1",
            "active_span_intent": "cad.read.layers",
        }),
        encoding="utf-8",
    )
    out = _run("subagent_start.py", {}, tmp_path)
    result = json.loads(out)
    assert "systemMessage" in result
    assert "span-parent-1" in result["systemMessage"]
    assert "icc_span_close" in result["systemMessage"]


def test_subagent_start_no_span_emits_empty(tmp_path: Path):
    """SubagentStart must emit {} when no active span."""
    out = _run("subagent_start.py", {}, tmp_path)
    assert json.loads(out) == {}


def test_process_once_formats_and_renames(tmp_path: Path):
    """process_once must call formatter, print output, rename file, return new ts."""
    from scripts.watch_file import process_once
    from io import StringIO

    pending = tmp_path / "alerts.json"
    pending.write_text(
        json.dumps({"submitted_at": 5000, "val": "hello"}),
        encoding="utf-8",
    )

    calls = []
    def fmt(data):
        calls.append(data)
        return f"msg:{data['val']}"

    buf = StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        new_ts = process_once(pending, fmt, ".processed.json", last_ts=0)
    finally:
        sys.stdout = old_stdout

    assert new_ts == 5000
    assert calls[0]["val"] == "hello"
    assert "msg:hello" in buf.getvalue()
    assert (tmp_path / "alerts.processed.json").exists()
    assert not pending.exists()


def test_process_once_skips_stale_timestamp(tmp_path: Path):
    """process_once must not re-process a file whose submitted_at <= last_ts."""
    from scripts.watch_file import process_once

    pending = tmp_path / "alerts.json"
    pending.write_text(json.dumps({"submitted_at": 100}), encoding="utf-8")

    calls = []
    new_ts = process_once(pending, lambda d: calls.append(d) or "x", ".processed.json", last_ts=100)

    assert new_ts == 100
    assert not calls
    assert pending.exists()


def test_format_pattern_alert_includes_key_fields():
    from scripts.pending_actions import format_pattern_alert
    data = {
        "stage": "canary",
        "intent_signature": "hm.node_create",
        "message": "Repeated pattern detected",
        "meta": {"occurrences": 5, "window_minutes": 10, "machine_ids": ["local"]},
    }
    result = format_pattern_alert(data)
    assert "[OperatorMonitor]" in result
    assert "canary" in result
    assert "hm.node_create" in result
    assert "occurrences=5" in result


def test_init_goal_control_plane_helper(tmp_path):
    from scripts.goal_control_plane import GoalControlPlane, init_goal_control_plane
    from scripts.state_tracker import StateTracker
    tracker = StateTracker()
    tracker.set_goal("my goal", "test_source")
    gcp = init_goal_control_plane(tmp_path, tracker)
    assert isinstance(gcp, GoalControlPlane)
    snap = gcp.read_snapshot()
    assert snap["text"] == "my goal"


# ── Span Protocol injection tests ────────────────────────────────────────────


def test_session_start_includes_span_protocol(tmp_path, monkeypatch):
    import json, subprocess, sys
    hook_state = tmp_path / "hook-state"
    hook_state.mkdir()
    (hook_state / "state.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(hook_state))
    result = subprocess.run(
        [sys.executable, "hooks/session_start.py"],
        input=json.dumps({}),
        capture_output=True, text=True,
        cwd=str(Path(__file__).parents[1]),
    )
    assert result.returncode == 0
    out = json.loads(result.stdout.strip())
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "Span Protocol" in ctx
    assert "icc_span_open" in ctx
    assert "icc_span_close" in ctx


def test_recovery_token_includes_active_span_fields():
    from scripts.state_tracker import StateTracker
    tracker = StateTracker()
    tracker.state["active_span_id"] = "span-abc"
    tracker.state["active_span_intent"] = "lark.read.get-doc"
    token = tracker.format_recovery_token()
    assert token["active_span_id"] == "span-abc"
    assert token["active_span_intent"] == "lark.read.get-doc"


def test_recovery_token_active_span_fields_null_when_no_span():
    from scripts.state_tracker import StateTracker
    tracker = StateTracker()
    token = tracker.format_recovery_token()
    assert token["active_span_id"] is None
    assert token["active_span_intent"] is None


def test_pre_compact_includes_span_protocol(tmp_path):
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_DATA"] = str(tmp_path)
    (tmp_path / "state.json").write_text("{}", encoding="utf-8")
    proc = subprocess.run(
        ["python3", str(ROOT / "hooks" / "pre_compact.py")],
        input="{}",
        capture_output=True, text=True, env=env, check=True,
    )
    out = json.loads(proc.stdout.strip())
    ctx = out["systemMessage"]
    assert "Span Protocol" in ctx
    assert "icc_span_open" in ctx


def test_pre_compact_includes_active_span_reminder(tmp_path):
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_DATA"] = str(tmp_path)
    state = {
        "active_span_id": "span-xyz",
        "active_span_intent": "zwcad.read.layers",
    }
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    proc = subprocess.run(
        ["python3", str(ROOT / "hooks" / "pre_compact.py")],
        input="{}",
        capture_output=True, text=True, env=env, check=True,
    )
    out = json.loads(proc.stdout.strip())
    ctx = out["systemMessage"]
    assert "Active span: span-xyz (zwcad.read.layers)" in ctx
    assert "icc_span_close" in ctx


def _seed_span_reflection_data(exec_root: Path) -> None:
    exec_root.mkdir(parents=True, exist_ok=True)
    (exec_root / "span-candidates.json").write_text(
        json.dumps(
            {
                "spans": {
                    "lark.read.get-doc": {
                        "attempts": 40,
                        "successes": 40,
                        "human_fixes": 0,
                        "consecutive_failures": 0,
                        "recent_outcomes": [1] * 20,
                        "last_ts_ms": 1000,
                    },
                    "lark.read.list-records": {
                        "attempts": 20,
                        "successes": 19,
                        "human_fixes": 0,
                        "consecutive_failures": 0,
                        "recent_outcomes": [1] * 20,
                        "last_ts_ms": 2000,
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    wal_dir = exec_root / "span-wal"
    wal_dir.mkdir(parents=True, exist_ok=True)
    (wal_dir / "spans.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"intent_signature": "lark.read.get-doc", "outcome": "success"}),
                json.dumps({"intent_signature": "lark.read.list-records", "outcome": "failure"}),
            ]
        ),
        encoding="utf-8",
    )


def test_pre_compact_includes_muscle_memory(tmp_path):
    hook_state = tmp_path / "hook-state"
    hook_state.mkdir()
    (hook_state / "state.json").write_text("{}", encoding="utf-8")
    exec_root = tmp_path / "exec-state"
    _seed_span_reflection_data(exec_root)
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_DATA"] = str(hook_state)
    env["EMERGE_STATE_ROOT"] = str(exec_root)
    proc = subprocess.run(
        ["python3", str(ROOT / "hooks" / "pre_compact.py")],
        input="{}",
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    out = json.loads(proc.stdout.strip())
    ctx = out["systemMessage"]
    assert "Muscle memory" in ctx
    assert "Stable (auto-bridge): lark.read.get-doc" in ctx
    assert "Canary: lark.read.list-records" in ctx
    assert "Recent: lark.read.get-doc 1ok/0fail" in ctx


def test_pre_compact_prefers_cached_deep_reflection(tmp_path):
    hook_state = tmp_path / "hook-state"
    hook_state.mkdir()
    (hook_state / "state.json").write_text("{}", encoding="utf-8")
    exec_root = tmp_path / "exec-state"
    _seed_span_reflection_data(exec_root)
    cache_dir = exec_root / "reflection-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "global.json").write_text(
        json.dumps(
            {
                "generated_at_ms": 9999999999999,
                "summary_text": "Muscle memory (deep)\nHigh-confidence intents: cached.intent",
                "meta": {"builder": "test"},
            }
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_DATA"] = str(hook_state)
    env["EMERGE_STATE_ROOT"] = str(exec_root)
    proc = subprocess.run(
        ["python3", str(ROOT / "hooks" / "pre_compact.py")],
        input="{}",
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    out = json.loads(proc.stdout.strip())
    ctx = out["systemMessage"]
    assert "Muscle memory (deep)" in ctx
    assert "cached.intent" in ctx


def test_user_prompt_submit_reflection_at_turn_threshold(tmp_path):
    hook_state = tmp_path / "hook-state"
    hook_state.mkdir()
    (hook_state / "state.json").write_text(json.dumps({"turn_count": 19}), encoding="utf-8")
    exec_root = tmp_path / "exec-state"
    _seed_span_reflection_data(exec_root)
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_DATA"] = str(hook_state)
    env["EMERGE_STATE_ROOT"] = str(exec_root)
    proc = subprocess.run(
        ["python3", str(ROOT / "hooks" / "user_prompt_submit.py")],
        input=json.dumps({}),
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    out = json.loads(proc.stdout.strip())
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "Muscle memory" in ctx
    state_after = json.loads((hook_state / "state.json").read_text(encoding="utf-8"))
    assert state_after.get("turn_count") == 20


def test_user_prompt_submit_no_reflection_before_threshold(tmp_path):
    hook_state = tmp_path / "hook-state"
    hook_state.mkdir()
    (hook_state / "state.json").write_text(json.dumps({"turn_count": 18}), encoding="utf-8")
    exec_root = tmp_path / "exec-state"
    _seed_span_reflection_data(exec_root)
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_DATA"] = str(hook_state)
    env["EMERGE_STATE_ROOT"] = str(exec_root)
    proc = subprocess.run(
        ["python3", str(ROOT / "hooks" / "user_prompt_submit.py")],
        input=json.dumps({}),
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    out = json.loads(proc.stdout.strip())
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "Muscle memory" not in ctx
    state_after = json.loads((hook_state / "state.json").read_text(encoding="utf-8"))
    assert state_after.get("turn_count") == 19


def test_build_reflection_cache_script_writes_cache(tmp_path):
    hook_state = tmp_path / "hook-state"
    hook_state.mkdir()
    exec_root = tmp_path / "exec-state"
    _seed_span_reflection_data(exec_root)
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_DATA"] = str(hook_state)
    env["EMERGE_STATE_ROOT"] = str(exec_root)
    proc = subprocess.run(
        ["python3", str(ROOT / "scripts" / "build_reflection_cache.py")],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert "Reflection cache written." in proc.stdout
    cache_file = exec_root / "reflection-cache" / "global.json"
    assert cache_file.exists()
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    assert "summary_text" in data
    assert "Muscle memory (deep)" in data["summary_text"]


def test_tool_audit_excludes_emerge_icc_tools(tmp_path: Path):
    """tool_audit.py must tolerate being called for emerge tools and produce valid JSON.

    In production the hooks.json matcher prevents this, but defensive check ensures
    the script doesn't crash if called accidentally.
    """
    out = _run(
        "tool_audit.py",
        {
            "tool_name": "mcp__plugin_emerge__icc_exec",
            "tool_input": {"intent_signature": "test.read.foo"},
            "tool_response": {},
            "hook_event_name": "PostToolUse",
        },
        tmp_path,
    )
    result = json.loads(out)
    # Must be valid JSON — no crash
    assert isinstance(result, dict)
