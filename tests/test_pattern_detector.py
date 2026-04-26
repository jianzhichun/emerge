from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.pattern_detector import PatternDetector, PatternSummary


def _event(app: str, event_type: str, layer: str = "annotation", content: str = "room", ts_delta_ms: int = 0):
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


def test_frequency_facts_include_counts_without_threshold_decision():
    detector = PatternDetector()
    facts = detector.ingest([_event("mock", "entity_added") for _ in range(2)])

    frequency_facts = [fact for fact in facts if "frequency_metric" in fact.detector_signals]
    assert len(frequency_facts) == 1
    fact = frequency_facts[0]
    assert isinstance(fact, PatternSummary)
    assert fact.occurrences == 2
    assert fact.detector_signals == ["frequency_metric"]
    assert fact.intent_signature == "mock.entity_added.annotation"
    assert fact.context_hint["threshold_met"] is False


def test_monitor_sub_events_are_filtered_from_facts():
    detector = PatternDetector()
    events = []
    for _ in range(5):
        event = _event("mock", "entity_added")
        event["session_role"] = "monitor_sub"
        events.append(event)

    assert detector.ingest(events) == []


def test_error_rate_fact_reports_ratio_without_triggering_alert():
    detector = PatternDetector()
    events = [_event("mock", "entity_added", ts_delta_ms=i * 10_000) for i in range(5)]
    events += [_event("mock", "undo", ts_delta_ms=(5 + i) * 10_000) for i in range(3)]

    facts = detector.ingest(events)
    error_facts = [fact for fact in facts if "error_rate_metric" in fact.detector_signals]

    assert error_facts
    assert error_facts[0].context_hint["undo_ratio"] == 0.6
    assert error_facts[0].context_hint["threshold_met"] is True


def test_cross_machine_fact_reports_machine_distribution():
    detector = PatternDetector()
    events = []
    for machine in ("m1", "m2"):
        for _ in range(2):
            event = _event("mock", "entity_added")
            event["machine_id"] = machine
            events.append(event)

    facts = detector.ingest(events)
    cross = [fact for fact in facts if "cross_machine_metric" in fact.detector_signals]

    assert cross
    assert cross[0].context_hint["machine_counts"] == {"m1": 2, "m2": 2}
    assert cross[0].context_hint["threshold_met"] is True


def test_detector_ignores_old_events_for_windowed_metrics():
    detector = PatternDetector()
    old = -PatternDetector.FREQ_WINDOW_MS - 60_000
    events = [_event("mock", "entity_added", ts_delta_ms=old - i * 1000) for i in range(3)]

    assert detector.ingest(events) == []
