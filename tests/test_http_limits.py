from __future__ import annotations

import http.client
import json
import sys
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_daemon_rejects_oversized_post_before_route_logic(tmp_path, monkeypatch):
    from scripts.daemon_http import DaemonHTTPServer

    monkeypatch.setenv("EMERGE_MAX_REQUEST_BYTES", "8")

    class _StubDaemon:
        def handle_jsonrpc(self, _req):
            raise AssertionError("route logic should not run")

    srv = DaemonHTTPServer(daemon=_StubDaemon(), port=0, pid_path=tmp_path / "d.pid")
    srv.start()
    try:
        conn = http.client.HTTPConnection("localhost", srv.port, timeout=5)
        conn.request("POST", "/mcp", body=b"0123456789", headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        assert resp.status == 413
    finally:
        srv.stop()


def test_bounded_threading_http_server_rejects_when_capacity_exhausted(tmp_path, monkeypatch):
    from http.server import BaseHTTPRequestHandler

    from scripts.http_limits import BoundedThreadingHTTPServer

    monkeypatch.setenv("EMERGE_HTTP_MAX_CONNECTIONS", "1")
    started = threading.Event()
    release = threading.Event()

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            started.set()
            release.wait(timeout=5)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args):
            pass

    srv = BoundedThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    first = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    try:
        threading.Thread(target=lambda: first.request("GET", "/hold"), daemon=True).start()
        assert started.wait(timeout=5)
        second = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        second.request("GET", "/second")
        resp = second.getresponse()
        assert resp.status == 503
    finally:
        release.set()
        srv.shutdown()
        srv.server_close()
