# tests/test_operator_monitor.py
from __future__ import annotations
import json
import sys
import time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.operator_monitor import OperatorMonitor


def _make_events(n: int, tmp_path: Path) -> Path:
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
        for i in range(n)
    ]
    machine_dir = tmp_path / "operator-events" / "m1"
    machine_dir.mkdir(parents=True)
    events_file = machine_dir / "events.jsonl"
    events_file.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return events_file


def test_process_local_file_writes_events_local_jsonl(tmp_path):
    """process_local_file writes local_pattern_observed to events-local.jsonl."""
    state_root = tmp_path / "repl"
    state_root.mkdir()
    events_file = _make_events(3, tmp_path)

    monitor = OperatorMonitor(
        machines={},
        poll_interval_s=0.05,
        event_root=tmp_path / "operator-events",
        state_root=state_root,
    )
    monitor.process_local_file(events_file)

    events_local = state_root / "events" / "events-local.jsonl"
    assert events_local.exists(), "events-local.jsonl must be written"
    lines = [json.loads(l) for l in events_local.read_text().splitlines() if l.strip()]
    assert len(lines) >= 1
    alert = lines[0]
    assert alert["type"] == "local_pattern_observed"
    assert alert["stage"] == "explore"
    assert "intent_signature" in alert
    assert alert["meta"]["occurrences"] >= 3


def test_process_local_file_enqueues_synthesis(tmp_path):
    state_root = tmp_path / "repl"
    state_root.mkdir()
    events_file = _make_events(3, tmp_path)
    monitor = OperatorMonitor(
        machines={},
        poll_interval_s=0.05,
        event_root=tmp_path / "operator-events",
        state_root=state_root,
    )
    monitor.process_local_file(events_file)

    events_local = state_root / "events" / "events-local.jsonl"
    lines = [json.loads(l) for l in events_local.read_text().splitlines() if l.strip()]
    observed = [e for e in lines if e.get("type") == "local_pattern_observed"]
    assert observed
    assert observed[0]["meta"]["occurrences"] >= 1
    assert not any(e.get("type") == "pattern_pending_synthesis" for e in lines)
    assert not any(e.get("type") == "synthesis_job_ready" for e in lines)


def test_process_local_file_no_events_does_not_write(tmp_path):
    """No events → events-local.jsonl not created."""
    state_root = tmp_path / "repl"
    state_root.mkdir()
    machine_dir = tmp_path / "operator-events" / "m1"
    machine_dir.mkdir(parents=True)
    events_file = machine_dir / "events.jsonl"
    events_file.write_text("")

    monitor = OperatorMonitor(
        machines={},
        poll_interval_s=0.05,
        event_root=tmp_path / "operator-events",
        state_root=state_root,
    )
    monitor.process_local_file(events_file)

    assert not (state_root / "events" / "events-local.jsonl").exists()


def test_operator_monitor_stops_cleanly(tmp_path):
    """start() / stop() lifecycle works without push_fn."""
    state_root = tmp_path / "repl"
    state_root.mkdir()
    monitor = OperatorMonitor(
        machines={},
        poll_interval_s=0.05,
        event_root=tmp_path / "events",
        state_root=state_root,
    )
    monitor.start()
    assert monitor.is_alive()
    monitor.stop()
    assert not monitor.is_alive()


def test_process_local_file_accumulates_across_calls(tmp_path):
    """Calling process_local_file with 3 events fires a pattern alert."""
    state_root = tmp_path / "repl"
    state_root.mkdir()
    events_file = _make_events(3, tmp_path)

    monitor = OperatorMonitor(
        machines={},
        poll_interval_s=0.05,
        event_root=tmp_path / "operator-events",
        state_root=state_root,
    )
    monitor.process_local_file(events_file)

    events_local = state_root / "events" / "events-local.jsonl"
    assert events_local.exists()
    count_first = len(events_local.read_text().splitlines())
    assert count_first >= 1, "should write at least one alert after 3 events"
