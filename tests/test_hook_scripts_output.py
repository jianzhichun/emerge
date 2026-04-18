import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _run(script: str, payload: dict, data_dir: Path) -> str:
    env = os.environ.copy()
    env["EMERGE_HOOK_STATE_ROOT"] = str(data_dir)
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
    s_out = _run("session_start.py", {}, tmp_path)
    s_json = json.loads(s_out)
    assert s_json["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "additionalContext" in s_json["hookSpecificOutput"]

    u_out = _run("user_prompt_submit.py", {"budget_chars": 120}, tmp_path)
    u_json = json.loads(u_out)
    assert u_json["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "FLYWHEEL_TOKEN" in u_json["hookSpecificOutput"]["additionalContext"]
    token = _extract_flywheel_token(u_json["hookSpecificOutput"]["additionalContext"])
    assert token["schema_version"] == "flywheel.v1"
    assert "deltas" in token


def test_post_tool_use_and_pre_compact_contract(tmp_path: Path):
    p_out = _run(
        "post_tool_use.py",
        {
            "tool_name": "mcp__plugin_emerge__icc_exec",
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
    env["EMERGE_HOOK_STATE_ROOT"] = str(tmp_path)
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
    env.pop("EMERGE_HOOK_STATE_ROOT", None)
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
    env["EMERGE_HOOK_STATE_ROOT"] = str(tmp_path)
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
            "tool_name": "mcp__plugin_emerge__icc_exec",
            "tool_result": "not-a-dict",
            "delta_message": "write attempted",
        },
        tmp_path,
    )
    parsed = json.loads(out)
    assert parsed["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "additionalContext" in parsed["hookSpecificOutput"]


def test_pre_compact_emits_recovery_token(tmp_path: Path):
    # First seed some state via post_tool_use
    env = os.environ.copy()
    env["EMERGE_HOOK_STATE_ROOT"] = str(tmp_path)
    subprocess.run(
        ["python3", str(ROOT / "hooks" / "post_tool_use.py")],
        input=json.dumps({
            "tool_name": "mcp__plugin_emerge__icc_exec",
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
    assert len(ctx) <= 1500  # budget enforced (includes cold-start reflection nudge)


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
            "tool_name": "mcp__plugin_emerge__icc_exec",
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
            "tool_name": "mcp__plugin_emerge__icc_exec",
            "tool_result": {"isError": False, "content": [{"type": "text", "text": "ok"}]},
            "delta_message": "Write applied",
        },
        tmp_path,
    )
    parsed = json.loads(out)
    assert "updatedMCPToolOutput" not in parsed.get("hookSpecificOutput", {}), (
        "PostToolUse must not echo updatedMCPToolOutput when result is unchanged"
    )


def test_pre_compact_resets_tracker_state(tmp_path: Path):
    """After PreCompact, state.json deltas and risks are cleared."""
    env = os.environ.copy()
    env["EMERGE_HOOK_STATE_ROOT"] = str(tmp_path)

    # Seed some deltas via post_tool_use
    subprocess.run(
        ["python3", str(ROOT / "hooks" / "session_start.py")],
        input=json.dumps({}),
        capture_output=True, text=True, env=env, check=True,
    )
    subprocess.run(
        ["python3", str(ROOT / "hooks" / "post_tool_use.py")],
        input=json.dumps({
            "tool_name": "mcp__plugin_emerge__icc_exec",
            "tool_result": {"isError": False, "content": [{"type": "text", "text": "ok"}]},
            "delta_message": "Wrote mesh to HyperMesh",
        }),
        capture_output=True, text=True, env=env, check=True,
    )

    state_path = tmp_path / "state.json"
    before = json.loads(state_path.read_text())
    assert before["deltas"], "must have deltas before compaction"

    subprocess.run(
        ["python3", str(ROOT / "hooks" / "pre_compact.py")],
        input="{}",
        capture_output=True, text=True, env=env, check=True,
    )

    after = json.loads(state_path.read_text())
    assert after["deltas"] == [], "deltas must be cleared after PreCompact"
    assert after["open_risks"] == [], "open_risks must be cleared after PreCompact"


def test_session_start_clears_stale_active_span(tmp_path, monkeypatch):
    """SessionStart must clear active_span_id left by a crashed previous session."""
    import json, subprocess, sys
    hook_state = tmp_path / "hook-state"
    hook_state.mkdir()
    stale_state = {
        "active_span_id": "stale-uuid",
        "active_span_intent": "lark.read.get-doc",
        "deltas": [],
    }
    (hook_state / "state.json").write_text(json.dumps(stale_state), encoding="utf-8")
    monkeypatch.setenv("EMERGE_HOOK_STATE_ROOT", str(hook_state))

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
    env = {**os.environ, "EMERGE_HOOK_STATE_ROOT": str(hook_state)}
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
             "deltas": []}
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
             "deltas": []}
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
             "deltas": []}
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


def test_user_prompt_submit_no_longer_drains_pending_actions(tmp_path: Path):
    """UserPromptSubmit no longer drains pending-actions files (dispatch moved to events.jsonl)."""
    processed = tmp_path / "pending-actions.processed.json"
    processed.write_text(json.dumps({
        "submitted_at": 1000,
        "actions": [
            {"type": "core.tool-call", "call": {"tool": "icc_exec", "arguments": {"intent_signature": "a.read.b"}}, "meta": {}},
        ],
    }))
    out = _run("user_prompt_submit.py", {}, tmp_path)
    parsed = json.loads(out)
    ctx = parsed["hookSpecificOutput"]["additionalContext"]
    # Should NOT contain cockpit action text — delivery is via watch_emerge.py now
    assert "[Cockpit]" not in ctx
    # File should not be renamed or consumed
    assert processed.exists()


def test_watch_pending_emits_and_renames(tmp_path: Path):
    """watch_emerge.py tails events.jsonl and writes ack for cockpit_action events."""
    import subprocess, time, signal
    events_dir = tmp_path / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    events_file = events_dir / "events.jsonl"
    event_id = "cockpit-test-ack-1"
    env = os.environ.copy()
    env["EMERGE_STATE_ROOT"] = str(tmp_path)
    proc = subprocess.Popen(
        ["python3", str(ROOT / "scripts" / "watch_emerge.py")],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    time.sleep(0.3)
    events_file.parent.mkdir(parents=True, exist_ok=True)
    with events_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "type": "cockpit_action",
            "event_id": event_id,
            "ts_ms": int(time.time() * 1000),
            "actions": [{"type": "core.tool-call", "call": {"tool": "icc_exec", "arguments": {"intent_signature": "a.read.b"}}, "meta": {}}],
        }) + "\n")
    time.sleep(0.6)
    proc.send_signal(signal.SIGTERM)
    stdout, _ = proc.communicate(timeout=3)
    assert "[Cockpit]" in stdout
    assert "icc_exec" in stdout
    ack_path = events_dir / "cockpit-action-acks.jsonl"
    assert ack_path.exists()
    ack = json.loads(ack_path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert ack["event_id"] == event_id


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
    env["EMERGE_HOOK_STATE_ROOT"] = str(tmp_path)
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
            "type": "core.tool-call",
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
    actions = [{"type": "intent.set", "key": "foo", "fields": {"stable": True}}]
    result = format_pending_actions(actions)
    assert "intent.set foo" in result


def test_format_pending_actions_notes_edit():
    from scripts.pending_actions import format_pending_actions
    actions = [{"type": "notes.edit", "connector": "gmail"}]
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
    env["EMERGE_HOOK_STATE_ROOT"] = str(tmp_path)
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


# ── Span Protocol injection tests ────────────────────────────────────────────


def test_session_start_includes_span_protocol(tmp_path, monkeypatch):
    import json, subprocess, sys
    hook_state = tmp_path / "hook-state"
    hook_state.mkdir()
    (hook_state / "state.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("EMERGE_HOOK_STATE_ROOT", str(hook_state))
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
    env["EMERGE_HOOK_STATE_ROOT"] = str(tmp_path)
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
    env["EMERGE_HOOK_STATE_ROOT"] = str(tmp_path)
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
    (exec_root / "registry").mkdir(parents=True, exist_ok=True)
    (exec_root / "registry" / "intents.json").write_text(
        json.dumps(
            {
                "intents": {
                    "lark.read.get-doc": {
                        "stage": "stable",
                        "attempts": 40,
                        "successes": 40,
                        "human_fixes": 0,
                        "consecutive_failures": 0,
                        "recent_outcomes": [1] * 20,
                        "last_ts_ms": 1000,
                    },
                    "lark.read.list-records": {
                        "stage": "canary",
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
    env["EMERGE_HOOK_STATE_ROOT"] = str(hook_state)
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
    env["EMERGE_HOOK_STATE_ROOT"] = str(hook_state)
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
    (hook_state / "state.json").write_text(json.dumps({"turn_count": 0}), encoding="utf-8")
    exec_root = tmp_path / "exec-state"
    _seed_span_reflection_data(exec_root)
    env = os.environ.copy()
    env["EMERGE_HOOK_STATE_ROOT"] = str(hook_state)
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
    assert state_after.get("turn_count") == 1


def test_user_prompt_submit_span_reminder_at_interval(tmp_path):
    """Every SPAN_REMINDER_INTERVAL turns (turn > 1, no active span) a reminder is injected."""
    hook_state = tmp_path / "hook-state"
    hook_state.mkdir()
    # turn_count=4 → becomes 5 after increment → 5 % 5 == 0 → reminder fires
    (hook_state / "state.json").write_text(json.dumps({"turn_count": 4}), encoding="utf-8")
    exec_root = tmp_path / "exec-state"
    env = os.environ.copy()
    env["EMERGE_HOOK_STATE_ROOT"] = str(hook_state)
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
    assert "[Span] No active span" in ctx


def test_user_prompt_submit_no_reminder_when_span_active(tmp_path):
    """Span reminder is suppressed when a span is already open."""
    hook_state = tmp_path / "hook-state"
    hook_state.mkdir()
    (hook_state / "state.json").write_text(
        json.dumps({"turn_count": 4, "active_span_id": "span-abc", "active_span_intent": "lark.read.foo"}),
        encoding="utf-8",
    )
    exec_root = tmp_path / "exec-state"
    env = os.environ.copy()
    env["EMERGE_HOOK_STATE_ROOT"] = str(hook_state)
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
    assert "[Span] No active span" not in ctx


def test_build_reflection_cache_script_writes_cache(tmp_path):
    hook_state = tmp_path / "hook-state"
    hook_state.mkdir()
    exec_root = tmp_path / "exec-state"
    _seed_span_reflection_data(exec_root)
    env = os.environ.copy()
    env["EMERGE_HOOK_STATE_ROOT"] = str(hook_state)
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


def test_tool_audit_span_nudge_fires_once_for_non_read_only_tool(tmp_path: Path):
    """First non-trivial tool call without a span injects a one-shot nudge via flag file."""
    state = tmp_path / "state.json"
    state.write_text(json.dumps({}), encoding="utf-8")
    out = _run(
        "tool_audit.py",
        {
            "tool_name": "Bash",
            "tool_input": {"command": "git status"},
            "tool_response": {},
            "hook_event_name": "PostToolUse",
        },
        tmp_path,
    )
    result = json.loads(out)
    ctx = result.get("hookSpecificOutput", {}).get("additionalContext", "")
    assert "Span nudge" in ctx
    assert "icc_span_open" in ctx
    # Flag file created — state.json must NOT have been modified
    assert (tmp_path / "span-nudge-sent").exists()
    state_after = json.loads(state.read_text(encoding="utf-8"))
    assert "_span_nudge_sent" not in state_after
    # Second call: flag file exists → no nudge
    out2 = _run(
        "tool_audit.py",
        {
            "tool_name": "Bash",
            "tool_input": {"command": "git log"},
            "tool_response": {},
            "hook_event_name": "PostToolUse",
        },
        tmp_path,
    )
    result2 = json.loads(out2)
    ctx2 = result2.get("hookSpecificOutput", {}).get("additionalContext", "")
    assert "Span nudge" not in ctx2


def test_tool_audit_no_nudge_for_read_only_tool(tmp_path: Path):
    """Read-only tools (Grep, Glob, Read) never trigger the span nudge."""
    state = tmp_path / "state.json"
    state.write_text(json.dumps({}), encoding="utf-8")
    out = _run(
        "tool_audit.py",
        {
            "tool_name": "Grep",
            "tool_input": {"pattern": "foo"},
            "tool_response": {},
            "hook_event_name": "PostToolUse",
        },
        tmp_path,
    )
    result = json.loads(out)
    ctx = result.get("hookSpecificOutput", {}).get("additionalContext", "")
    assert "Span nudge" not in ctx
    # No flag file for read-only tools
    assert not (tmp_path / "span-nudge-sent").exists()


def test_tool_audit_no_nudge_when_span_active(tmp_path: Path):
    """No nudge when an active span is already open."""
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"active_span_id": "span-xyz", "active_span_intent": "lark.read.foo"}), encoding="utf-8")
    out = _run(
        "tool_audit.py",
        {
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "tool_response": {},
            "hook_event_name": "PostToolUse",
        },
        tmp_path,
    )
    result = json.loads(out)
    ctx = result.get("hookSpecificOutput", {}).get("additionalContext", "")
    assert "Span nudge" not in ctx


def test_tool_audit_delta_written_to_tool_deltas_jsonl(tmp_path: Path):
    """With active span, delta goes to tool-deltas.jsonl; state.json is NOT modified."""
    state = tmp_path / "state.json"
    original_state = {"active_span_id": "span-abc", "active_span_intent": "lark.read.get-doc"}
    state.write_text(json.dumps(original_state), encoding="utf-8")
    _run(
        "tool_audit.py",
        {
            "tool_name": "Bash",
            "tool_input": {"command": "ls -la"},
            "tool_response": {},
            "hook_event_name": "PostToolUse",
        },
        tmp_path,
    )
    # Delta written to separate file
    deltas_path = tmp_path / "tool-deltas.jsonl"
    assert deltas_path.exists()
    entries = [json.loads(line) for line in deltas_path.read_text().splitlines() if line.strip()]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["intent_signature"] == "lark.read.get-doc"
    assert entry["level"] == "peripheral"
    assert "Bash" in entry["message"]
    # state.json must be identical — no "deltas" key injected
    state_after = json.loads(state.read_text(encoding="utf-8"))
    assert state_after == original_state


def test_cwd_changed_emits_system_message(tmp_path: Path):
    """CwdChanged outputs systemMessage when CWD shifts to a new project."""
    out = _run(
        "cwd_changed.py",
        {
            "hook_event_name": "CwdChanged",
            "old_cwd": "/Users/alice/projects/emerge",
            "new_cwd": "/Users/alice/projects/other-project",
            "cwd": "/Users/alice/projects/other-project",
        },
        tmp_path,
    )
    result = json.loads(out)
    # CwdChanged uses top-level systemMessage
    assert "systemMessage" in result
    assert "hookSpecificOutput" not in result
    assert "other-project" in result["systemMessage"]


def test_cwd_changed_same_dir_emits_empty(tmp_path: Path):
    """CwdChanged with same old and new CWD emits empty object."""
    out = _run(
        "cwd_changed.py",
        {
            "hook_event_name": "CwdChanged",
            "old_cwd": "/Users/alice/projects/emerge",
            "new_cwd": "/Users/alice/projects/emerge",
            "cwd": "/Users/alice/projects/emerge",
        },
        tmp_path,
    )
    result = json.loads(out)
    assert result == {}


import os as _os


def test_elicitation_ci_mode_auto_accepts_span_approve(tmp_path: Path):
    """In EMERGE_CI=1 mode, Elicitation hook auto-accepts icc_span_approve elicitation."""
    env_backup = _os.environ.copy()
    try:
        _os.environ["EMERGE_CI"] = "1"
        _os.environ["EMERGE_HOOK_STATE_ROOT"] = str(tmp_path)
        out = _run(
            "elicitation.py",
            {
                "hook_event_name": "Elicitation",
                "mcp_server_name": "plugin_emerge_emerge",
                "message": "Activate pipeline `lark.read.get-doc`?\nThis will move from _pending/ to ...",
                "mode": "form",
                "elicitation_id": "elicit-abc123",
                "requested_schema": {
                    "type": "object",
                    "properties": {"confirmed": {"type": "boolean"}},
                },
            },
            tmp_path,
        )
        result = json.loads(out)
        assert result["hookSpecificOutput"]["hookEventName"] == "Elicitation"
        assert result["hookSpecificOutput"]["action"] == "accept"
        assert result["hookSpecificOutput"]["content"]["confirmed"] is True
    finally:
        _os.environ.clear()
        _os.environ.update(env_backup)


def test_elicitation_non_ci_mode_passes_through(tmp_path: Path):
    """Without EMERGE_CI=1, Elicitation hook emits empty object (let CC show dialog)."""
    env_backup = _os.environ.copy()
    try:
        _os.environ.pop("EMERGE_CI", None)
        _os.environ["EMERGE_HOOK_STATE_ROOT"] = str(tmp_path)
        out = _run(
            "elicitation.py",
            {
                "hook_event_name": "Elicitation",
                "mcp_server_name": "plugin_emerge_emerge",
                "message": "Activate pipeline `lark.read.get-doc`?",
                "mode": "form",
                "elicitation_id": "elicit-abc123",
                "requested_schema": {"type": "object"},
            },
            tmp_path,
        )
        result = json.loads(out)
        # No override — empty output lets CC show the dialog normally
        assert result == {}
    finally:
        _os.environ.clear()
        _os.environ.update(env_backup)


def test_elicitation_ci_auto_accepts_reconcile(tmp_path: Path):
    """In EMERGE_CI=1, auto-accepts reconcile with outcome=confirm."""
    env_backup = _os.environ.copy()
    try:
        _os.environ["EMERGE_CI"] = "1"
        _os.environ["EMERGE_HOOK_STATE_ROOT"] = str(tmp_path)
        out = _run(
            "elicitation.py",
            {
                "hook_event_name": "Elicitation",
                "mcp_server_name": "plugin_emerge_emerge",
                "message": "Choose the reconciliation outcome for delta `delta-001`:",
                "mode": "form",
                "elicitation_id": "elicit-def456",
                "requested_schema": {
                    "type": "object",
                    "properties": {"outcome": {"type": "string"}},
                },
            },
            tmp_path,
        )
        result = json.loads(out)
        assert result["hookSpecificOutput"]["action"] == "accept"
        assert result["hookSpecificOutput"]["content"]["outcome"] == "confirm"
    finally:
        _os.environ.clear()
        _os.environ.update(env_backup)


def test_elicitation_result_writes_audit_log(tmp_path: Path):
    """ElicitationResult appends entry to elicitation-log.jsonl."""
    out = _run(
        "elicitation_result.py",
        {
            "hook_event_name": "ElicitationResult",
            "mcp_server_name": "plugin_emerge_emerge",
            "action": "accept",
            "content": {"confirmed": True},
            "mode": "form",
            "elicitation_id": "elicit-abc123",
        },
        tmp_path,
    )
    result = json.loads(out)
    # ElicitationResult uses top-level systemMessage or empty — never hookSpecificOutput
    assert "hookSpecificOutput" not in result
    # Audit log must have been written
    log_path = tmp_path / "elicitation-log.jsonl"
    assert log_path.exists()
    entry = json.loads(log_path.read_text().strip())
    assert entry["elicitation_id"] == "elicit-abc123"
    assert entry["action"] == "accept"
    assert entry["mcp_server_name"] == "plugin_emerge_emerge"


