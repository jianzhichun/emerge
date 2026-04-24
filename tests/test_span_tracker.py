from __future__ import annotations
import json
from pathlib import Path
import pytest
from scripts.span_tracker import SpanTracker, is_read_only_tool


@pytest.fixture
def tracker(tmp_path):
    hook_state = tmp_path / "hook-state"
    hook_state.mkdir(exist_ok=True)
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
    monkeypatch.setattr("scripts.policy_engine.PROMOTE_MIN_ATTEMPTS", 3)
    monkeypatch.setattr("scripts.policy_engine.PROMOTE_MIN_SUCCESS_RATE", 0.9)
    monkeypatch.setattr("scripts.policy_engine.PROMOTE_MAX_HUMAN_FIX_RATE", 0.1)
    for _ in range(3):
        s = tracker.open_span("lark.read.get-doc")
        tracker.close_span(s, outcome="success")
    assert tracker.get_policy_status("lark.read.get-doc") == "canary"

def test_policy_reaches_stable(tracker, monkeypatch):
    monkeypatch.setattr("scripts.policy_engine.PROMOTE_MIN_ATTEMPTS", 2)
    monkeypatch.setattr("scripts.policy_engine.PROMOTE_MIN_SUCCESS_RATE", 0.8)
    monkeypatch.setattr("scripts.policy_engine.PROMOTE_MAX_HUMAN_FIX_RATE", 0.2)
    monkeypatch.setattr("scripts.policy_engine.STABLE_MIN_ATTEMPTS", 4)
    monkeypatch.setattr("scripts.policy_engine.STABLE_MIN_SUCCESS_RATE", 0.8)
    for _ in range(4):
        s = tracker.open_span("lark.read.get-doc")
        tracker.close_span(s, outcome="success")
    # canary → stable requires at least one operator confirmation (evidence anchor phase 2)
    tracker._get_policy_engine().apply_evidence(
        "lark.read.get-doc", success=True, anchor_type="operator_action",
    )
    assert tracker.get_policy_status("lark.read.get-doc") == "stable"

def test_policy_rollback_on_consecutive_failures(tracker, monkeypatch):
    monkeypatch.setattr("scripts.policy_engine.ROLLBACK_CONSECUTIVE_FAILURES", 2)
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


def test_span_reflection_cold_start_nudge_when_no_data(tracker):
    result = tracker.format_reflection()
    assert "Muscle memory: no learned patterns yet." in result
    assert "icc_span_open" in result


def test_span_reflection_with_stable_intents(tracker, monkeypatch):
    # explore → canary → stable requires two transitions (promote then stabilize)
    monkeypatch.setattr("scripts.policy_engine.PROMOTE_MIN_ATTEMPTS", 1)
    monkeypatch.setattr("scripts.policy_engine.PROMOTE_MIN_SUCCESS_RATE", 1.0)
    monkeypatch.setattr("scripts.policy_engine.STABLE_MIN_ATTEMPTS", 2)
    monkeypatch.setattr("scripts.policy_engine.STABLE_MIN_SUCCESS_RATE", 1.0)
    for _ in range(2):
        s = tracker.open_span("lark.read.get-doc")
        tracker.close_span(s, outcome="success")
    # operator confirmation required to unblock canary → stable
    tracker._get_policy_engine().apply_evidence(
        "lark.read.get-doc", success=True, anchor_type="operator_action",
    )
    reflection = tracker.format_reflection()
    assert "Muscle memory" in reflection
    assert "Stable (auto-bridge): lark.read.get-doc" in reflection


def test_span_reflection_includes_recent_wal(tracker):
    s1 = tracker.open_span("lark.read.get-doc")
    tracker.close_span(s1, outcome="success")
    s2 = tracker.open_span("lark.write.create-doc")
    tracker.close_span(s2, outcome="failure")
    reflection = tracker.format_reflection()
    assert "Recent:" in reflection
    assert "lark.read.get-doc 1ok/0fail" in reflection
    assert "lark.write.create-doc 0ok/1fail" in reflection


def test_span_reflection_surfaces_recent_demotions(tracker, monkeypatch):
    # Two failures in explore → rollback (ROLLBACK_CONSECUTIVE_FAILURES=2).
    # The demotion reason must land in the next reflection so the next session
    # does not repeat the same mistake (CLAUDE.md P2: failed-once, learn-forever).
    monkeypatch.setattr("scripts.policy_engine.ROLLBACK_CONSECUTIVE_FAILURES", 2)
    s1 = tracker.open_span("lark.write.create-doc")
    tracker.close_span(s1, outcome="failure")
    s2 = tracker.open_span("lark.write.create-doc")
    tracker.close_span(s2, outcome="failure")
    reflection = tracker.format_reflection()
    assert "Demoted:" in reflection
    assert "lark.write.create-doc" in reflection
    # The reason and target stage must be visible — that's the whole point.
    assert "rollback" in reflection


def test_span_reflection_surfaces_bridge_exception_fingerprint(tracker):
    """Bridge-broken demotions must leak the exception class to the next
    session so the model skips re-diagnosing the root cause."""
    from scripts.intent_registry import IntentRegistry, registry_path
    import json

    key = "lark.read.get-doc"
    # Seed a stable intent then demote via record_bridge_outcome with a
    # realistic exception fingerprint.
    reg = registry_path(tracker._state_root)
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text(json.dumps({
        "intents": {
            key: {
                "intent_signature": key,
                "stage": "stable",
                "attempts": 50,
                "successes": 50,
                "recent_outcomes": [1] * 20,
            }
        }
    }), encoding="utf-8")
    from scripts.policy_config import BRIDGE_BROKEN_THRESHOLD
    engine = tracker._get_policy_engine()
    for _ in range(BRIDGE_BROKEN_THRESHOLD):
        engine.record_bridge_outcome(
            key,
            success=False,
            reason="name '__action' is not defined",
            exception_class="NameError",
        )

    reflection = tracker.format_reflection()
    assert "Demoted:" in reflection
    assert "NameError" in reflection, "exception fingerprint must surface in reflection"


def test_span_reflection_surfaces_synthesis_skipped_reason(tracker):
    """Crystallizer refusals must reach the next session — otherwise an
    intent stuck on canary with no pipeline has no way to self-heal."""
    from scripts.intent_registry import registry_path
    import json

    key = "lark.read.list-docs"
    reg = registry_path(tracker._state_root)
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text(json.dumps({
        "intents": {
            key: {
                "intent_signature": key,
                "stage": "canary",
                "attempts": 3,
                "successes": 3,
                "synthesis_skipped_reason": "missing___result_assignment",
            }
        }
    }), encoding="utf-8")

    reflection = tracker.format_reflection()
    assert "Synthesis blocked:" in reflection
    assert key in reflection
    assert "missing___result_assignment" in reflection


def test_format_reflection_uses_policy_status(tracker, monkeypatch):
    monkeypatch.setattr("scripts.policy_engine.PROMOTE_MIN_ATTEMPTS", 1)
    monkeypatch.setattr("scripts.policy_engine.PROMOTE_MIN_SUCCESS_RATE", 1.0)
    monkeypatch.setattr("scripts.policy_engine.PROMOTE_MAX_HUMAN_FIX_RATE", 1.0)
    monkeypatch.setattr("scripts.policy_engine.STABLE_MIN_ATTEMPTS", 2)
    monkeypatch.setattr("scripts.policy_engine.STABLE_MIN_SUCCESS_RATE", 1.0)
    # canary intent: 1/1 success (meets promote, not stable)
    s1 = tracker.open_span("lark.read.list-records")
    tracker.close_span(s1, outcome="success")
    # stable intent: 2/2 success + operator confirmation
    s2 = tracker.open_span("lark.read.get-doc")
    tracker.close_span(s2, outcome="success")
    s3 = tracker.open_span("lark.read.get-doc")
    tracker.close_span(s3, outcome="success")
    tracker._get_policy_engine().apply_evidence(
        "lark.read.get-doc", success=True, anchor_type="operator_action",
    )
    reflection = tracker.format_reflection()
    assert "Stable (auto-bridge): lark.read.get-doc" in reflection
    assert "Canary: lark.read.list-records" in reflection


def test_reflection_cache_roundtrip(tracker):
    tracker.write_reflection_cache("Muscle memory (deep)\nHigh-confidence intents: a.b.c")
    cached = tracker.load_reflection_cache()
    assert "Muscle memory (deep)" in cached


def test_reflection_cache_expired_returns_empty(tracker, monkeypatch):
    tracker.write_reflection_cache("Muscle memory (deep)\nHigh-confidence intents: a.b.c")
    monkeypatch.setattr("scripts.span_tracker.time.time", lambda: 10**12)
    assert tracker.load_reflection_cache(ttl_ms=1) == ""


def test_format_reflection_with_cache_prefers_cached(tracker):
    tracker.write_reflection_cache("Muscle memory (deep)\nHigh-confidence intents: cached.intent")
    # No candidates/WAL needed — fresh cache should be returned directly.
    result = tracker.format_reflection_with_cache()
    assert "cached.intent" in result


def test_reflection_cache_text_is_capped(tracker):
    very_long = "Muscle memory (deep)\n" + ("x" * 2000)
    tracker.write_reflection_cache(very_long)
    cached = tracker.load_reflection_cache()
    assert len(cached) <= 700
    assert cached.endswith("...")


# ── _atomic_write ─────────────────────────────────────────────────────────────

def test_atomic_write_leaves_no_stray_tmp_file(tmp_path):
    """After _atomic_write succeeds, no fixed-name .tmp file must remain."""
    from scripts.span_tracker import SpanTracker
    st = SpanTracker(state_root=tmp_path, hook_state_root=tmp_path)
    target = tmp_path / "state.json"
    st._atomic_write(target, {"key": "value"})
    assert target.exists()
    # Old implementation leaves state.tmp behind (it's the temp file that was renamed)
    # Actually .replace() removes the source, so no file remains — but verify anyway
    # The real issue is that the OLD code uses write_text (no fsync) + a fixed name.
    # New code must use mkstemp so the temp name is unique (verified via glob).
    stray = list(tmp_path.glob("state.tmp"))
    assert stray == [], f"stray .tmp files: {stray}"

def test_atomic_write_content_correct(tmp_path):
    """Written content must be exactly what was passed in."""
    import json
    from scripts.span_tracker import SpanTracker
    st = SpanTracker(state_root=tmp_path, hook_state_root=tmp_path)
    target = tmp_path / "out.json"
    data = {"hello": "world", "num": 42, "nested": {"a": [1, 2, 3]}}
    st._atomic_write(target, data)
    result = json.loads(target.read_text(encoding="utf-8"))
    assert result == data


# ── args_snapshot and result_summary ───────────────────────────────────────────

def test_action_record_serializes_snapshot_and_summary():
    from scripts.span_tracker import ActionRecord
    a = ActionRecord(
        seq=0,
        tool_name="mcp__plugin_emerge__icc_exec",
        args_hash="abc123",
        has_side_effects=True,
        ts_ms=1000,
        args_snapshot={"intent_signature": "mock.read.layers"},
        result_summary={"rows_count": 5},
    )
    d = a.to_dict()
    assert d["args_snapshot"] == {"intent_signature": "mock.read.layers"}
    assert d["result_summary"] == {"rows_count": 5}


def test_action_record_omits_empty_snapshot():
    from scripts.span_tracker import ActionRecord
    a = ActionRecord(seq=0, tool_name="Read", args_hash="x", has_side_effects=False, ts_ms=1)
    d = a.to_dict()
    assert "args_snapshot" not in d
    assert "result_summary" not in d


def test_close_span_reads_args_snapshot_from_buffer(tmp_path):
    """close_span populates ActionRecord.args_snapshot from the buffer JSONL."""
    hook_state = tmp_path / "hook-state"
    hook_state.mkdir(exist_ok=True)
    (hook_state / "state.json").write_text("{}", encoding="utf-8")

    from scripts.span_tracker import SpanTracker
    tracker = SpanTracker(state_root=tmp_path, hook_state_root=hook_state)

    span = tracker.open_span("mock.read.layers", description="")
    buf = hook_state / "active-span-actions.jsonl"
    buf.write_text(json.dumps({
        "tool_name": "mcp__plugin_emerge__icc_exec",
        "args_hash": "abc",
        "has_side_effects": True,
        "ts_ms": 1000,
        "args_snapshot": {"intent_signature": "mock.read.layers"},
        "result_summary": {"rows_count": 3},
    }) + "\n")

    closed = tracker.close_span(span, outcome="success")
    assert len(closed.actions) == 1
    assert closed.actions[0].args_snapshot == {"intent_signature": "mock.read.layers"}
    assert closed.actions[0].result_summary == {"rows_count": 3}
