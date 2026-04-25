# tests/test_daemon_http.py
from __future__ import annotations
import http.client
import json, threading, time, urllib.request, urllib.error
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


def test_mcp_post_jsonrpc_notification_returns_202_empty_body(tmp_path):
    """JSON-RPC notifications (no response body) map to HTTP 202 per handle_post_mcp."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.daemon_http import DaemonHTTPServer

    class _StubDaemon:
        def handle_jsonrpc(self, req):
            if str(req.get("method", "")).startswith("notifications/"):
                return None
            return {"jsonrpc": "2.0", "id": req.get("id"), "result": {}}

    srv = DaemonHTTPServer(daemon=_StubDaemon(), port=0, pid_path=tmp_path / "d.pid")
    srv.start()
    time.sleep(0.1)
    url = f"http://localhost:{srv.port}/mcp"
    body = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 202
        assert r.read() == b""
    srv.stop()


def test_cockpit_root_served_on_daemon_port(tmp_path):
    """Cockpit HTML is merged into DaemonHTTPServer (GET /)."""
    srv = _make_server(tmp_path)
    req = urllib.request.Request(f"http://localhost:{srv.port}/")
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 200
        ctype = r.headers.get("Content-Type", "").lower()
        body = r.read().decode("utf-8", errors="replace")
    assert "text/html" in ctype
    assert len(body) > 100
    srv.stop()


def test_control_plane_sessions_endpoint_lists_known_sessions(tmp_path):
    srv = _make_server_with_daemon(tmp_path)
    session_dir = tmp_path / "repl" / "sessions" / "demo-session"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "wal.jsonl").write_text('{"x":1}\n', encoding="utf-8")
    req = urllib.request.Request(f"http://localhost:{srv.port}/api/control-plane/sessions")
    with urllib.request.urlopen(req, timeout=5) as r:
        payload = json.loads(r.read().decode("utf-8"))
    assert payload["ok"] is True
    ids = [x.get("session_id") for x in payload.get("sessions", [])]
    assert "demo-session" in ids
    srv.stop()


def test_control_plane_session_rejects_invalid_session_id(tmp_path):
    srv = _make_server_with_daemon(tmp_path)
    req = urllib.request.Request(
        f"http://localhost:{srv.port}/api/control-plane/session?session_id=../bad",
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 400
    srv.stop()


def test_policy_status_accepts_explicit_session_id(tmp_path):
    srv = _make_server_with_daemon(tmp_path)
    req = urllib.request.Request(
        f"http://localhost:{srv.port}/api/policy?session_id=session-optimal-1",
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        payload = json.loads(r.read().decode("utf-8"))
    assert payload.get("session_id") == "session-optimal-1"
    srv.stop()


def test_apis_path_not_routed_to_cockpit(tmp_path):
    """`/apis` must not match the `/api` prefix (regression guard)."""
    srv = _make_server(tmp_path)
    req = urllib.request.Request(f"http://localhost:{srv.port}/apis")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 404
    srv.stop()


def test_assets_path_is_routed_to_cockpit_handler():
    """`/assets/*` must be treated as cockpit route (Svelte dist assets)."""
    from scripts.daemon_http import _is_cockpit_http_path
    assert _is_cockpit_http_path("/assets/index.js") is True
    assert _is_cockpit_http_path("/apis") is False


def test_get_absolute_request_target_without_path_serves_cockpit(tmp_path):
    """RFC 7230 absolute-form ``GET http://host:port`` has empty path → must serve ``/``."""
    srv = _make_server(tmp_path)
    host, port = "127.0.0.1", srv.port
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request(
            "GET",
            f"http://{host}:{port}",
            headers={"Host": f"{host}:{port}"},
        )
        r = conn.getresponse()
        body = r.read().decode("utf-8", errors="replace")
        assert r.status == 200
        assert "text/html" in (r.getheader("Content-Type") or "").lower()
        assert len(body) > 100
    finally:
        conn.close()
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


def test_health_deep_exposes_metrics_and_request_id(tmp_path):
    srv = _make_server(tmp_path)
    req = urllib.request.Request(f"http://localhost:{srv.port}/health/deep")
    with urllib.request.urlopen(req, timeout=5) as r:
        payload = json.loads(r.read().decode("utf-8"))
        req_id = r.headers.get("X-Request-Id", "")
    assert payload.get("ok") is True
    assert isinstance(payload.get("metrics"), dict)
    assert payload.get("request_id") == req_id
    assert isinstance(payload["metrics"].get("requests_total", 0), int)
    srv.stop()


def test_pid_file_written(tmp_path):
    srv = _make_server(tmp_path)
    pid_path = tmp_path / "d.pid"
    assert pid_path.exists()
    info = json.loads(pid_path.read_text())
    assert info["port"] == srv.port
    assert info.get("host") == "0.0.0.0"
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

    events_file = tmp_path / "repl" / "events" / "events-p1.jsonl"
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


def test_runner_push_pattern_enqueues_synthesis(tmp_path):
    """Pattern detection should emit pending synthesis and call the synthesis agent."""
    srv = _make_server_with_daemon(tmp_path)

    calls = []

    class _Agent:
        def process_pattern(self, *, summary, runner_profile, events, event_path):
            calls.append(
                {
                    "intent_signature": summary.intent_signature,
                    "runner_profile": runner_profile,
                    "events": list(events),
                    "event_path": event_path,
                }
            )
            return {"status": "queued", "job_id": "job-test"}

    srv._synthesis_agent = _Agent()

    body = json.dumps({"runner_profile": "p1", "machine_id": "m1"}).encode()
    r = urllib.request.Request(f"http://localhost:{srv.port}/runner/online",
                               data=body, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(r, timeout=5)

    now_ms = int(time.time() * 1000)
    for i in range(3):
        event = {
            "runner_profile": "p1",
            "machine_id": "m1",
            "session_role": "operator",
            "event_type": "entity_added",
            "app": "zwcad",
            "payload": {"layer": "rooms", "content": f"room_{i}"},
            "ts_ms": now_ms - i * 60_000,
        }
        body2 = json.dumps(event).encode()
        r2 = urllib.request.Request(f"http://localhost:{srv.port}/runner/event",
                                    data=body2, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(r2, timeout=5)

    events_file = tmp_path / "repl" / "events" / "events-p1.jsonl"
    lines = [json.loads(l) for l in events_file.read_text().splitlines() if l.strip()]
    assert any(e.get("type") == "pattern_pending_synthesis" for e in lines)
    assert calls, "synthesis agent should receive the detected pattern"
    assert calls[0]["runner_profile"] == "p1"
    assert len(calls[0]["events"]) >= 3
    assert calls[0]["event_path"] == events_file
    srv.stop()


def test_runner_event_rejects_invalid_machine_id(tmp_path):
    """Path-traversal-like machine_id should be rejected with 400."""
    srv = _make_server_with_daemon(tmp_path)
    bad = {
        "runner_profile": "pbad",
        "machine_id": "../escape",
        "type": "op_event",
        "ts_ms": int(time.time() * 1000),
    }
    body = json.dumps(bad).encode()
    req = urllib.request.Request(
        f"http://localhost:{srv.port}/runner/event",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 400
    assert not (tmp_path / "escape" / "events.jsonl").exists()
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

    state_path = tmp_path / "repl" / "events" / "runner-monitor-state.json"
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


def test_daemon_bind_all_interfaces_reachable_on_loopback(tmp_path):
    """0.0.0.0 bind still accepts connections to 127.0.0.1."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.daemon_http import DaemonHTTPServer

    class _StubDaemon:
        def handle_jsonrpc(self, req):
            if req.get("method") == "ping":
                return {"jsonrpc": "2.0", "id": req.get("id"), "result": {}}
            return {"jsonrpc": "2.0", "id": req.get("id"),
                    "error": {"code": -32601, "message": "not implemented"}}

    srv = DaemonHTTPServer(
        daemon=_StubDaemon(), port=0, pid_path=tmp_path / "d.pid",
        bind_host="0.0.0.0",
    )
    srv.start()
    time.sleep(0.1)
    resp = _post_mcp(srv.port, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert resp["result"] == {}
    srv.stop()


def test_resolve_daemon_bind_rejects_invalid():
    from scripts.daemon_http import resolve_daemon_bind
    with pytest.raises(ValueError, match="valid IP"):
        resolve_daemon_bind("not-an-ip")
