# tests/test_pattern_detector.py
from __future__ import annotations
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import time
from scripts.pattern_detector import PatternDetector, PatternSummary


def _event(app: str, event_type: str, layer: str = "标注", content: str = "room", ts_delta_ms: int = 0):
    return {
        "ts_ms": int(time.time() * 1000) + ts_delta_ms,
        "machine_id": "test-machine",
        "session_id": "op_test",
        "session_role": "operator",
        "observer_type": "accessibility",
        "event_type": event_type,
        "app": app,
        "payload": {"layer": layer, "content": content},
    }


def test_frequency_detector_fires_at_threshold():
    detector = PatternDetector()
    events = [_event("zwcad", "entity_added", ts_delta_ms=i * 60_000) for i in range(3)]
    summaries = detector.ingest(events)
    assert len(summaries) == 1
    s = summaries[0]
    assert s.occurrences == 3
    assert s.detector_signals == ["frequency"]
    assert "zwcad" in s.intent_signature


def test_frequency_detector_does_not_fire_below_threshold():
    detector = PatternDetector()
    events = [_event("zwcad", "entity_added", ts_delta_ms=i * 60_000) for i in range(2)]
    summaries = detector.ingest(events)
    assert summaries == []


def test_monitor_sub_events_are_filtered():
    detector = PatternDetector()
    events = []
    for i in range(5):
        e = _event("zwcad", "entity_added", ts_delta_ms=i * 60_000)
        e["session_role"] = "monitor_sub"
        events.append(e)
    summaries = detector.ingest(events)
    assert summaries == []


def test_cross_machine_detector_fires():
    detector = PatternDetector()
    events = []
    for machine in ("m1", "m2"):
        for i in range(2):
            e = _event("zwcad", "entity_added", ts_delta_ms=i * 60_000)
            e["machine_id"] = machine
            events.append(e)
    summaries = detector.ingest(events)
    assert any("cross_machine" in s.detector_signals for s in summaries)


def test_pattern_summary_fields():
    detector = PatternDetector()
    events = [_event("zwcad", "entity_added", ts_delta_ms=i * 60_000) for i in range(3)]
    summaries = detector.ingest(events)
    s = summaries[0]
    assert isinstance(s, PatternSummary)
    assert s.machine_ids == ["test-machine"]
    assert isinstance(s.intent_signature, str)
    assert s.occurrences >= 3
    assert isinstance(s.window_minutes, float)
    assert isinstance(s.context_hint, dict)
    assert s.policy_stage == "explore"


def test_error_rate_detector_fires_on_high_undo():
    detector = PatternDetector()
    events = []
    # 5 ops, 3 undos → ratio 0.6 > threshold 0.4
    for i in range(5):
        events.append(_event("zwcad", "entity_added", ts_delta_ms=i * 10_000))
    for i in range(3):
        events.append(_event("zwcad", "undo", ts_delta_ms=(5 + i) * 10_000))
    summaries = detector.ingest(events)
    assert any("error_rate" in s.detector_signals for s in summaries)
