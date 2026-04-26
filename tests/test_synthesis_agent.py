from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.pattern_detector import PatternSummary


def _summary() -> PatternSummary:
    return PatternSummary(
        machine_ids=["m1"],
        intent_signature="zwcad.entity_added.rooms",
        occurrences=3,
        window_minutes=2.0,
        detector_signals=["frequency"],
        context_hint={"app": "zwcad", "event_type": "entity_added", "layer": "rooms"},
    )


def _events() -> list[dict]:
    return [
        {
            "ts_ms": 1000 + i,
            "machine_id": "m1",
            "session_role": "operator",
            "app": "zwcad",
            "event_type": "entity_added",
            "payload": {"layer": "rooms", "content": f"room_{i}"},
        }
        for i in range(3)
    ]


def _read_events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_normalize_intent_signature_preserves_clean_signature():
    from scripts.synthesis_agent import _normalize_intent_signature

    assert _normalize_intent_signature("zwcad.annotate.room_labels") == "zwcad.annotate.room_labels"


def test_normalize_intent_signature_replaces_non_ascii_segments():
    from scripts.synthesis_agent import _normalize_intent_signature

    sig = _normalize_intent_signature("zwcad.标注层")

    assert sig.startswith("zwcad.")
    assert all(c.isascii() or c in (".", "_") for c in sig)


def test_normalize_intent_signature_caps_length():
    from scripts.synthesis_agent import _normalize_intent_signature

    long_sig = "a" * 100 + "." + "b" * 100 + "." + "c" * 100

    assert len(_normalize_intent_signature(long_sig)) <= 200


def test_synthesis_agent_enqueues_job_for_lead_agent_even_with_provider(tmp_path, monkeypatch):
    from scripts.synthesis_agent import SynthesisAgent, SynthesisResult

    connector_root = tmp_path / "connectors"
    (connector_root / "zwcad").mkdir(parents=True)
    (connector_root / "zwcad" / "NOTES.md").write_text("Use zwcad COM API.", encoding="utf-8")
    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(connector_root))

    calls: list[dict] = []

    class _Provider:
        def synthesize(self, job):
            raise AssertionError("provider must not run on the default lead-agent path")

    agent = SynthesisAgent(
        state_root=tmp_path / "state",
        connector_root=connector_root,
        provider=_Provider(),
        exec_tool=lambda args: calls.append(args) or {"isError": False},
    )

    result = agent.process_pattern(
        summary=_summary(),
        runner_profile="p1",
        events=_events(),
        event_path=tmp_path / "state" / "events" / "events-p1.jsonl",
    )

    assert result["status"] == "enqueued"
    assert calls == []
    events = _read_events(tmp_path / "state" / "events" / "events-p1.jsonl")
    assert [e["type"] for e in events] == [
        "pattern_pending_synthesis",
        "synthesis_job_ready",
    ]
    assert events[-1]["job"]["skill_name"] == "emerge-reverse-synthesis"
    assert events[-1]["job"]["connector_notes"] == "Use zwcad COM API."


def test_synthesis_agent_provider_exec_mode_remains_compatibility_path(tmp_path, monkeypatch):
    from scripts.synthesis_agent import SynthesisAgent, SynthesisResult

    connector_root = tmp_path / "connectors"
    (connector_root / "zwcad").mkdir(parents=True)
    (connector_root / "zwcad" / "NOTES.md").write_text("Use zwcad COM API.", encoding="utf-8")
    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(connector_root))

    calls: list[dict] = []

    class _Provider:
        def synthesize(self, job):
            assert job.normalized_intent == "zwcad.entity_added.rooms"
            assert job.connector == "zwcad"
            assert "COM API" in job.connector_notes
            return SynthesisResult(
                connector="zwcad",
                mode="read",
                pipeline_name="rooms",
                code="__result = [{'name': 'room_0'}]",
                confidence=0.9,
                rationale="fixture",
            )

    def _exec_tool(arguments: dict):
        calls.append(arguments)
        return {"isError": False, "content": [{"type": "text", "text": "ok"}]}

    agent = SynthesisAgent(
        state_root=tmp_path / "state",
        connector_root=connector_root,
        provider=_Provider(),
        exec_tool=_exec_tool,
        mode="provider_exec",
    )

    result = agent.process_pattern(
        summary=_summary(),
        runner_profile="p1",
        events=_events(),
        event_path=tmp_path / "state" / "events" / "events-p1.jsonl",
    )

    assert result["status"] == "executed"
    assert len(calls) == 1
    call = calls[0]
    assert call["intent_signature"] == "zwcad.read.rooms"
    assert call["result_var"] == "__result"
    assert call["source"] == "reverse_flywheel_synthesis"
    assert call["synthesis_job_id"] == result["job_id"]
    assert call["code"] == "__result = [{'name': 'room_0'}]"

    events = _read_events(tmp_path / "state" / "events" / "events-p1.jsonl")
    assert [e["type"] for e in events] == [
        "pattern_pending_synthesis",
        "synthesis_exec_succeeded",
    ]
    assert events[0]["job_id"] == result["job_id"]
    assert events[1]["intent_signature"] == "zwcad.read.rooms"


def test_synthesis_agent_dedupes_same_event_fingerprint(tmp_path):
    from scripts.synthesis_agent import SynthesisAgent, SynthesisResult

    provider_calls = 0

    class _Provider:
        def synthesize(self, job):
            nonlocal provider_calls
            provider_calls += 1
            return SynthesisResult(
                connector="zwcad",
                mode="write",
                pipeline_name="rooms",
                code="__action = {'ok': True}",
            )

    agent = SynthesisAgent(
        state_root=tmp_path / "state",
        connector_root=tmp_path / "connectors",
        provider=_Provider(),
        exec_tool=lambda args: {"isError": False},
        mode="provider_exec",
    )

    first = agent.process_pattern(
        summary=_summary(),
        runner_profile="p1",
        events=_events(),
        event_path=tmp_path / "state" / "events" / "events-p1.jsonl",
    )
    second = agent.process_pattern(
        summary=_summary(),
        runner_profile="p1",
        events=_events(),
        event_path=tmp_path / "state" / "events" / "events-p1.jsonl",
    )

    assert first["status"] == "executed"
    assert second["status"] == "duplicate"
    assert provider_calls == 1


def test_null_provider_records_unconfigured_failure(tmp_path):
    from scripts.synthesis_agent import NullSynthesisProvider, SynthesisAgent

    agent = SynthesisAgent(
        state_root=tmp_path / "state",
        connector_root=tmp_path / "connectors",
        provider=NullSynthesisProvider(),
        exec_tool=lambda args: {"isError": False},
        mode="provider_exec",
    )

    result = agent.process_pattern(
        summary=_summary(),
        runner_profile="p1",
        events=_events(),
        event_path=tmp_path / "state" / "events" / "events-p1.jsonl",
    )

    assert result["status"] == "failed"
    events = _read_events(tmp_path / "state" / "events" / "events-p1.jsonl")
    assert events[-1]["type"] == "synthesis_unconfigured"
    assert events[-1]["job_id"] == result["job_id"]


def test_synthesis_agent_default_mode_enqueues_for_main_brain(tmp_path):
    from scripts.synthesis_agent import SynthesisAgent

    calls: list[dict] = []
    agent = SynthesisAgent(
        state_root=tmp_path / "state",
        connector_root=tmp_path / "connectors",
        exec_tool=lambda args: calls.append(args) or {"isError": False},
    )

    result = agent.process_pattern(
        summary=_summary(),
        runner_profile="p1",
        events=_events(),
        event_path=tmp_path / "state" / "events" / "events-p1.jsonl",
    )

    assert result["status"] == "enqueued"
    assert calls == []
    events = _read_events(tmp_path / "state" / "events" / "events-p1.jsonl")
    assert [e["type"] for e in events] == ["pattern_pending_synthesis", "synthesis_job_ready"]
    assert events[-1]["job"]["normalized_intent"] == "zwcad.entity_added.rooms"
    assert events[-1]["job"]["skill_name"] == "emerge-reverse-synthesis"


def test_command_synthesis_provider_uses_json_stdin_stdout(tmp_path):
    from scripts.synthesis_agent import CommandSynthesisProvider, SynthesisJob

    script = tmp_path / "provider.py"
    script.write_text(
        "import json, sys\n"
        "job = json.load(sys.stdin)\n"
        "json.dump({\n"
        "  'connector': job['connector'],\n"
        "  'mode': 'write',\n"
        "  'pipeline_name': 'rooms',\n"
        "  'code': \"__action = {'ok': True}\",\n"
        "  'confidence': 0.75\n"
        "}, sys.stdout)\n",
        encoding="utf-8",
    )

    provider = CommandSynthesisProvider([sys.executable, str(script)])
    result = provider.synthesize(
        SynthesisJob(
            job_id="job-1",
            normalized_intent="zwcad.entity_added.rooms",
            connector="zwcad",
            runner_profile="p1",
            machine_ids=["m1"],
            detector_signals=["frequency"],
            context_hint={"app": "zwcad"},
            events=_events(),
        )
    )

    assert result.connector == "zwcad"
    assert result.mode == "write"
    assert result.pipeline_name == "rooms"
    assert "__action" in result.code


def test_icc_exec_persists_reverse_synthesis_metadata(tmp_path, monkeypatch):
    from scripts.emerge_daemon import EmergeDaemon
    from scripts.policy_config import sessions_root

    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("EMERGE_SESSION_ID", "synthesis-meta")
    daemon = EmergeDaemon(root=ROOT)

    result = daemon.call_tool(
        "icc_exec",
        {
            "intent_signature": "zwcad.read.rooms",
            "code": "__result = [{'name': 'room_0'}]",
            "result_var": "__result",
            "source": "reverse_flywheel_synthesis",
            "synthesis_job_id": "syn-123",
            "source_intent_signature": "zwcad.entity_added.rooms",
        },
    )

    assert result.get("isError") is not True
    wal_path = sessions_root(tmp_path / "state") / "synthesis-meta" / "wal.jsonl"
    lines = [json.loads(line) for line in wal_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    metadata = lines[-1]["metadata"]
    assert metadata["source"] == "reverse_flywheel_synthesis"
    assert metadata["synthesis_job_id"] == "syn-123"
    assert metadata["source_intent_signature"] == "zwcad.entity_added.rooms"
