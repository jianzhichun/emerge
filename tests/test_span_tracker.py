from __future__ import annotations
import json
from pathlib import Path
import pytest
from scripts.span_tracker import SpanTracker, is_read_only_tool


@pytest.fixture
def tracker(tmp_path):
    hook_state = tmp_path / "hook-state"
    hook_state.mkdir()
    (hook_state / "state.json").write_text("{}", encoding="utf-8")
    return SpanTracker(state_root=tmp_path, hook_state_root=hook_state)


# ── is_read_only_tool ─────────────────────────────────────────────────────────

def test_is_read_only_known_names():
    for name in ("Read", "Glob", "Grep", "WebFetch", "WebSearch", "ToolSearch"):
        assert is_read_only_tool(name) is True, name

def test_is_read_only_context7_prefix():
    assert is_read_only_tool("mcp__context7__query-docs") is True
    assert is_read_only_tool("mcp__context7__resolve-library-id") is True

def test_is_read_only_suffix_patterns():
    assert is_read_only_tool("mcp__lark_doc__get") is True
    assert is_read_only_tool("mcp__lark_base__list") is True
    assert is_read_only_tool("mcp__lark_drive__search") is True

def test_is_not_read_only_write_tools():
    assert is_read_only_tool("mcp__lark_doc__create") is False
    assert is_read_only_tool("mcp__lark_im__send") is False
    assert is_read_only_tool("Edit") is False
    assert is_read_only_tool("Write") is False
    assert is_read_only_tool("Bash") is False

def test_is_not_read_only_icc_exec():
    # icc_exec is excluded from span recording entirely — conservatively not read-only
    assert is_read_only_tool("emerge__icc_exec") is False


# ── open / close ──────────────────────────────────────────────────────────────

def test_open_writes_active_span_to_hook_state(tracker, tmp_path):
    hook_state = tmp_path / "hook-state"
    span = tracker.open_span("lark.read.get-doc", description="test")
    state = json.loads((hook_state / "state.json").read_text())
    assert state["active_span_id"] == span.span_id
    assert state["active_span_intent"] == "lark.read.get-doc"

def test_open_clears_action_buffer(tracker, tmp_path):
    hook_state = tmp_path / "hook-state"
    buf = hook_state / "active-span-actions.jsonl"
    buf.write_text("stale\n", encoding="utf-8")
    tracker.open_span("lark.read.get-doc")
    assert buf.read_text() == ""

def test_open_errors_when_span_already_active(tracker):
    tracker.open_span("lark.read.get-doc")
    with pytest.raises(RuntimeError, match="active span"):
        tracker.open_span("lark.read.other")

def test_close_writes_span_to_wal(tracker, tmp_path):
    span = tracker.open_span("lark.write.create-doc", args={"title": "T"})
    tracker.close_span(span, outcome="success", result_summary={"doc_id": "x"})
    wal = tmp_path / "span-wal" / "spans.jsonl"
    assert wal.exists()
    record = json.loads(wal.read_text().strip())
    assert record["intent_signature"] == "lark.write.create-doc"
    assert record["outcome"] == "success"
    assert record["result_summary"] == {"doc_id": "x"}

def test_close_clears_hook_state(tracker, tmp_path):
    hook_state = tmp_path / "hook-state"
    span = tracker.open_span("lark.read.get-doc")
    tracker.close_span(span, outcome="success")
    state = json.loads((hook_state / "state.json").read_text())
    assert "active_span_id" not in state
    assert "active_span_intent" not in state

def test_close_reads_actions_from_buffer(tracker, tmp_path):
    hook_state = tmp_path / "hook-state"
    span = tracker.open_span("lark.read.get-doc")
    buf = hook_state / "active-span-actions.jsonl"
    buf.write_text(
        json.dumps({"tool_name": "mcp__lark_doc__get", "args_hash": "abc",
                    "has_side_effects": False, "ts_ms": 1}) + "\n",
        encoding="utf-8",
    )
    tracker.close_span(span, outcome="success")
    wal = tmp_path / "span-wal" / "spans.jsonl"
    record = json.loads(wal.read_text().strip())
    assert len(record["actions"]) == 1
    assert record["actions"][0]["tool_name"] == "mcp__lark_doc__get"
    assert record["is_read_only"] is True

def test_close_is_not_read_only_when_any_side_effect(tracker, tmp_path):
    hook_state = tmp_path / "hook-state"
    span = tracker.open_span("lark.write.create-doc")
    buf = hook_state / "active-span-actions.jsonl"
    buf.write_text(
        json.dumps({"tool_name": "mcp__lark_doc__create", "args_hash": "abc",
                    "has_side_effects": True, "ts_ms": 1}) + "\n",
        encoding="utf-8",
    )
    tracker.close_span(span, outcome="success")
    record = json.loads((tmp_path / "span-wal" / "spans.jsonl").read_text().strip())
    assert record["is_read_only"] is False


# ── policy lifecycle ──────────────────────────────────────────────────────────

def test_policy_starts_explore(tracker):
    assert tracker.get_policy_status("lark.read.get-doc") == "explore"

def test_policy_reaches_canary(tracker, monkeypatch):
    monkeypatch.setattr("scripts.span_tracker.PROMOTE_MIN_ATTEMPTS", 3)
    monkeypatch.setattr("scripts.span_tracker.PROMOTE_MIN_SUCCESS_RATE", 0.9)
    monkeypatch.setattr("scripts.span_tracker.PROMOTE_MAX_HUMAN_FIX_RATE", 0.1)
    for _ in range(3):
        s = tracker.open_span("lark.read.get-doc")
        tracker.close_span(s, outcome="success")
    assert tracker.get_policy_status("lark.read.get-doc") == "canary"

def test_policy_reaches_stable(tracker, monkeypatch):
    monkeypatch.setattr("scripts.span_tracker.PROMOTE_MIN_ATTEMPTS", 2)
    monkeypatch.setattr("scripts.span_tracker.PROMOTE_MIN_SUCCESS_RATE", 0.8)
    monkeypatch.setattr("scripts.span_tracker.PROMOTE_MAX_HUMAN_FIX_RATE", 0.2)
    monkeypatch.setattr("scripts.span_tracker.STABLE_MIN_ATTEMPTS", 4)
    monkeypatch.setattr("scripts.span_tracker.STABLE_MIN_SUCCESS_RATE", 0.8)
    for _ in range(4):
        s = tracker.open_span("lark.read.get-doc")
        tracker.close_span(s, outcome="success")
    assert tracker.get_policy_status("lark.read.get-doc") == "stable"

def test_policy_rollback_on_consecutive_failures(tracker, monkeypatch):
    monkeypatch.setattr("scripts.span_tracker.ROLLBACK_CONSECUTIVE_FAILURES", 2)
    for _ in range(2):
        s = tracker.open_span("lark.read.get-doc")
        tracker.close_span(s, outcome="failure")
    assert tracker.get_policy_status("lark.read.get-doc") == "rollback"

def test_latest_successful_span_returns_most_recent(tracker):
    for i in range(3):
        s = tracker.open_span("lark.read.get-doc", args={"n": i})
        tracker.close_span(s, outcome="success", result_summary={"n": i})
    latest = tracker.latest_successful_span("lark.read.get-doc")
    assert latest is not None
    assert latest["result_summary"]["n"] == 2

def test_latest_successful_span_ignores_failures(tracker):
    s = tracker.open_span("lark.read.get-doc")
    tracker.close_span(s, outcome="failure")
    assert tracker.latest_successful_span("lark.read.get-doc") is None
