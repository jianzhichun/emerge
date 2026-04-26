from __future__ import annotations

import json
from pathlib import Path

from scripts.pattern_detector import PatternSummary


def test_enqueue_reverse_synthesis_writes_fact_events_only(tmp_path):
    from scripts.synthesis_events import enqueue_reverse_synthesis

    event_path = tmp_path / "events.jsonl"
    summary = PatternSummary(
        machine_ids=["runner-a"],
        intent_signature="Mock.Read Thing!",
        occurrences=3,
        window_minutes=1.0,
        detector_signals=["frequency"],
        context_hint={"app": "mock"},
    )

    result = enqueue_reverse_synthesis(
        state_root=tmp_path / "state",
        connector_root=tmp_path / "connectors",
        summary=summary,
        runner_profile="local",
        events=[{"ts_ms": 1, "event_type": "read"}],
        event_path=event_path,
    )

    assert result["status"] == "enqueued"
    events = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
    assert [event["type"] for event in events] == ["pattern_pending_synthesis", "synthesis_job_ready"]
    assert events[0]["intent_signature"] == "mock.read_thing"
    assert events[1]["job"]["skill_name"] == "distill-from-pattern"


def test_enqueue_forward_synthesis_collects_wal_samples_without_writing_pipeline(tmp_path):
    from scripts.synthesis_events import enqueue_forward_synthesis

    session_dir = tmp_path / "state" / "sessions" / "default"
    session_dir.mkdir(parents=True)
    (session_dir / "wal.jsonl").write_text(
        json.dumps(
            {
                "status": "success",
                "no_replay": False,
                "finished_at_ms": 123,
                "code": "__result = [{'x': 1}]",
                "metadata": {
                    "intent_signature": "mock.read.thing",
                    "script_args": {"name": "a"},
                    "result_var_value": [{"x": 1}],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    event_path = tmp_path / "events.jsonl"

    result = enqueue_forward_synthesis(
        state_root=tmp_path / "state",
        connector_root=tmp_path / "connectors",
        intent_signature="mock.read.thing",
        connector="mock",
        pipeline_name="thing",
        mode="read",
        event_path=event_path,
    )

    assert result["status"] == "enqueued"
    assert result["samples"] == 1
    events = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
    assert events[0]["type"] == "forward_synthesis_pending"
    assert events[0]["job"]["skill_name"] == "crystallize-from-wal"
    assert not (tmp_path / "connectors" / "mock" / "pipelines").exists()
