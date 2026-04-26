from __future__ import annotations

import json
from pathlib import Path


def test_auto_crystallize_enqueues_forward_synthesis_and_does_not_write_pipeline(tmp_path, monkeypatch):
    from scripts.crystallizer import PipelineCrystallizer

    state_root = tmp_path / "state"
    connector_root = tmp_path / "connectors"
    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(connector_root))
    session = state_root / "sessions" / "s1"
    session.mkdir(parents=True)
    (session / "wal.jsonl").write_text(
        json.dumps(
            {
                "status": "success",
                "no_replay": False,
                "code": "__result = [{'x': 1}]",
                "finished_at_ms": 100,
                "metadata": {"intent_signature": "mock.read.auto"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    PipelineCrystallizer(state_root).auto_crystallize(
        intent_signature="mock.read.auto",
        connector="mock",
        pipeline_name="auto",
        mode="read",
    )

    assert not (connector_root / "mock" / "pipelines" / "read" / "auto.py").exists()
    events_path = state_root / "events" / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    assert [event["type"] for event in events] == [
        "crystallizer_deprecated",
        "forward_synthesis_pending",
    ]
    assert events[-1]["job"]["skill_name"] == "emerge-forward-synthesis"
