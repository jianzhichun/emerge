from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_runner_role_icc_exec_forwards_evidence_without_local_policy_write(monkeypatch, tmp_path):
    from scripts.emerge_daemon import EmergeDaemon
    from scripts.intent_registry import registry_path
    import scripts.runner_emit as runner_emit

    monkeypatch.setenv("EMERGE_NODE_ROLE", "runner")
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path / "runner-state"))
    forwarded: list[dict] = []
    monkeypatch.setattr(runner_emit, "emit_event", lambda event, **_kw: forwarded.append(event) or True)

    daemon = EmergeDaemon(root=ROOT)
    result = daemon.call_tool(
        "icc_exec",
        {
            "intent_signature": "mock.write.forwarded",
            "code": "__action = {'ok': True}",
            "result_var": "__action",
        },
    )

    assert result.get("isError") is False
    evidence = [event for event in forwarded if event.get("type") == "evidence_report"]
    assert len(evidence) == 1
    assert evidence[0]["intent_signature"] == "mock.write.forwarded"
    assert evidence[0]["success"] is True
    assert evidence[0]["verify_observed"] is True
    assert evidence[0]["evidence_unit_id"]
    assert not registry_path(tmp_path / "runner-state").exists()


def test_orchestrator_ingests_evidence_report_idempotently(tmp_path, monkeypatch):
    from scripts.daemon_http import DaemonHTTPServer
    from scripts.emerge_daemon import EmergeDaemon
    from scripts.intent_registry import IntentRegistry

    state_root = tmp_path / "state"
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(state_root))
    daemon = EmergeDaemon(root=ROOT)
    srv = DaemonHTTPServer(
        daemon=daemon,
        port=0,
        pid_path=tmp_path / "daemon.pid",
        state_root=state_root,
        event_root=tmp_path / "operator-events",
    )

    try:
        payload = {
            "type": "evidence_report",
            "message_id": "msg-1",
            "runner_profile": "cad1",
            "machine_id": "m1",
            "intent_signature": "mock.write.forwarded",
            "success": True,
            "verify_observed": True,
            "verify_passed": True,
            "evidence_unit_id": "exec-1",
            "ts_ms": 1234,
        }

        srv._on_runner_event(dict(payload))
        srv._on_runner_event(dict(payload))

        entry = IntentRegistry.get(state_root, "mock.write.forwarded")
        assert entry is not None
        assert entry["attempts"] == 1
        assert entry["successes"] == 1
        assert entry["verify_attempts"] == 1
    finally:
        srv.stop()


def test_evidence_report_dedupe_survives_daemon_http_restart(tmp_path, monkeypatch):
    from scripts.daemon_http import DaemonHTTPServer
    from scripts.emerge_daemon import EmergeDaemon
    from scripts.intent_registry import IntentRegistry

    state_root = tmp_path / "state"
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(state_root))
    payload = {
        "type": "evidence_report",
        "message_id": "msg-restart",
        "runner_profile": "cad1",
        "machine_id": "m1",
        "intent_signature": "mock.write.forwarded",
        "success": True,
        "verify_observed": True,
        "verify_passed": True,
        "evidence_unit_id": "exec-restart",
        "ts_ms": 1234,
    }

    first = DaemonHTTPServer(
        daemon=EmergeDaemon(root=ROOT),
        port=0,
        pid_path=tmp_path / "daemon-1.pid",
        state_root=state_root,
        event_root=tmp_path / "operator-events",
    )
    try:
        first._on_runner_event(dict(payload))
    finally:
        first.stop()

    second = DaemonHTTPServer(
        daemon=EmergeDaemon(root=ROOT),
        port=0,
        pid_path=tmp_path / "daemon-2.pid",
        state_root=state_root,
        event_root=tmp_path / "operator-events",
    )
    try:
        second._on_runner_event(dict(payload))
    finally:
        second.stop()

    entry = IntentRegistry.get(state_root, "mock.write.forwarded")
    assert entry is not None
    assert entry["attempts"] == 1
