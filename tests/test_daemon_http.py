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
