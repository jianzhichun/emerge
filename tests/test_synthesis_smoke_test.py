from __future__ import annotations

import json
from pathlib import Path


def _events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_smoke_failure_does_not_write_pipeline_and_blocks_after_three(tmp_path):
    from scripts.synthesis_coordinator import SynthesisCoordinator

    state_root = tmp_path / "state"
    connector_root = tmp_path / "connectors"
    event_path = state_root / "events" / "events.jsonl"
    blocked: list[str] = []
    coordinator = SynthesisCoordinator(
        state_root=state_root,
        connector_root=connector_root,
        exec_tool=lambda _args: {"isError": True, "error": "NameError"},
        mark_blocked=lambda intent, reason: blocked.append(f"{intent}:{reason}"),
    )
    job = {
        "job_id": "fwd-fail",
        "normalized_intent": "mock.read.sheet",
        "source": "forward",
        "samples": [{"args": {"filename": "/tmp/a.xlsx"}}],
    }
    result = {
        "connector": "mock",
        "mode": "read",
        "pipeline_name": "sheet",
        "code": "__result = missing_name",
    }

    for _ in range(3):
        response = coordinator.submit_synthesis_result(job=job, result=result, event_path=event_path)
        assert response["status"] == "smoke_failed"

    assert not (connector_root / "mock" / "pipelines" / "read" / "_pending" / "sheet.py").exists()
    event_types = [event["type"] for event in _events(event_path)]
    assert event_types.count("forward_synthesis_smoke_failed") == 3
    assert event_types[-1] == "forward_synthesis_blocked"
    assert blocked == ["mock.read.sheet:smoke_failed"]
