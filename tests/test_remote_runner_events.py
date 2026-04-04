# tests/test_remote_runner_events.py
from __future__ import annotations
import json
import socket
import threading
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import urllib.request
import urllib.parse
import urllib.error
import pytest
from scripts.remote_runner import RunnerExecutor, RunnerHTTPHandler, ThreadingHTTPServer


class _RunnerServer:
    def __init__(self, state_root: Path) -> None:
        self._state_root = state_root
        self._server = None
        self._thread = None
        self.url = ""

    def __enter__(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        host, port = sock.getsockname()
        sock.close()
        executor = RunnerExecutor(root=ROOT, state_root=self._state_root)
        handler_cls = type("H", (RunnerHTTPHandler,), {"executor": executor})
        self._server = ThreadingHTTPServer((host, port), handler_cls)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.url = f"http://{host}:{port}"
        return self

    def __exit__(self, *_):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)


def _post_event(base_url: str, event: dict) -> dict:
    body = json.dumps(event).encode()
    req = urllib.request.Request(
        f"{base_url}/operator-event",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def _get_events(base_url: str, machine_id: str, since_ms: int = 0) -> dict:
    url = f"{base_url}/operator-events?machine_id={urllib.parse.quote(machine_id)}&since_ms={since_ms}"
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.loads(resp.read())


def test_post_operator_event_stores_event(tmp_path):
    with _RunnerServer(tmp_path / "state") as server:
        event = {
            "ts_ms": 1000,
            "machine_id": "test-m1",
            "session_role": "operator",
            "event_type": "entity_added",
            "app": "zwcad",
            "payload": {"content": "主卧"},
        }
        result = _post_event(server.url, event)
        assert result.get("ok") is True

        events_file = tmp_path / "operator-events" / "test-m1" / "events.jsonl"
        assert events_file.exists()
        lines = [json.loads(l) for l in events_file.read_text().splitlines() if l.strip()]
        assert len(lines) == 1
        assert lines[0]["event_type"] == "entity_added"


def test_get_operator_events_returns_events(tmp_path):
    with _RunnerServer(tmp_path / "state") as server:
        for i in range(3):
            _post_event(server.url, {
                "ts_ms": 1000 + i * 1000,
                "machine_id": "test-m2",
                "session_role": "operator",
                "event_type": "entity_added",
                "app": "zwcad",
                "payload": {"content": f"room_{i}"},
            })
        result = _get_events(server.url, "test-m2", since_ms=0)
        assert result.get("ok") is True
        assert len(result["events"]) == 3


def test_get_operator_events_filters_by_since_ms(tmp_path):
    with _RunnerServer(tmp_path / "state") as server:
        for ts in (1000, 2000, 5000):
            _post_event(server.url, {
                "ts_ms": ts,
                "machine_id": "test-m3",
                "session_role": "operator",
                "event_type": "entity_added",
                "app": "zwcad",
                "payload": {},
            })
        result = _get_events(server.url, "test-m3", since_ms=2001)
        assert result.get("ok") is True
        assert len(result["events"]) == 1
        assert result["events"][0]["ts_ms"] == 5000


def test_post_event_rejects_missing_machine_id(tmp_path):
    with _RunnerServer(tmp_path / "state") as server:
        body = json.dumps({"event_type": "entity_added"}).encode()
        req = urllib.request.Request(
            f"{server.url}/operator-event",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            assert False, "should have raised"
        except urllib.error.HTTPError as e:
            assert e.code == 400
