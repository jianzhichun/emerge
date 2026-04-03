from __future__ import annotations
import json, time, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _make_flaky_server(fail_count: int, port: int) -> tuple[HTTPServer, list[int]]:
    """Server that returns 503 for first `fail_count` requests then succeeds."""
    call_log: list[int] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            call_log.append(len(call_log) + 1)
            if len(call_log) <= fail_count:
                body = b'{"ok":false,"error":"temporary"}'
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                body = b'{"ok":true,"result":{"isError":false,"content":[{"type":"text","text":"ok"}]}}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def log_message(self, *a): pass

    srv = HTTPServer(("127.0.0.1", port), Handler)
    return srv, call_log


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_call_tool_retries_on_5xx_and_succeeds():
    from scripts.runner_client import RunnerClient, RetryConfig
    port = _free_port()
    srv, log = _make_flaky_server(fail_count=2, port=port)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        client = RunnerClient(
            base_url=f"http://127.0.0.1:{port}",
            timeout_s=5.0,
            retry=RetryConfig(max_attempts=4, base_delay_s=0.01, max_delay_s=0.05),
        )
        result = client.call_tool("icc_exec", {"code": "x=1"})
        assert result["isError"] is False
        assert len(log) == 3  # 2 failures + 1 success
    finally:
        srv.shutdown()


def test_call_tool_raises_after_max_attempts():
    from scripts.runner_client import RunnerClient, RetryConfig
    import pytest
    port = _free_port()
    srv, log = _make_flaky_server(fail_count=99, port=port)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        client = RunnerClient(
            base_url=f"http://127.0.0.1:{port}",
            timeout_s=5.0,
            retry=RetryConfig(max_attempts=2, base_delay_s=0.01, max_delay_s=0.05),
        )
        with pytest.raises(RuntimeError, match="runner http 503"):
            client.call_tool("icc_exec", {"code": "x=1"})
        assert len(log) == 2
    finally:
        srv.shutdown()


def test_retry_config_defaults():
    from scripts.runner_client import RetryConfig
    r = RetryConfig()
    assert r.max_attempts == 3
    assert r.base_delay_s == 0.5
    assert r.max_delay_s == 10.0
