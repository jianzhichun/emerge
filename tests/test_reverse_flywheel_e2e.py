from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_runner_pattern_synthesizes_wal_and_crystallized_pipeline(tmp_path, monkeypatch):
    from scripts.daemon_http import DaemonHTTPServer
    from scripts.emerge_daemon import EmergeDaemon
    from scripts.policy_config import PROMOTE_MIN_ATTEMPTS, sessions_root
    from scripts.synthesis_agent import SynthesisAgent, SynthesisResult

    state_root = tmp_path / "state"
    connector_root = tmp_path / "connectors"
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(state_root))
    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(connector_root))
    monkeypatch.setenv("EMERGE_SESSION_ID", "reverse-e2e")

    class _Provider:
        def synthesize(self, job):
            return SynthesisResult(
                connector="zwcad",
                mode="read",
                pipeline_name="rooms",
                code="__result = [{'name': 'room', 'count': len(__args)}]",
                confidence=0.8,
            )

    daemon = EmergeDaemon(root=ROOT)
    srv = DaemonHTTPServer(
        daemon=daemon,
        port=0,
        pid_path=tmp_path / "daemon.pid",
        state_root=state_root,
        event_root=tmp_path / "operator-events",
    )
    srv._synthesis_agent = SynthesisAgent(
        state_root=state_root,
        connector_root=connector_root,
        provider=_Provider(),
        exec_tool=lambda args: daemon.call_tool("icc_exec", args),
    )
    srv.start()
    try:
        body = json.dumps({"runner_profile": "cad1", "machine_id": "m1"}).encode()
        req = urllib.request.Request(
            f"http://localhost:{srv.port}/runner/online",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5)

        now_ms = int(time.time() * 1000)
        # Pattern starts firing on the third event. Send enough post-threshold
        # events to produce PROMOTE_MIN_ATTEMPTS synthesized exec attempts.
        for i in range(PROMOTE_MIN_ATTEMPTS + 2):
            event = {
                "runner_profile": "cad1",
                "machine_id": "m1",
                "session_role": "operator",
                "event_type": "entity_added",
                "app": "zwcad",
                "payload": {"layer": "rooms", "content": f"room_{i}"},
                "ts_ms": now_ms + i,
            }
            req = urllib.request.Request(
                f"http://localhost:{srv.port}/runner/event",
                data=json.dumps(event).encode(),
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=5)

        events_file = state_root / "events" / "events-cad1.jsonl"
        events = [json.loads(line) for line in events_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert any(e.get("type") == "pattern_alert" for e in events)
        assert any(e.get("type") == "pattern_pending_synthesis" for e in events)
        assert any(e.get("type") == "synthesis_exec_succeeded" for e in events)

        wal_path = sessions_root(state_root) / "reverse-e2e__cad1-b748e14c48" / "wal.jsonl"
        if not wal_path.exists():
            wal_path = next(sessions_root(state_root).glob("reverse-e2e__*/wal.jsonl"))
        wal = [json.loads(line) for line in wal_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert any(
            row.get("metadata", {}).get("source") == "reverse_flywheel_synthesis"
            and row.get("metadata", {}).get("intent_signature") == "zwcad.read.rooms"
            for row in wal
        )

        py_path = connector_root / "zwcad" / "pipelines" / "read" / "rooms.py"
        yaml_path = connector_root / "zwcad" / "pipelines" / "read" / "rooms.yaml"
        assert py_path.exists()
        assert yaml_path.exists()
    finally:
        srv.stop()
