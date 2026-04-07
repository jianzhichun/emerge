from scripts.state_tracker import StateTracker, LEVEL_CORE_CRITICAL


def test_add_delta_with_intent_signature():
    tracker = StateTracker()
    delta_id = tracker.add_delta(
        message="exec zwcad.write.apply-change",
        level=LEVEL_CORE_CRITICAL,
        intent_signature="zwcad.write.apply-change",
        tool_name="icc_exec",
    )
    deltas = tracker.to_dict()["deltas"]
    assert len(deltas) == 1
    assert deltas[0]["intent_signature"] == "zwcad.write.apply-change"
    assert deltas[0]["tool_name"] == "icc_exec"
    assert deltas[0]["ts_ms"] > 0


def test_add_delta_without_intent_defaults_to_none():
    tracker = StateTracker()
    tracker.add_delta(message="generic tool call")
    deltas = tracker.to_dict()["deltas"]
    assert deltas[0]["intent_signature"] is None
    assert deltas[0]["tool_name"] is None
    assert deltas[0]["ts_ms"] > 0


def test_normalize_state_fills_missing_delta_fields():
    raw = {
        "goal": "",
        "goal_source": "unset",
        "open_risks": [],
        "deltas": [
            {
                "id": "d-1",
                "message": "old delta",
                "level": "core_critical",
                "verification_state": "verified",
                "provisional": False,
            }
        ],
        "verification_state": "verified",
        "consistency_window_ms": 0,
    }
    tracker = StateTracker(state=raw)
    d = tracker.to_dict()["deltas"][0]
    assert d["intent_signature"] is None
    assert d["tool_name"] is None
    assert d["ts_ms"] == 0


def test_add_risk_creates_object():
    tracker = StateTracker()
    tracker.add_risk("pipeline verification failed")
    risks = tracker.to_dict()["open_risks"]
    assert len(risks) == 1
    assert isinstance(risks[0], dict)
    assert risks[0]["text"] == "pipeline verification failed"
    assert risks[0]["status"] == "open"
    assert "risk_id" in risks[0]
    assert risks[0]["created_at_ms"] > 0


def test_add_risk_dedup_by_text():
    tracker = StateTracker()
    tracker.add_risk("same risk")
    tracker.add_risk("same risk")
    assert len(tracker.to_dict()["open_risks"]) == 1


def test_normalize_state_migrates_bare_string_risks():
    raw = {
        "goal": "",
        "goal_source": "unset",
        "open_risks": ["bare risk string", "another risk"],
        "deltas": [],
        "verification_state": "verified",
        "consistency_window_ms": 0,
    }
    tracker = StateTracker(state=raw)
    risks = tracker.to_dict()["open_risks"]
    assert len(risks) == 2
    assert risks[0]["text"] == "bare risk string"
    assert risks[0]["status"] == "open"
    assert isinstance(risks[0]["risk_id"], str)


def test_update_risk_handle():
    tracker = StateTracker()
    tracker.add_risk("test risk")
    risk_id = tracker.to_dict()["open_risks"][0]["risk_id"]
    tracker.update_risk(risk_id, action="handle", reason="manually resolved")
    r = tracker.to_dict()["open_risks"][0]
    assert r["status"] == "handled"
    assert r["handled_reason"] == "manually resolved"


def test_update_risk_snooze():
    tracker = StateTracker()
    tracker.add_risk("snooze risk")
    risk_id = tracker.to_dict()["open_risks"][0]["risk_id"]
    tracker.update_risk(risk_id, action="snooze", snooze_duration_ms=3600000)
    r = tracker.to_dict()["open_risks"][0]
    assert r["status"] == "snoozed"
    assert r["snoozed_until_ms"] > 0


def test_format_context_uses_risk_text():
    tracker = StateTracker()
    tracker.add_risk("a real risk")
    ctx = tracker.format_context()
    assert "a real risk" in ctx["Open Risks"]
