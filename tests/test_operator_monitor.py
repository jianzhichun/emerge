# tests/test_operator_monitor.py
from __future__ import annotations
import sys
import time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.operator_monitor import OperatorMonitor


class _FakeRunnerClient:
    """Simulates a remote runner that returns pre-seeded events."""
    def __init__(self, events: list[dict]):
        self._events = events

    def get_events(self, machine_id: str, since_ms: int = 0) -> list[dict]:
        return [e for e in self._events if e.get("ts_ms", 0) > since_ms]


def test_operator_monitor_detects_pattern_and_calls_push(tmp_path):
    push_calls = []

    def fake_push(stage: str, context: dict, summary) -> None:
        push_calls.append({"stage": stage, "context": context, "summary": summary})

    now_ms = int(time.time() * 1000)
    events = [
        {
            "ts_ms": now_ms - i * 60_000,
            "machine_id": "m1",
            "session_role": "operator",
            "event_type": "entity_added",
            "app": "zwcad",
            "payload": {"layer": "标注", "content": f"room_{i}"},
        }
        for i in range(3)
    ]

    monitor = OperatorMonitor(
        machines={"m1": _FakeRunnerClient(events)},
        push_fn=fake_push,
        poll_interval_s=0.05,
        event_root=tmp_path / "operator-events",
        adapter_root=tmp_path / "adapters",
    )
    monitor.start()
    time.sleep(0.3)
    monitor.stop()

    assert len(push_calls) >= 1
    assert push_calls[0]["stage"] == "explore"


def test_operator_monitor_does_not_fire_on_empty_events(tmp_path):
    push_calls = []

    monitor = OperatorMonitor(
        machines={"m1": _FakeRunnerClient([])},
        push_fn=lambda s, c, x: push_calls.append(1),
        poll_interval_s=0.05,
        event_root=tmp_path / "events",
        adapter_root=tmp_path / "adapters",
    )
    monitor.start()
    time.sleep(0.2)
    monitor.stop()

    assert push_calls == []


def test_operator_monitor_stops_cleanly(tmp_path):
    monitor = OperatorMonitor(
        machines={},
        push_fn=lambda s, c, x: None,
        poll_interval_s=0.05,
        event_root=tmp_path / "events",
        adapter_root=tmp_path / "adapters",
    )
    monitor.start()
    assert monitor.is_alive()
    monitor.stop()
    monitor.join(timeout=1.0)
    assert not monitor.is_alive()
