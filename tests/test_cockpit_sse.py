# tests/test_cockpit_sse.py
from __future__ import annotations
import json
import threading
import time
import urllib.request
import pytest


def _start_cockpit_server(tmp_path, monkeypatch):
    """Start cockpit HTTP server via cmd_serve; return base URL."""
    monkeypatch.setenv("EMERGE_REPL_ROOT", str(tmp_path))
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(tmp_path / "connectors"))
    (tmp_path / "connectors").mkdir(exist_ok=True)
    from scripts.repl_admin import cmd_serve
    result = cmd_serve(port=0, open_browser=False)
    assert result["ok"]
    return result["url"]


def test_sse_status_returns_online_event(tmp_path, monkeypatch):
    """GET /api/sse/status must stream an online event immediately."""
    url = _start_cockpit_server(tmp_path, monkeypatch)
    resp = urllib.request.urlopen(f"{url}/api/sse/status", timeout=3)
    assert "text/event-stream" in resp.headers["Content-Type"]
    line = resp.readline().decode().strip()
    assert line.startswith("data:")
    data = json.loads(line[5:])
    assert data["status"] == "online"
    assert "pid" in data


def test_sse_broadcast_reaches_connected_client(tmp_path, monkeypatch):
    """cockpit._sse_broadcast must push events to connected SSE clients."""
    monkeypatch.setenv("EMERGE_REPL_ROOT", str(tmp_path))
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(tmp_path / "connectors"))
    (tmp_path / "connectors").mkdir(exist_ok=True)
    from scripts.repl_admin import cmd_serve
    srv = cmd_serve(port=0, open_browser=False)
    assert srv["ok"]
    cockpit = srv["cockpit"]
    url = srv["url"]
    resp = urllib.request.urlopen(f"{url}/api/sse/status", timeout=3)
    # SSE format: "data: {...}\n\n" — consume both the data line and the blank separator
    resp.readline()  # data: {...}\n
    resp.readline()  # blank \n separator
    # Wait for this newly-opened connection to register itself in cockpit._sse_clients.
    deadline = time.time() + 2.0
    while len(cockpit._sse_clients) == 0 and time.time() < deadline:
        time.sleep(0.01)
    cockpit.broadcast({"status": "test_event", "x": 42})
    time.sleep(0.1)
    line = resp.readline().decode().strip()
    assert line.startswith("data:")
    data = json.loads(line[5:])
    assert data["x"] == 42


def test_sse_broadcast_pending_on_submit(tmp_path, monkeypatch):
    """POST /api/submit must broadcast a pending=true event via SSE."""
    import scripts.repl_admin as repl_admin
    start_clients = len(repl_admin._sse_clients)
    url = _start_cockpit_server(tmp_path, monkeypatch)
    resp = urllib.request.urlopen(f"{url}/api/sse/status", timeout=3)
    resp.readline()  # data: {status: online}\n
    resp.readline()  # blank separator

    deadline = time.time() + 2.0
    while len(repl_admin._sse_clients) <= start_clients and time.time() < deadline:
        time.sleep(0.01)

    body = json.dumps({"actions": [{"type": "pipeline-delete", "key": "x"}]}).encode()
    req = urllib.request.Request(
        f"{url}/api/submit", data=body,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req) as r:
        assert json.loads(r.read())["ok"] is True

    time.sleep(0.2)
    line = resp.readline().decode().strip()
    assert line.startswith("data:")
    data = json.loads(line[5:])
    assert data["pending"] is True


def test_sse_initial_event_has_status_online(tmp_path, monkeypatch):
    """Initial SSE event must have status=online with pid and ts_ms."""
    url = _start_cockpit_server(tmp_path, monkeypatch)
    resp = urllib.request.urlopen(f"{url}/api/sse/status", timeout=3)
    line = resp.readline().decode().strip()
    assert line.startswith("data:")
    data = json.loads(line[5:])
    assert data["status"] == "online"
    assert isinstance(data["pid"], int)
    assert isinstance(data["ts_ms"], int)
