from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_runner_role_exec_does_not_emit_forward_synthesis(monkeypatch, tmp_path):
    from scripts import runner_emit
    from scripts.emerge_daemon import EmergeDaemon
    from scripts.intent_registry import registry_path

    forwarded: list[dict] = []
    monkeypatch.setattr(runner_emit, "emit_event", forwarded.append)
    monkeypatch.setenv("EMERGE_NODE_ROLE", "runner")
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("EMERGE_SESSION_ID", "runner-forward-guard")

    daemon = EmergeDaemon(root=ROOT)
    for i in range(6):
        result = daemon.call_tool(
            "icc_exec",
            {
                "intent_signature": "mock.read.runner-guard",
                "code": f"__result = [{{'i': {i}}}]",
                "result_var": "__result",
            },
        )
        assert result.get("isError") is not True

    assert forwarded
    assert all(event["type"] == "evidence_report" for event in forwarded)
    assert not registry_path(tmp_path / "state").exists()
    events_file = tmp_path / "state" / "events" / "events.jsonl"
    if events_file.exists():
        assert "forward_synthesis_pending" not in events_file.read_text(encoding="utf-8")
