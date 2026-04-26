from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _body(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def test_runner_role_blocks_orchestrator_only_tools(monkeypatch):
    from scripts.emerge_daemon import EmergeDaemon

    monkeypatch.setenv("EMERGE_NODE_ROLE", "runner")
    daemon = EmergeDaemon(root=ROOT)

    crystallize = daemon.call_tool(
        "icc_crystallize",
        {
            "intent_signature": "mock.write.add-wall",
            "connector": "mock",
            "pipeline_name": "add-wall",
            "mode": "write",
        },
    )
    compose = daemon.call_tool(
        "icc_compose",
        {
            "intent_signature": "mock.write.composite",
            "children": ["mock.write.a", "mock.write.b"],
        },
    )
    reconcile = daemon.call_tool(
        "icc_reconcile",
        {
            "delta_id": "d1",
            "outcome": "confirm",
            "intent_signature": "mock.write.add-wall",
        },
    )
    approve = daemon.call_tool("icc_span_approve", {"intent_signature": "mock.write.add-wall"})
    submit = daemon.call_tool("icc_synthesis_submit", {"job": {}, "result": {}})

    assert crystallize["isError"] is True
    assert "orchestrator-only" in crystallize["content"][0]["text"]
    assert compose["isError"] is True
    assert "orchestrator-only" in compose["content"][0]["text"]
    assert reconcile["isError"] is True
    assert "orchestrator-only" in reconcile["content"][0]["text"]
    assert approve["isError"] is True
    assert "orchestrator-only" in approve["content"][0]["text"]
    assert submit["isError"] is True
    assert "orchestrator-only" in submit["content"][0]["text"]


def test_runner_span_close_does_not_generate_or_emit_synthesis(monkeypatch):
    from scripts.emerge_daemon import EmergeDaemon

    monkeypatch.setenv("EMERGE_NODE_ROLE", "runner")
    daemon = EmergeDaemon(root=ROOT)
    emitted: list[dict] = []

    daemon._span_handlers._emit_cockpit_action = emitted.append
    daemon._span_tracker.is_synthesis_ready = lambda _sig: True
    daemon._span_tracker.skeleton_already_generated = lambda _sig: False
    daemon._span_handlers._generate_skeleton = lambda **_kw: (_ for _ in ()).throw(
        AssertionError("runner role must not generate skeletons")
    )

    opened = _body(daemon.call_tool("icc_span_open", {"intent_signature": "mock.write.runner-span"}))
    closed = _body(
        daemon.call_tool(
            "icc_span_close",
            {"span_id": opened["span_id"], "outcome": "success"},
        )
    )

    assert closed["synthesis_ready"] is True
    assert "skeleton_path" not in closed
    assert emitted == []
