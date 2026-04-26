from __future__ import annotations

import json
from pathlib import Path


def _write_wal_entry(
    state_root: Path,
    session_id: str,
    *,
    intent_signature: str,
    code: str,
    ts: int,
    script_args: dict | None = None,
) -> None:
    session_dir = state_root / "sessions" / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "status": "success",
        "no_replay": False,
        "code": code,
        "finished_at_ms": ts,
        "metadata": {
            "intent_signature": intent_signature,
            "script_args": script_args or {},
        },
    }
    with (session_dir / "wal.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _read_events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_forward_synthesis_enqueue_collects_success_samples(tmp_path):
    from scripts.synthesis_coordinator import SynthesisCoordinator

    state_root = tmp_path / "state"
    connector_root = tmp_path / "connectors"
    _write_wal_entry(
        state_root,
        "s1",
        intent_signature="mock.read.sheet",
        code="__result = [{'file': __args['filename']}]",
        ts=100,
        script_args={"filename": "/tmp/a.xlsx"},
    )
    _write_wal_entry(
        state_root,
        "s2",
        intent_signature="mock.read.sheet",
        code="__result = [{'file': __args['filename']}]",
        ts=200,
        script_args={"filename": "/tmp/b.xlsx"},
    )

    event_path = state_root / "events" / "events.jsonl"
    coordinator = SynthesisCoordinator(
        state_root=state_root,
        connector_root=connector_root,
        exec_tool=lambda _args: {"isError": False},
    )

    result = coordinator.enqueue_forward_synthesis(
        intent_signature="mock.read.sheet",
        connector="mock",
        pipeline_name="sheet",
        mode="read",
        target_profile="default",
        event_path=event_path,
    )

    assert result["status"] == "enqueued"
    events = _read_events(event_path)
    assert events[-1]["type"] == "forward_synthesis_pending"
    job = events[-1]["job"]
    assert job["source"] == "forward"
    assert job["skill_name"] == "emerge-forward-synthesis"
    assert job["normalized_intent"] == "mock.read.sheet"
    assert [sample["args"]["filename"] for sample in job["samples"]] == [
        "/tmp/a.xlsx",
        "/tmp/b.xlsx",
    ]


def test_forward_synthesis_samples_include_real_exec_result_value(tmp_path, monkeypatch):
    from scripts.emerge_daemon import EmergeDaemon
    from scripts.synthesis_coordinator import SynthesisCoordinator

    state_root = tmp_path / "state"
    connector_root = tmp_path / "connectors"
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(state_root))
    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(connector_root))
    monkeypatch.setenv("EMERGE_SESSION_ID", "sample-result")

    daemon = EmergeDaemon(root=Path(__file__).resolve().parents[1])
    daemon.call_tool(
        "icc_exec",
        {
            "intent_signature": "mock.read.sheet",
            "code": "__result = [{'file': __args['filename']}]",
            "result_var": "__result",
            "script_args": {"filename": "/tmp/a.xlsx"},
        },
    )

    samples = SynthesisCoordinator(
        state_root=state_root,
        connector_root=connector_root,
        exec_tool=lambda _args: {"isError": False},
    ).collect_success_samples("mock.read.sheet")

    assert samples[-1]["result"] == [{"file": "/tmp/a.xlsx"}]
    assert samples[-1]["args"] == {"filename": "/tmp/a.xlsx"}


def test_forward_synthesis_submit_smoke_writes_pending_pipeline(tmp_path):
    from scripts.synthesis_coordinator import SynthesisCoordinator

    state_root = tmp_path / "state"
    connector_root = tmp_path / "connectors"
    event_path = state_root / "events" / "events.jsonl"
    exec_calls: list[dict] = []

    def _exec_tool(args: dict):
        exec_calls.append(args)
        return {"isError": False, "result_var_value": [{"file": "/tmp/b.xlsx"}]}

    coordinator = SynthesisCoordinator(
        state_root=state_root,
        connector_root=connector_root,
        exec_tool=_exec_tool,
    )
    job = {
        "job_id": "fwd-1",
        "normalized_intent": "mock.read.sheet",
        "connector": "mock",
        "runner_profile": "default",
        "source": "forward",
        "skill_name": "emerge-forward-synthesis",
        "samples": [{"args": {"filename": "/tmp/b.xlsx"}, "result": [{"file": "/tmp/b.xlsx"}]}],
    }

    result = coordinator.submit_synthesis_result(
        job=job,
        result={
            "connector": "mock",
            "mode": "read",
            "pipeline_name": "sheet",
            "code": "__result = [{'file': __args['filename']}]",
            "confidence": 0.91,
            "rationale": "filename varied across samples",
            "verify_strategy": {"required_fields": ["file"]},
        },
        event_path=event_path,
    )

    assert result["status"] == "completed"
    assert exec_calls[0]["intent_signature"] == "mock.read.sheet"
    assert exec_calls[0]["script_args"] == {"filename": "/tmp/b.xlsx"}
    assert exec_calls[0]["source"] == "forward_flywheel_synthesis"
    py_path = connector_root / "mock" / "pipelines" / "read" / "_pending" / "sheet.py"
    yaml_path = connector_root / "mock" / "pipelines" / "read" / "_pending" / "sheet.yaml"
    assert py_path.exists()
    assert yaml_path.exists()
    assert "__args['filename']" in py_path.read_text(encoding="utf-8")
    assert _read_events(event_path)[-1]["type"] == "forward_synthesis_completed"


def test_reverse_synthesis_submit_allows_distilled_intent_to_differ_from_source(tmp_path):
    from scripts.synthesis_coordinator import SynthesisCoordinator

    coordinator = SynthesisCoordinator(
        state_root=tmp_path / "state",
        connector_root=tmp_path / "connectors",
        exec_tool=lambda _args: {"isError": False, "result_var_value": [{"room": "101"}]},
    )

    response = coordinator.submit_synthesis_result(
        job={
            "job_id": "rev-1",
            "normalized_intent": "zwcad.entity_added.rooms",
            "source": "reverse",
            "runner_profile": "default",
            "samples": [{"args": {}}],
        },
        result={
            "connector": "zwcad",
            "mode": "read",
            "pipeline_name": "rooms",
            "code": "__result = [{'room': '101'}]",
        },
        event_path=tmp_path / "state" / "events" / "events.jsonl",
    )

    assert response["status"] == "completed"


def test_synthesis_submit_smoke_preserves_runner_profile(tmp_path):
    from scripts.synthesis_coordinator import SynthesisCoordinator

    calls: list[dict] = []
    coordinator = SynthesisCoordinator(
        state_root=tmp_path / "state",
        connector_root=tmp_path / "connectors",
        exec_tool=lambda args: calls.append(args) or {"isError": False, "result_var_value": []},
    )

    coordinator.submit_synthesis_result(
        job={
            "job_id": "fwd-remote",
            "normalized_intent": "mock.read.remote",
            "source": "forward",
            "runner_profile": "cad-win",
            "samples": [{"args": {}}],
        },
        result={
            "connector": "mock",
            "mode": "read",
            "pipeline_name": "remote",
            "code": "__result = []",
        },
        event_path=tmp_path / "state" / "events" / "events.jsonl",
    )

    assert calls[0]["target_profile"] == "cad-win"


def test_synthesis_submit_rejects_pipeline_name_path_traversal(tmp_path):
    from scripts.synthesis_coordinator import SynthesisCoordinator

    coordinator = SynthesisCoordinator(
        state_root=tmp_path / "state",
        connector_root=tmp_path / "connectors",
        exec_tool=lambda _args: {"isError": False, "result_var_value": []},
    )

    response = coordinator.submit_synthesis_result(
        job={
            "job_id": "fwd-traversal",
            "normalized_intent": "mock.read../outside",
            "source": "reverse",
            "samples": [{"args": {}}],
        },
        result={
            "connector": "mock",
            "mode": "read",
            "pipeline_name": "../outside",
            "code": "__result = []",
        },
        event_path=tmp_path / "state" / "events" / "events.jsonl",
    )

    assert response["status"] == "failed"
    assert not (tmp_path / "connectors" / "mock" / "pipelines" / "outside.py").exists()


def test_icc_synthesis_submit_exposes_lead_agent_result_entrypoint(tmp_path, monkeypatch):
    from scripts.emerge_daemon import EmergeDaemon

    state_root = tmp_path / "state"
    connector_root = tmp_path / "connectors"
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(state_root))
    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(connector_root))
    monkeypatch.setenv("EMERGE_SESSION_ID", "lead-agent-submit")

    daemon = EmergeDaemon(root=Path(__file__).resolve().parents[1])
    response = daemon.call_tool(
        "icc_synthesis_submit",
        {
            "job": {
                "job_id": "lead-1",
                "normalized_intent": "mock.read.sheet",
                "connector": "mock",
                "runner_profile": "default",
                "source": "forward",
                "skill_name": "emerge-forward-synthesis",
                "samples": [{"args": {"filename": "/tmp/c.xlsx"}}],
            },
            "result": {
                "connector": "mock",
                "mode": "read",
                "pipeline_name": "sheet",
                "code": "__result = [{'file': __args['filename']}]",
                "confidence": 0.9,
                "rationale": "parameterized filename",
            },
        },
    )

    assert response.get("isError") is not True, response
    body = response["structuredContent"]
    assert body["status"] == "completed"
    assert (connector_root / "mock" / "pipelines" / "read" / "_pending" / "sheet.py").exists()


def test_forward_synthesis_pending_formats_with_required_skill():
    from scripts.watch_emerge import _format_event

    rendered = _format_event(
        {
            "type": "forward_synthesis_pending",
            "job_id": "fwd-1",
            "intent_signature": "mock.read.sheet",
            "skill_name": "emerge-forward-synthesis",
        }
    )

    assert rendered is not None
    assert "ForwardSynthesisPending" in rendered
    assert "fwd-1" in rendered
    assert "emerge-forward-synthesis" in rendered
