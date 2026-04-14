from __future__ import annotations
import json, threading, time, urllib.request
import pytest
from pathlib import Path


def _make_server_with_files(tmp_path):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.daemon_http import DaemonHTTPServer

    class _StubDaemon:
        def handle_jsonrpc(self, req):
            return {"jsonrpc": "2.0", "id": req.get("id"), "result": {}}

    srv = DaemonHTTPServer(
        daemon=_StubDaemon(), port=0,
        pid_path=tmp_path / "d.pid",
        event_root=tmp_path / "operator-events",
        state_root=tmp_path / "repl",
    )
    srv.start()
    time.sleep(0.1)
    return srv


def _post(port, path, payload):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"http://localhost:{port}{path}", data=body,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def test_runner_online_writes_discovered_file(tmp_path):
    srv = _make_server_with_files(tmp_path)
    resp = _post(srv.port, "/runner/online",
                 {"runner_profile": "mycader-1", "machine_id": "wkst-A"})
    assert resp["ok"]
    disc = tmp_path / "repl" / "events.jsonl"
    events = [json.loads(l) for l in disc.read_text().splitlines()]
    assert any(e["type"] == "runner_discovered" and e["runner_profile"] == "mycader-1"
               for e in events)
    srv.stop()


def test_runner_event_forwarded_to_events_jsonl(tmp_path):
    srv = _make_server_with_files(tmp_path)
    resp = _post(srv.port, "/runner/event",
                 {"runner_profile": "mycader-1", "machine_id": "wkst-A",
                  "type": "op_event", "ts_ms": 1000, "data": "x"})
    assert resp["ok"]
    profile_events = tmp_path / "repl" / "events-mycader-1.jsonl"
    events = [json.loads(l) for l in profile_events.read_text().splitlines()]
    assert any(e["type"] == "runner_event" for e in events)
    srv.stop()


def test_runner_tracked_in_connected_runners(tmp_path):
    srv = _make_server_with_files(tmp_path)
    _post(srv.port, "/runner/online",
          {"runner_profile": "mycader-1", "machine_id": "wkst-A"})
    with srv._runners_lock:
        assert "mycader-1" in srv._connected_runners
    srv.stop()


def test_popup_correlation_resolves_future(tmp_path):
    """daemon correctly correlates popup_result with waiting caller."""
    srv = _make_server_with_files(tmp_path)

    popup_id = "test-popup-123"
    ev = threading.Event()
    with srv._popup_lock:
        srv._popup_futures[popup_id] = ev

    def _submit_result():
        time.sleep(0.2)
        _post(srv.port, "/runner/popup-result",
              {"popup_id": popup_id, "value": "接管"})

    t = threading.Thread(target=_submit_result, daemon=True)
    t.start()

    fired = ev.wait(timeout=2)
    assert fired
    with srv._popup_lock:
        result = srv._popup_results.get(popup_id, {})
    assert result.get("value") == "接管"
    srv.stop()


def test_runner_monitor_state_written(tmp_path):
    """runner-monitor-state.json is written when runner goes online."""
    srv = _make_server_with_files(tmp_path)
    _post(srv.port, "/runner/online",
          {"runner_profile": "mycader-1", "machine_id": "wkst-A"})
    state_path = tmp_path / "repl" / "runner-monitor-state.json"
    assert state_path.exists()
    state = json.loads(state_path.read_text())
    assert any(r["runner_profile"] == "mycader-1" for r in state["runners"])
    srv.stop()


def test_runner_executor_forwards_event_to_daemon(tmp_path):
    """RunnerExecutor.write_operator_event forwards to daemon when team_lead_url set."""
    from scripts.remote_runner import RunnerExecutor

    srv = _make_server_with_files(tmp_path)

    runner_config = tmp_path / "runner-config.json"
    runner_config.write_text(json.dumps({
        "team_lead_url": f"http://localhost:{srv.port}",
        "runner_profile": "test-runner",
    }))

    ex = RunnerExecutor(
        root=tmp_path,
        state_root=tmp_path / "state",
        runner_config_path=runner_config,
    )
    ex.write_operator_event({
        "machine_id": "wkst-A",
        "type": "test",
        "ts_ms": 1234,
        "data": "hello",
    })
    time.sleep(0.3)  # wait for background thread

    profile_events = tmp_path / "repl" / "events-test-runner.jsonl"
    assert profile_events.exists()
    events = [json.loads(l) for l in profile_events.read_text().splitlines()]
    assert any(e["type"] == "runner_event" for e in events)
    srv.stop()


def test_runner_notify_returns_error_when_no_http_server(tmp_path):
    """runner_notify MCP tool returns error when not in HTTP mode."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.emerge_daemon import EmergeDaemon

    daemon = EmergeDaemon()
    # No _http_server set — should return isError
    result = daemon.call_tool("runner_notify", {
        "runner_profile": "mycader-1",
        "ui_spec": {"type": "choice", "title": "Test"}
    })
    assert result.get("isError") or (
        result.get("content", [{}])[0].get("text", "").startswith("runner_notify")
    )


def test_runner_notify_returns_error_when_runner_not_connected(tmp_path):
    """runner_notify returns error when runner is not connected to SSE."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.emerge_daemon import EmergeDaemon
    from scripts.daemon_http import DaemonHTTPServer

    class _StubDaemon:
        def handle_jsonrpc(self, req):
            return {"jsonrpc": "2.0", "id": req.get("id"), "result": {}}

    srv = DaemonHTTPServer(
        daemon=_StubDaemon(), port=0,
        pid_path=tmp_path / "d.pid",
        event_root=tmp_path / "operator-events",
        state_root=tmp_path / "repl",
    )
    srv.start()
    time.sleep(0.1)

    daemon = EmergeDaemon()
    daemon._http_server = srv

    result = daemon.call_tool("runner_notify", {
        "runner_profile": "not-connected",
        "ui_spec": {"type": "choice", "title": "Test"}
    })
    content_text = result.get("content", [{}])[0].get("text", "")
    result_data = json.loads(content_text)
    assert result_data.get("ok") is False
    assert result_data.get("error") == "runner_not_connected"
    srv.stop()
