# tests/test_daemon_http.py
from __future__ import annotations
import json, threading, time, urllib.request
import pytest
from pathlib import Path


def _post_mcp(port: int, payload: dict, session_id: str | None = None) -> dict:
    url = f"http://localhost:{port}/mcp"
    if session_id:
        url += f"?session_id={session_id}"
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body,
                                  headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def _make_server(tmp_path):
    """Start a DaemonHTTPServer with a minimal stub daemon and return server."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.daemon_http import DaemonHTTPServer

    class _StubDaemon:
        def handle_jsonrpc(self, req):
            if req.get("method") == "ping":
                return {"jsonrpc": "2.0", "id": req.get("id"), "result": {}}
            return {"jsonrpc": "2.0", "id": req.get("id"),
                    "error": {"code": -32601, "message": "not implemented"}}

    srv = DaemonHTTPServer(daemon=_StubDaemon(), port=0, pid_path=tmp_path / "d.pid")
    srv.start()
    time.sleep(0.1)
    return srv


def test_mcp_post_ping(tmp_path):
    srv = _make_server(tmp_path)
    resp = _post_mcp(srv.port, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert resp["result"] == {}
    srv.stop()


def test_sse_channel_established(tmp_path):
    srv = _make_server(tmp_path)
    lines = []
    def _read():
        req = urllib.request.Request(f"http://localhost:{srv.port}/mcp",
                                     headers={"Accept": "text/event-stream"})
        with urllib.request.urlopen(req, timeout=2) as r:
            for _ in range(2):
                lines.append(r.readline().decode())
    t = threading.Thread(target=_read, daemon=True)
    t.start()
    time.sleep(0.3)
    assert any("session_id" in l for l in lines)
    srv.stop()


def test_pid_file_written(tmp_path):
    srv = _make_server(tmp_path)
    pid_path = tmp_path / "d.pid"
    assert pid_path.exists()
    info = json.loads(pid_path.read_text())
    assert info["port"] == srv.port
    srv.stop()
    # PID file removed on stop
    assert not pid_path.exists()


def test_ensure_running_noop_when_already_running(tmp_path):
    """ensure_running_or_launch() returns early if daemon is already alive."""
    from scripts.daemon_http import DaemonHTTPServer, ensure_running_or_launch

    pid_path = tmp_path / "d.pid"

    class _StubDaemon:
        def handle_jsonrpc(self, req):
            return {"jsonrpc": "2.0", "id": req.get("id"), "result": {}}

    srv = DaemonHTTPServer(daemon=_StubDaemon(), port=0, pid_path=pid_path)
    srv.start()
    port = srv.port
    time.sleep(0.1)

    result = ensure_running_or_launch(pid_path=pid_path, port=0, daemon_factory=None)
    assert result == "already_running"
    assert srv.port == port
    srv.stop()


def _make_server_with_daemon(tmp_path, daemon=None):
    """Start DaemonHTTPServer with a daemon that has _span_tracker stubbed."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.daemon_http import DaemonHTTPServer

    class _SpanTrackerStub:
        def get_policy_status(self, intent_signature: str) -> str:
            return "explore"

    class _DaemonStub:
        def handle_jsonrpc(self, req):
            return {"jsonrpc": "2.0", "id": req.get("id"), "result": {}}
        _span_tracker = _SpanTrackerStub()
        _cockpit_server = None

    d = daemon or _DaemonStub()
    srv = DaemonHTTPServer(daemon=d, port=0, pid_path=tmp_path / "d.pid",
                           state_root=tmp_path / "repl")
    srv.start()
    time.sleep(0.1)
    return srv


def test_runner_push_pattern_alert_written_to_events_jsonl(tmp_path):
    """Pushing >=3 matching events → pattern_alert in events-{profile}.jsonl."""
    srv = _make_server_with_daemon(tmp_path)

    # Register runner
    body = json.dumps({"runner_profile": "p1", "machine_id": "m1"}).encode()
    r = urllib.request.Request(f"http://localhost:{srv.port}/runner/online",
                               data=body, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(r, timeout=5)

    # Push 3 identical-pattern events
    now_ms = int(time.time() * 1000)
    for i in range(3):
        event = {
            "runner_profile": "p1",
            "machine_id": "m1",
            "session_role": "operator",
            "event_type": "entity_added",
            "app": "zwcad",
            "payload": {"layer": "标注", "content": f"room_{i}"},
            "ts_ms": now_ms - i * 60_000,
        }
        body2 = json.dumps(event).encode()
        r2 = urllib.request.Request(f"http://localhost:{srv.port}/runner/event",
                                    data=body2, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(r2, timeout=5)

    events_file = tmp_path / "repl" / "events-p1.jsonl"
    assert events_file.exists(), "events-p1.jsonl must exist"
    alerts = [
        json.loads(l)
        for l in events_file.read_text().splitlines()
        if l.strip() and json.loads(l).get("type") == "pattern_alert"
    ]
    assert len(alerts) >= 1, "at least one pattern_alert expected"
    alert = alerts[0]
    assert alert["stage"] == "explore"
    assert "intent_signature" in alert
    assert alert["meta"]["occurrences"] >= 3
    srv.stop()


def test_runner_push_pattern_updates_last_alert(tmp_path):
    """After >=3 events, _connected_runners[profile]['last_alert'] is populated."""
    srv = _make_server_with_daemon(tmp_path)

    body = json.dumps({"runner_profile": "p2", "machine_id": "m2"}).encode()
    r = urllib.request.Request(f"http://localhost:{srv.port}/runner/online",
                               data=body, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(r, timeout=5)

    now_ms = int(time.time() * 1000)
    for i in range(3):
        event = {
            "runner_profile": "p2", "machine_id": "m2",
            "session_role": "operator", "event_type": "entity_added",
            "app": "zwcad", "payload": {"layer": "标注", "content": f"x_{i}"},
            "ts_ms": now_ms - i * 60_000,
        }
        body2 = json.dumps(event).encode()
        r2 = urllib.request.Request(f"http://localhost:{srv.port}/runner/event",
                                    data=body2, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(r2, timeout=5)

    with srv._runners_lock:
        last_alert = srv._connected_runners.get("p2", {}).get("last_alert")
    assert last_alert is not None, "last_alert must be set after pattern detection"
    assert last_alert["stage"] == "explore"
    srv.stop()


def test_team_active_true_when_runner_connected(tmp_path):
    """_write_monitor_state sets team_active=True when a runner is connected."""
    srv = _make_server_with_daemon(tmp_path)

    body = json.dumps({"runner_profile": "p3", "machine_id": "m3"}).encode()
    r = urllib.request.Request(f"http://localhost:{srv.port}/runner/online",
                               data=body, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(r, timeout=5)

    state_path = tmp_path / "repl" / "runner-monitor-state.json"
    time.sleep(0.1)
    assert state_path.exists()
    state = json.loads(state_path.read_text())
    assert state["team_active"] is True
    srv.stop()


def test_broadcast_called_on_pattern_detection(tmp_path):
    """cockpit.broadcast({"monitors_updated": True}) is called after pattern detected."""
    broadcasts = []

    class _MockCockpit:
        def broadcast(self, event: dict) -> None:
            broadcasts.append(event)

    class _SpanTrackerStub:
        def get_policy_status(self, _sig): return "explore"

    class _DaemonStub:
        def handle_jsonrpc(self, req):
            return {"jsonrpc": "2.0", "id": req.get("id"), "result": {}}
        _span_tracker = _SpanTrackerStub()
        _cockpit_server = _MockCockpit()

    srv = _make_server_with_daemon(tmp_path, daemon=_DaemonStub())

    body = json.dumps({"runner_profile": "p4", "machine_id": "m4"}).encode()
    r = urllib.request.Request(f"http://localhost:{srv.port}/runner/online",
                               data=body, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(r, timeout=5)

    now_ms = int(time.time() * 1000)
    for i in range(3):
        event = {
            "runner_profile": "p4", "machine_id": "m4",
            "session_role": "operator", "event_type": "entity_added",
            "app": "zwcad", "payload": {"layer": "标注", "content": f"y_{i}"},
            "ts_ms": now_ms - i * 60_000,
        }
        body2 = json.dumps(event).encode()
        r2 = urllib.request.Request(f"http://localhost:{srv.port}/runner/event",
                                    data=body2, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(r2, timeout=5)

    assert any(b.get("monitors_updated") for b in broadcasts), \
        "broadcast(monitors_updated=True) must be called after pattern detection"
    srv.stop()
