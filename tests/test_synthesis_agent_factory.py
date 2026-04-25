from __future__ import annotations

import sys
import json
import time
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_synthesis_agent_factory_returns_real_instance(tmp_path, monkeypatch):
    from scripts.daemon_http import DaemonHTTPServer
    from scripts.emerge_daemon import EmergeDaemon
    from scripts.synthesis_agent import SynthesisAgent

    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path / "state"))
    daemon = EmergeDaemon(root=ROOT)
    srv = DaemonHTTPServer(
        daemon=daemon,
        port=0,
        pid_path=tmp_path / "daemon.pid",
        state_root=tmp_path / "state",
        event_root=tmp_path / "operator-events",
    )
    try:
        assert isinstance(srv._synthesis_agent, SynthesisAgent)
    finally:
        srv.stop()


def test_reverse_flywheel_factory_emits_synthesis_job_ready(tmp_path, monkeypatch):
    from scripts.daemon_http import DaemonHTTPServer
    from scripts.emerge_daemon import EmergeDaemon

    state_root = tmp_path / "state"
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(state_root))
    monkeypatch.delenv("EMERGE_SYNTHESIS_COMMAND", raising=False)
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
        online = urllib.request.Request(
            f"http://localhost:{srv.port}/runner/online",
            data=json.dumps({"runner_profile": "p1", "machine_id": "m1"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(online, timeout=5)
        now_ms = int(time.time() * 1000)
        for i in range(3):
            event = {
                "runner_profile": "p1",
                "machine_id": "m1",
                "session_role": "operator",
                "event_type": "entity_added",
                "app": "generic",
                "payload": {"kind": "item", "content": f"item_{i}"},
                "ts_ms": now_ms + i,
            }
            req = urllib.request.Request(
                f"http://localhost:{srv.port}/runner/event",
                data=json.dumps(event).encode(),
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=5)

        events_file = state_root / "events" / "events-p1.jsonl"
        events = [json.loads(line) for line in events_file.read_text().splitlines() if line.strip()]
        assert any(event.get("type") == "synthesis_job_ready" for event in events)
        pending = [event for event in events if event.get("type") == "pattern_pending_synthesis"]
        assert len(pending) == 1
    finally:
        srv.stop()


def test_synthesis_job_ready_formats_for_watch_emerge():
    from scripts.watch_emerge import _format_event

    rendered = _format_event(
        {
            "type": "synthesis_job_ready",
            "runner_profile": "p1",
            "job_id": "job-1",
            "intent_signature": "foo.write.bar",
        }
    )

    assert rendered is not None
    assert "SynthesisJobReady" in rendered
    assert "job-1" in rendered
