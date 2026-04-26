from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_runner_pattern_synthesizes_wal_without_direct_pipeline_write(tmp_path, monkeypatch):
    from scripts.daemon_http import DaemonHTTPServer
    from scripts.emerge_daemon import EmergeDaemon
    from scripts.policy_config import PROMOTE_MIN_ATTEMPTS

    state_root = tmp_path / "state"
    connector_root = tmp_path / "connectors"
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(state_root))
    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(connector_root))
    monkeypatch.setenv("EMERGE_SESSION_ID", "reverse-e2e")

    daemon = EmergeDaemon(root=ROOT)
    srv = DaemonHTTPServer(
        daemon=daemon,
        port=0,
        pid_path=tmp_path / "daemon.pid",
        state_root=state_root,
        event_root=tmp_path / "operator-events",
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
        assert any(e.get("type") == "pattern_observed" for e in events)
        assert not any(e.get("type") == "pattern_pending_synthesis" for e in events)
        assert not any(e.get("type") == "synthesis_job_ready" for e in events)
        assert not any(e.get("type") == "synthesis_exec_succeeded" for e in events)

        py_path = connector_root / "zwcad" / "pipelines" / "read" / "rooms.py"
        yaml_path = connector_root / "zwcad" / "pipelines" / "read" / "rooms.yaml"
        assert not py_path.exists()
        assert not yaml_path.exists()
    finally:
        srv.stop()
