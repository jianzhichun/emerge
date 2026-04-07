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
