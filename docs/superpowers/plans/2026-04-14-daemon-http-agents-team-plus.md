# Daemon HTTP + Agents-Team Phase 1+ Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate emerge daemon from stdio to HTTP MCP server, enable runner→daemon push via SSE, unify event streams, auto-start cockpit, and add agents-team Monitors tab.

**Architecture:** A new `DaemonHTTPServer` runs as a single persistent process (port 8789), shared by all CC sessions and watcher subagents. Runner machines connect via SSE, pushing events and receiving commands in real-time. A unified `watch_emerge.py` replaces the three existing watch scripts.

**Tech Stack:** Python stdlib (`http.server`, `threading`, `urllib.request`), existing `EventRouter`, `PatternDetector`, `OperatorMonitor`, MCP 2025-11-25 HTTP Streamable Transport

---

## File Structure

| File | Change |
|---|---|
| `scripts/daemon_http.py` | **NEW** — `DaemonHTTPServer`: HTTP MCP transport, runner SSE hub, popup correlation, ensure-running logic |
| `scripts/watch_emerge.py` | **NEW** — unified tail watcher (global/runner/local modes) |
| `scripts/emerge_daemon.py` | Modify: import `DaemonHTTPServer`, add `--ensure-running` entry point, `runner_notify` MCP tool, cockpit auto-start |
| `scripts/remote_runner.py` | Modify: SSE client to daemon on startup, forward events, dispatch SSE commands |
| `scripts/runner_client.py` | Modify: `notify()` → daemon MCP `runner_notify` tool (with fallback) |
| `scripts/operator_monitor.py` | Modify: remove HTTP poll loop, keep `process_local_file` |
| `scripts/repl_admin.py` | Modify: add `GET /api/control-plane/monitors`, `_sse_broadcast(monitors_updated)` |
| `scripts/cockpit_shell.html` | Modify: add Monitors tab |
| `scripts/pending_actions.py` | Modify: add `format_runner_discovered`, `format_runner_event`, `format_runner_online` |
| `scripts/watch_patterns.py` | Modify: shim → `watch_emerge.py --runner-profile` |
| `scripts/watch_pending.py` | Modify: shim → `watch_emerge.py` |
| `.claude-plugin/plugin.json` | Modify: `url: "http://localhost:8789/mcp"`, remove `command` |
| `hooks/session_start.py` | Modify: launch daemon if not running |
| `commands/cockpit.md` | Modify: simplify to URL print + monitor launch |
| `commands/monitor.md` | Modify: watcher prompt uses `runner_notify` MCP tool |
| `tests/test_daemon_http.py` | **NEW** — DaemonHTTPServer unit tests |
| `tests/test_runner_push.py` | **NEW** — runner SSE + popup correlation tests |
| `tests/test_watch_emerge.py` | **NEW** — watch_emerge.py tests |

---

## Phase A — Daemon HTTP MCP Server

### Task 1: DaemonHTTPServer — basic HTTP MCP transport

**Context:** `emerge_daemon.py` currently runs as a stdio MCP server. `EmergeDaemon.handle_jsonrpc(request)` already handles all MCP methods. We need to wrap this in an HTTP server where CC sessions POST JSON-RPC to `/mcp` and get responses back. We also need per-session SSE channels so the daemon can push `elicitations/create` requests (used by `_elicit()`). Each CC session GETs `/mcp` to establish its SSE channel, then POSTs `/mcp?session_id=<id>` for tool calls.

**Files:**
- Create: `scripts/daemon_http.py`
- Create: `tests/test_daemon_http.py`

- [ ] **Step 1: Write the failing tests**

```python
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
    """Start a DaemonHTTPServer with a minimal stub daemon and return (server, port)."""
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
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python -m pytest tests/test_daemon_http.py -q 2>&1 | head -20
```

Expected: `ImportError: cannot import name 'DaemonHTTPServer'`

- [ ] **Step 3: Implement `scripts/daemon_http.py`**

```python
# scripts/daemon_http.py
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    pass

_KEEPALIVE_INTERVAL_S = 20.0


class DaemonHTTPServer:
    """HTTP MCP transport for EmergeDaemon.

    Exposes two URL patterns on the same port:
      POST /mcp[?session_id=<id>]  — JSON-RPC request/response
      GET  /mcp                    — SSE channel (server → client pushes)

    All runner endpoints (/runner/*) are separate and added in Phase B.
    """

    def __init__(
        self,
        daemon: Any,
        port: int = 8789,
        pid_path: Path | None = None,
    ) -> None:
        self._daemon = daemon
        self._port = port
        self._pid_path = pid_path or (Path.home() / ".emerge" / "daemon.pid")
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        # session_id → wfile for SSE push
        self._sse_sessions: dict[str, Any] = {}
        self._sse_lock = threading.Lock()
        # Connected runners: runner_profile → {connected_at_ms, last_event_ts_ms, machine_id}
        self._connected_runners: dict[str, dict] = {}
        self._runners_lock = threading.Lock()
        # popup_id → threading.Future
        self._popup_futures: dict[str, threading.Event] = {}
        self._popup_results: dict[str, dict] = {}
        self._popup_lock = threading.Lock()

    @property
    def port(self) -> int:
        if self._server is None:
            return self._port
        return self._server.server_address[1]

    def start(self) -> None:
        handler = _make_handler(self)
        self._server = ThreadingHTTPServer(("127.0.0.1", self._port), handler)
        self._port = self._server.server_address[1]
        self._write_pid()
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="DaemonHTTPServer"
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
        self._pid_path.unlink(missing_ok=True)

    def _write_pid(self) -> None:
        self._pid_path.parent.mkdir(parents=True, exist_ok=True)
        self._pid_path.write_text(
            json.dumps({"pid": os.getpid(), "port": self.port}), encoding="utf-8"
        )

    def push_to_session(self, session_id: str, payload: dict) -> bool:
        """Push a JSON-RPC notification to a connected CC session via SSE."""
        with self._sse_lock:
            wfile = self._sse_sessions.get(session_id)
        if wfile is None:
            return False
        try:
            line = f"data: {json.dumps(payload)}\n\n"
            wfile.write(line.encode())
            wfile.flush()
            return True
        except OSError:
            with self._sse_lock:
                self._sse_sessions.pop(session_id, None)
            return False

    def handle_post_mcp(self, body: bytes, session_id: str | None) -> dict:
        """Dispatch a JSON-RPC request to the daemon."""
        try:
            req = json.loads(body)
        except json.JSONDecodeError as exc:
            return {"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": f"Parse error: {exc}"}}
        # Elicitation responses: wake waiting threads
        req_id = req.get("id")
        if req_id and req_id in self._popup_futures:
            self._popup_results[req_id] = req.get("result") or {}
            self._popup_futures[req_id].set()
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}
        try:
            resp = self._daemon.handle_jsonrpc(req)
        except Exception as exc:
            resp = {"jsonrpc": "2.0", "id": req_id,
                    "error": {"code": -32603, "message": str(exc)}}
        return resp or {"jsonrpc": "2.0", "id": req_id, "result": {}}

    def handle_get_mcp_sse(self, wfile: Any) -> str:
        """Register an SSE client, return session_id."""
        session_id = uuid.uuid4().hex
        with self._sse_lock:
            self._sse_sessions[session_id] = wfile
        return session_id

    def remove_sse_session(self, session_id: str) -> None:
        with self._sse_lock:
            self._sse_sessions.pop(session_id, None)


def _make_handler(srv: DaemonHTTPServer):
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass  # suppress default access log

        def _send_json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            import urllib.parse as _up
            path = _up.urlparse(self.path).path
            if path == "/mcp":
                accept = self.headers.get("Accept", "")
                if "text/event-stream" in accept:
                    self._handle_sse_mcp()
                else:
                    self._send_json(200, {"ok": True, "service": "emerge-daemon"})
            elif path == "/health":
                self._send_json(200, {"ok": True})
            else:
                self._send_json(404, {"ok": False, "error": "not_found"})

        def _handle_sse_mcp(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            session_id = srv.handle_get_mcp_sse(self.wfile)
            # Send session_id as first event
            msg = json.dumps({"session_id": session_id})
            self.wfile.write(f"data: {msg}\n\n".encode())
            self.wfile.flush()
            try:
                while True:
                    time.sleep(_KEEPALIVE_INTERVAL_S)
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except OSError:
                pass
            finally:
                srv.remove_sse_session(session_id)

        def do_POST(self):  # noqa: N802
            import urllib.parse as _up
            parsed = _up.urlparse(self.path)
            path = parsed.path
            qs = _up.parse_qs(parsed.query)
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b""
            if path == "/mcp":
                session_id = qs.get("session_id", [None])[0]
                resp = srv.handle_post_mcp(body, session_id)
                self._send_json(200, resp)
            else:
                self._send_json(404, {"ok": False, "error": "not_found"})

    return _Handler
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_daemon_http.py -q
```

Expected: `3 passed`

- [ ] **Step 5: Commit**

```bash
git add scripts/daemon_http.py tests/test_daemon_http.py
git commit -m "feat: DaemonHTTPServer — HTTP MCP transport with SSE sessions (Phase A)"
```

---

### Task 2: `--ensure-running` mode, `plugin.json` migration, `session_start.py` update

**Context:** In HTTP mode, CC uses `url:` in `plugin.json` to connect to an already-running server. CC no longer spawns the process. `session_start.py` must launch the daemon if it's not running. `emerge_daemon.py` needs an `--ensure-running` entry point that daemonizes the process.

**Files:**
- Modify: `scripts/emerge_daemon.py` (add `run_http()` + `ensure_running()` + `--ensure-running` CLI arg)
- Modify: `.claude-plugin/plugin.json`
- Modify: `hooks/session_start.py`

- [ ] **Step 1: Write the failing test**

```python
# In tests/test_daemon_http.py — add this test
def test_ensure_running_noop_when_already_running(tmp_path):
    """ensure_running() returns early if daemon is already alive."""
    from scripts.daemon_http import DaemonHTTPServer, ensure_running_or_launch

    pid_path = tmp_path / "d.pid"

    class _StubDaemon:
        def handle_jsonrpc(self, req):
            return {"jsonrpc": "2.0", "id": req.get("id"), "result": {}}

    srv = DaemonHTTPServer(daemon=_StubDaemon(), port=0, pid_path=pid_path)
    srv.start()
    port = srv.port
    time.sleep(0.1)

    # Second call should detect running server and not start a new one
    result = ensure_running_or_launch(pid_path=pid_path, port=0, daemon_factory=None)
    assert result == "already_running"
    assert srv.port == port  # port unchanged
    srv.stop()
```

- [ ] **Step 2: Run test to confirm it fails**

```bash
python -m pytest tests/test_daemon_http.py::test_ensure_running_noop_when_already_running -q
```

Expected: `ImportError: cannot import name 'ensure_running_or_launch'`

- [ ] **Step 3: Add `ensure_running_or_launch` to `scripts/daemon_http.py`**

Add after the `DaemonHTTPServer` class:

```python
def ensure_running_or_launch(
    pid_path: Path | None = None,
    port: int = 8789,
    daemon_factory: Any = None,
) -> str:
    """Check if daemon is running via PID file; return 'already_running' or 'launched'.

    When daemon_factory is None, this is detection-only (used in tests).
    """
    import os as _os
    pid_path = pid_path or (Path.home() / ".emerge" / "daemon.pid")
    if pid_path.exists():
        try:
            info = json.loads(pid_path.read_text(encoding="utf-8"))
            pid = int(info["pid"])
            _os.kill(pid, 0)  # raises ProcessLookupError if dead
            return "already_running"
        except (ProcessLookupError, KeyError, ValueError, json.JSONDecodeError):
            pid_path.unlink(missing_ok=True)
    if daemon_factory is None:
        return "not_running"
    daemon_obj = daemon_factory()
    srv = DaemonHTTPServer(daemon=daemon_obj, port=port, pid_path=pid_path)
    srv.start()
    return "launched"
```

- [ ] **Step 4: Add `run_http()` entry point to `scripts/emerge_daemon.py`**

At the bottom of `emerge_daemon.py`, before `if __name__ == "__main__":`, add:

```python
def run_http(port: int = 8789) -> None:
    """Start emerge daemon in HTTP MCP server mode."""
    import atexit
    from scripts.daemon_http import DaemonHTTPServer

    daemon = EmergeDaemon()
    daemon.start_operator_monitor()
    daemon.start_event_router()
    atexit.register(daemon.stop_operator_monitor)
    atexit.register(daemon.stop_event_router)

    pid_path = Path.home() / ".emerge" / "daemon.pid"
    srv = DaemonHTTPServer(daemon=daemon, port=port, pid_path=pid_path)
    # Patch _write_mcp_push to push via HTTP SSE instead of stdout
    # (session_id context is threaded via threading.local set by _Handler)
    srv.start()
    print(f"Emerge daemon HTTP server running on port {srv.port}", flush=True)
    try:
        threading.Event().wait()  # block forever
    except KeyboardInterrupt:
        pass
    finally:
        srv.stop()
```

Change `if __name__ == "__main__":` block:

```python
if __name__ == "__main__":
    import argparse as _ap
    _p = _ap.ArgumentParser()
    _p.add_argument("--http", action="store_true", help="Run as HTTP MCP server")
    _p.add_argument("--port", type=int, default=8789)
    _p.add_argument("--ensure-running", action="store_true",
                    help="Launch daemon if not already running, then exit")
    _args = _p.parse_args()
    if _args.ensure_running:
        from scripts.daemon_http import ensure_running_or_launch
        result = ensure_running_or_launch(
            port=_args.port,
            daemon_factory=EmergeDaemon,
        )
        print(result)
    elif _args.http:
        run_http(port=_args.port)
    else:
        run_stdio()
```

- [ ] **Step 5: Update `.claude-plugin/plugin.json`**

Replace `mcpServers.emerge` command block with url:

```json
{
  "name": "emerge",
  "version": "0.3.67",
  "description": "Emerge — policy-driven crystallization flywheel for Claude Code: exec patterns promote to stable pipelines, PreToolUse enforcement, optional remote runner",
  "mcpServers": {
    "emerge": {
      "url": "http://localhost:8789/mcp"
    }
  },
  "permissions": {
    "filesystem": [
      "~/.emerge/"
    ],
    "network": [
      "localhost",
      "192.168.122.0/24"
    ]
  },
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 ${CLAUDE_PLUGIN_ROOT}/scripts/runner_sync.py"
          }
        ]
      }
    ]
  }
}
```

- [ ] **Step 6: Update `hooks/session_start.py` — add daemon ensure-running call**

After `pin_plugin_data_path_if_present()` and before the state load, add:

```python
    # Ensure HTTP daemon is running (idempotent — no-op if already alive)
    import subprocess as _sub
    _plugin_root = Path(__file__).resolve().parents[1]
    try:
        _sub.Popen(
            [sys.executable,
             str(_plugin_root / "scripts" / "emerge_daemon.py"),
             "--ensure-running"],
            start_new_session=True,
            stdout=_sub.DEVNULL,
            stderr=_sub.DEVNULL,
        )
    except Exception:
        pass
```

- [ ] **Step 7: Run the full test suite to verify nothing is broken**

```bash
python -m pytest tests -q 2>&1 | tail -5
```

Expected: all existing tests pass (daemon still runs in stdio mode for tests; HTTP mode is additive)

- [ ] **Step 8: Commit**

```bash
git add scripts/emerge_daemon.py scripts/daemon_http.py .claude-plugin/plugin.json hooks/session_start.py
git commit -m "feat: daemon --ensure-running mode, HTTP entry point, plugin.json url migration"
```

---

### Task 3: `_write_mcp_push` in HTTP mode — thread-local session routing

**Context:** `_elicit()` calls `_write_mcp_push()` to send `elicitations/create` to CC. In HTTP mode, it needs to push to the specific CC session that made the current tool call. We use `threading.local()` to stash the `session_id` in the request handler, so `_write_mcp_push` can look it up.

**Files:**
- Modify: `scripts/daemon_http.py`
- Modify: `scripts/emerge_daemon.py`

- [ ] **Step 1: Write the failing test**

```python
# In tests/test_daemon_http.py — add:
def test_push_to_session_delivers_event(tmp_path):
    """push_to_session sends data to the correct SSE client."""
    srv = _make_server(tmp_path)
    received = []

    def _reader():
        req = urllib.request.Request(
            f"http://localhost:{srv.port}/mcp",
            headers={"Accept": "text/event-stream"})
        with urllib.request.urlopen(req, timeout=3) as r:
            # First line: session_id event
            line1 = r.readline().decode()
            sid = json.loads(line1.split("data: ", 1)[1])["session_id"]
            r.readline()  # blank line
            received.append(sid)
            # Second event: pushed payload
            line2 = r.readline().decode()
            received.append(json.loads(line2.split("data: ", 1)[1]))

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    time.sleep(0.2)

    assert len(received) >= 1, "SSE connection not established"
    sid = received[0]
    srv.push_to_session(sid, {"type": "test", "value": 42})
    t.join(timeout=2)
    assert {"type": "test", "value": 42} in received
    srv.stop()
```

- [ ] **Step 2: Run to confirm it fails (or passes — push_to_session already implemented)**

```bash
python -m pytest tests/test_daemon_http.py::test_push_to_session_delivers_event -q
```

- [ ] **Step 3: Add `_tl` thread-local to `daemon_http.py` and wire up handler**

At the top of `daemon_http.py` add:

```python
import threading as _threading
_tl = _threading.local()  # _tl.session_id: str | None — set per HTTP request
```

In `_make_handler`, in `do_POST` for `/mcp`, set thread-local before dispatching:

```python
            if path == "/mcp":
                session_id = qs.get("session_id", [None])[0]
                _tl.session_id = session_id   # ← add this line
                resp = srv.handle_post_mcp(body, session_id)
                _tl.session_id = None          # ← add this line
                self._send_json(200, resp)
```

Add a helper function to `daemon_http.py`:

```python
def get_current_session_id() -> str | None:
    """Return the session_id of the currently-executing HTTP request thread."""
    return getattr(_tl, "session_id", None)
```

- [ ] **Step 4: Patch `EmergeDaemon._write_mcp_push` to support HTTP mode**

In `emerge_daemon.py`, modify `_write_mcp_push`:

```python
    def _write_mcp_push(self, payload: dict) -> None:
        """Write a JSON-RPC notification/request to the active transport.

        In stdio mode: writes to stdout.
        In HTTP mode: pushes to the current session's SSE channel.
        """
        # HTTP mode: try to push via SSE session
        try:
            from scripts.daemon_http import get_current_session_id
            sid = get_current_session_id()
            if sid and hasattr(self, "_http_server") and self._http_server is not None:
                self._http_server.push_to_session(sid, payload)
                return
        except ImportError:
            pass
        # Fallback: stdio mode
        line = json.dumps(payload) + "\n"
        with _stdout_lock:
            sys.stdout.write(line)
            sys.stdout.flush()
```

In `run_http()`, after creating `srv`, set `daemon._http_server = srv`.

- [ ] **Step 5: Run tests**

```bash
python -m pytest tests/test_daemon_http.py -q
```

Expected: all 4 tests pass

- [ ] **Step 6: Commit**

```bash
git add scripts/daemon_http.py scripts/emerge_daemon.py
git commit -m "feat: HTTP mode _write_mcp_push via thread-local SSE session routing"
```

---

## Phase B — Runner SSE Push

### Task 4: DaemonHTTPServer runner endpoints

**Context:** Runner machines connect to `GET /runner/sse?runner_profile=<name>`, report online via `POST /runner/online`, and forward events via `POST /runner/event`. The daemon writes forwarded events to `~/.emerge/operator-events/{machine_id}/events.jsonl` (same path as before, so EventRouter picks them up unchanged).

**Files:**
- Modify: `scripts/daemon_http.py`
- Create: `tests/test_runner_push.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_runner_push.py
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
```

- [ ] **Step 2: Run to confirm they fail**

```bash
python -m pytest tests/test_runner_push.py -q 2>&1 | head -10
```

Expected: `TypeError: DaemonHTTPServer.__init__() got unexpected keyword arguments`

- [ ] **Step 3: Add `event_root` and `state_root` to `DaemonHTTPServer.__init__`**

Modify `__init__` signature:

```python
    def __init__(
        self,
        daemon: Any,
        port: int = 8789,
        pid_path: Path | None = None,
        event_root: Path | None = None,
        state_root: Path | None = None,
    ) -> None:
        ...
        self._event_root = event_root or (Path.home() / ".emerge" / "operator-events")
        self._state_root = state_root or (Path.home() / ".emerge" / "repl")
```

- [ ] **Step 4: Add runner endpoint handling to `_make_handler` in `daemon_http.py`**

In `do_POST`, before the final `else`:

```python
            elif path == "/runner/online":
                try:
                    payload = json.loads(body) if body else {}
                    profile = str(payload.get("runner_profile", "")).strip()
                    machine_id = str(payload.get("machine_id", "")).strip()
                    if not profile:
                        raise ValueError("runner_profile required")
                    srv._on_runner_online(profile, machine_id)
                    self._send_json(200, {"ok": True})
                except Exception as exc:
                    self._send_json(400, {"ok": False, "error": str(exc)})
            elif path == "/runner/event":
                try:
                    payload = json.loads(body) if body else {}
                    srv._on_runner_event(payload)
                    self._send_json(200, {"ok": True})
                except Exception as exc:
                    self._send_json(400, {"ok": False, "error": str(exc)})
            elif path == "/runner/popup-result":
                try:
                    payload = json.loads(body) if body else {}
                    srv._on_popup_result(payload)
                    self._send_json(200, {"ok": True})
                except Exception as exc:
                    self._send_json(400, {"ok": False, "error": str(exc)})
```

In `do_GET`, add SSE for runner:

```python
            elif path == "/runner/sse":
                import urllib.parse as _up
                qs2 = _up.parse_qs(_up.urlparse(self.path).query)
                profile = qs2.get("runner_profile", [""])[0].strip()
                self._handle_runner_sse(profile)
```

Add `_handle_runner_sse` to the handler class:

```python
        def _handle_runner_sse(self, runner_profile: str):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                while True:
                    time.sleep(15)
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except OSError:
                pass
            finally:
                if runner_profile:
                    with srv._runners_lock:
                        srv._connected_runners.pop(runner_profile, None)
```

- [ ] **Step 5: Add `_on_runner_online`, `_on_runner_event`, `_on_popup_result` to `DaemonHTTPServer`**

```python
    def _on_runner_online(self, runner_profile: str, machine_id: str) -> None:
        import re as _re, time as _time
        if not _re.fullmatch(r"[a-zA-Z0-9_.-]+", runner_profile) or len(runner_profile) > 64:
            raise ValueError(f"invalid runner_profile: {runner_profile!r}")
        with self._runners_lock:
            self._connected_runners[runner_profile] = {
                "connected_at_ms": int(_time.time() * 1000),
                "last_event_ts_ms": 0,
                "machine_id": machine_id,
                "last_alert": None,
            }
        # Append to global events.jsonl
        self._append_event(self._state_root / "events.jsonl", {
            "type": "runner_discovered",
            "ts_ms": int(_time.time() * 1000),
            "runner_profile": runner_profile,
            "machine_id": machine_id,
        })
        # Append to per-runner events
        self._append_event(self._state_root / f"events-{runner_profile}.jsonl", {
            "type": "runner_online",
            "ts_ms": int(_time.time() * 1000),
            "runner_profile": runner_profile,
            "machine_id": machine_id,
        })

    def _on_runner_event(self, payload: dict) -> None:
        import time as _time
        runner_profile = str(payload.get("runner_profile", "")).strip()
        machine_id = str(payload.get("machine_id", "")).strip()
        ts_ms = int(_time.time() * 1000)
        # Write to EventBus (operator-events/{machine_id}/events.jsonl)
        if machine_id:
            machine_dir = self._event_root / machine_id
            machine_dir.mkdir(parents=True, exist_ok=True)
            import json as _j
            with (machine_dir / "events.jsonl").open("a", encoding="utf-8") as f:
                f.write(_j.dumps(payload, ensure_ascii=False) + "\n")
        # Write to per-runner event stream
        if runner_profile:
            with self._runners_lock:
                if runner_profile in self._connected_runners:
                    self._connected_runners[runner_profile]["last_event_ts_ms"] = ts_ms
            self._append_event(self._state_root / f"events-{runner_profile}.jsonl", {
                "type": "runner_event",
                "ts_ms": ts_ms,
                "runner_profile": runner_profile,
                **{k: v for k, v in payload.items()
                   if k not in ("runner_profile",)},
            })

    def _on_popup_result(self, payload: dict) -> None:
        popup_id = str(payload.get("popup_id", "")).strip()
        if not popup_id:
            return
        with self._popup_lock:
            self._popup_results[popup_id] = payload
            ev = self._popup_futures.get(popup_id)
        if ev:
            ev.set()

    def _append_event(self, path: Path, event: dict) -> None:
        import json as _j
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(_j.dumps(event, ensure_ascii=False) + "\n")
```

- [ ] **Step 6: Run tests**

```bash
python -m pytest tests/test_runner_push.py -q
```

Expected: `3 passed`

- [ ] **Step 7: Commit**

```bash
git add scripts/daemon_http.py tests/test_runner_push.py
git commit -m "feat: daemon runner endpoints — /runner/online /runner/event /runner/popup-result"
```

---

### Task 5: `remote_runner.py` — SSE client + event forwarding

**Context:** `remote_runner.py` runs on the runner machine. On startup, it should connect to the team lead daemon's SSE endpoint and report online. When `POST /operator-event` is called locally, it should forward the event to the daemon. The team lead URL is read from `~/.emerge/runner-config.json` (key: `team_lead_url`).

**Files:**
- Modify: `scripts/remote_runner.py`
- Modify: `tests/test_runner_push.py` (add integration test)

- [ ] **Step 1: Write the failing test**

```python
# Append to tests/test_runner_push.py:

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
    time.sleep(0.2)

    profile_events = tmp_path / "repl" / "events-test-runner.jsonl"
    assert profile_events.exists()
    events = [json.loads(l) for l in profile_events.read_text().splitlines()]
    assert any(e["type"] == "runner_event" for e in events)
    srv.stop()
```

- [ ] **Step 2: Run to confirm it fails**

```bash
python -m pytest tests/test_runner_push.py::test_runner_executor_forwards_event_to_daemon -q
```

Expected: `TypeError: RunnerExecutor.__init__() got unexpected keyword argument 'runner_config_path'`

- [ ] **Step 3: Add `runner_config_path` to `RunnerExecutor.__init__` and load team_lead config**

In `remote_runner.py`, modify `RunnerExecutor.__init__`:

```python
    def __init__(self, root: Path | None = None, state_root: Path | None = None,
                 runner_config_path: Path | None = None) -> None:
        resolved_root = root or ROOT
        self._root = resolved_root
        self._script_roots = self._resolve_script_roots()
        self._state_root = (state_root or default_exec_root()).expanduser().resolve()
        self._base_session_id = derive_session_id(os.environ.get("EMERGE_SESSION_ID"), resolved_root)
        self._sessions_by_profile: dict[str, ExecSession] = {}
        self._repl_lock = threading.Lock()
        self._event_write_lock = threading.Lock()
        self._event_root = self._state_root.parent / "operator-events"
        # Team lead config (optional)
        cfg_path = runner_config_path or (Path.home() / ".emerge" / "runner-config.json")
        self._team_lead_url: str = ""
        self._runner_profile: str = ""
        self._load_team_lead_config(cfg_path)
```

Add `_load_team_lead_config`:

```python
    def _load_team_lead_config(self, path: Path) -> None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self._team_lead_url = str(data.get("team_lead_url", "")).rstrip("/")
            self._runner_profile = str(data.get("runner_profile", "")).strip()
        except (OSError, json.JSONDecodeError):
            pass
```

- [ ] **Step 4: Forward events to daemon in `write_operator_event`**

In `write_operator_event`, add forwarding after writing to local EventBus:

```python
    def write_operator_event(self, event: dict) -> None:
        machine_id = str(event.get("machine_id", "")).strip()
        _validate_machine_id(machine_id)
        machine_dir = self._event_root / machine_id
        machine_dir.mkdir(parents=True, exist_ok=True)
        events_path = machine_dir / "events.jsonl"
        with self._event_write_lock:
            with events_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        # Forward to team lead daemon (fire-and-forget, non-blocking)
        if self._team_lead_url and self._runner_profile:
            self._forward_event_to_daemon(event)

    def _forward_event_to_daemon(self, event: dict) -> None:
        """Forward event to team lead daemon. Runs in background thread."""
        import urllib.request as _ur
        import urllib.error as _ue
        url = f"{self._team_lead_url}/runner/event"
        payload = {**event, "runner_profile": self._runner_profile}
        body = json.dumps(payload, ensure_ascii=True).encode()
        req = _ur.Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            with _ur.urlopen(req, timeout=3) as _r:
                pass
        except (_ue.URLError, OSError):
            pass  # best-effort, never block operator
```

- [ ] **Step 5: Run tests**

```bash
python -m pytest tests/test_runner_push.py -q
```

Expected: `4 passed`

- [ ] **Step 6: Commit**

```bash
git add scripts/remote_runner.py tests/test_runner_push.py
git commit -m "feat: remote_runner forwards events to team lead daemon"
```

---

### Task 6: runner-bootstrap `--team-lead-url` + remove OperatorMonitor HTTP poll

**Context:** `runner-bootstrap` should write `team_lead_url` and `runner_profile` to the runner's `~/.emerge/runner-config.json`. Also, since runner now pushes events to daemon, the HTTP poll loop in `OperatorMonitor` is no longer needed. Remove `_RunnerClientAdapter` and poll loop; keep `process_local_file` intact.

**Files:**
- Modify: `scripts/repl_admin.py` (`cmd_runner_bootstrap`)
- Modify: `scripts/emerge_daemon.py` (`start_operator_monitor`, remove `_RunnerClientAdapter` usage)
- Modify: `scripts/operator_monitor.py` (remove poll loop, keep `process_local_file`)

- [ ] **Step 1: Write the failing test**

```python
# Append to tests/test_runner_push.py:

def test_operator_monitor_has_no_poll_loop(tmp_path):
    """OperatorMonitor with empty machines dict should still start/stop cleanly."""
    from scripts.operator_monitor import OperatorMonitor
    pushed = []
    mon = OperatorMonitor(
        machines={},
        push_fn=lambda s, c, sum: pushed.append(s),
        poll_interval_s=0.1,
        event_root=tmp_path / "events",
    )
    mon.start()
    time.sleep(0.3)
    mon.stop()
    assert pushed == []  # no events, no push
```

- [ ] **Step 2: Run to confirm it passes already (or fails if poll loop breaks)**

```bash
python -m pytest tests/test_runner_push.py::test_operator_monitor_has_no_poll_loop -q
```

- [ ] **Step 3: Remove HTTP poll loop from `OperatorMonitor.run()`**

In `scripts/operator_monitor.py`, replace the `run()` method:

```python
    def run(self) -> None:
        """Block until stop() is called. Operator events arrive via process_local_file()."""
        self._stop_event.wait()
```

Remove the `_machines` dict usage from `__init__` (keep parameter for backward compat but ignore it):

```python
    def __init__(
        self,
        machines: dict[str, Any],
        push_fn: Callable[[str, dict, PatternSummary], None],
        poll_interval_s: float = 5.0,
        event_root: Path | None = None,
        adapter_root: Path | None = None,
    ) -> None:
        super().__init__(daemon=True, name="OperatorMonitor")
        # machines parameter kept for API compatibility; polling removed (runner pushes events)
        self._push_fn = push_fn
        self._poll_interval_s = poll_interval_s  # kept but unused
        self._event_root = event_root or (Path.home() / ".emerge" / "operator-events")
        self._adapter_registry = AdapterRegistry(adapter_root=adapter_root)
        self._detector = PatternDetector()
        self._last_poll_ms: dict[str, int] = {}
        self._event_buffers: dict[str, deque] = {}
        self._stop_event = threading.Event()
```

- [ ] **Step 4: Add `--team-lead-url` to `cmd_runner_bootstrap` in `repl_admin.py`**

Find `cmd_runner_bootstrap` (around line 759) and add the parameter and remote config write:

```python
def cmd_runner_bootstrap(
    *,
    ssh_target: str,
    target_profile: str,
    runner_url: str,
    team_lead_url: str = "",
    python_bin: str = "python3",
) -> dict:
```

In the function body, after writing runner scripts, add a step to write runner-config.json on the remote:

```python
    if team_lead_url:
        runner_cfg = json.dumps({
            "team_lead_url": team_lead_url.rstrip("/"),
            "runner_profile": target_profile,
        }, indent=2)
        _ssh_run(ssh_target, f"mkdir -p ~/.emerge && echo '{runner_cfg}' > ~/.emerge/runner-config.json")
```

Also update the argparse block at the bottom:

```python
    parser.add_argument("--team-lead-url", default="",
                        help="Team lead daemon URL (e.g. http://192.168.1.100:8789)")
```

And in `elif args.command == "runner-bootstrap":` dispatch:

```python
        out = cmd_runner_bootstrap(
            ssh_target=args.ssh_target,
            target_profile=args.target_profile,
            runner_url=args.runner_url,
            team_lead_url=getattr(args, "team_lead_url", ""),
            python_bin=args.python_bin,
        )
```

- [ ] **Step 5: Run tests**

```bash
python -m pytest tests -q 2>&1 | tail -5
```

Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add scripts/operator_monitor.py scripts/repl_admin.py scripts/emerge_daemon.py
git commit -m "feat: remove OperatorMonitor HTTP poll loop (runner pushes), add --team-lead-url to bootstrap"
```

---

## Phase C — Popup via SSE

### Task 7: `runner_notify` MCP tool + popup correlation

**Context:** Watcher agents call `runner_notify(runner_profile, ui_spec)`. The daemon SSE-pushes a `notify` command to the runner's SSE connection, waits for `POST /runner/popup-result` with a matching `popup_id`, and returns the result to the MCP caller.

**Files:**
- Modify: `scripts/daemon_http.py` (add `request_popup` method)
- Modify: `scripts/emerge_daemon.py` (add `runner_notify` MCP tool)

- [ ] **Step 1: Write the failing test**

```python
# Append to tests/test_runner_push.py:

def test_popup_correlation_resolves_future(tmp_path):
    """daemon correctly correlates popup_result with waiting caller."""
    srv = _make_server_with_files(tmp_path)

    # Simulate runner: submit popup result after brief delay
    def _submit_result(popup_id):
        time.sleep(0.2)
        _post(srv.port, "/runner/popup-result",
              {"popup_id": popup_id, "value": "接管"})

    # Pre-register a future
    popup_id = "test-popup-123"
    ev = threading.Event()
    with srv._popup_lock:
        srv._popup_futures[popup_id] = ev

    t = threading.Thread(target=_submit_result, args=(popup_id,), daemon=True)
    t.start()

    fired = ev.wait(timeout=2)
    assert fired
    with srv._popup_lock:
        result = srv._popup_results.get(popup_id, {})
    assert result.get("value") == "接管"
    srv.stop()
```

- [ ] **Step 2: Run to confirm it passes (future resolution already implemented)**

```bash
python -m pytest tests/test_runner_push.py::test_popup_correlation_resolves_future -q
```

- [ ] **Step 3: Add `request_popup` method to `DaemonHTTPServer`**

```python
    def request_popup(
        self,
        runner_profile: str,
        ui_spec: dict,
        timeout_s: float = 30.0,
    ) -> dict:
        """Send popup to runner via SSE, wait for result. Blocks calling thread."""
        import uuid as _uuid, time as _time
        popup_id = _uuid.uuid4().hex
        ev = threading.Event()
        with self._popup_lock:
            self._popup_futures[popup_id] = ev
        # Push via runner SSE (need per-runner wfile)
        # For now: push to all connected SSE sessions (runner subscribes on /runner/sse)
        command = json.dumps({
            "type": "notify",
            "popup_id": popup_id,
            "ui_spec": ui_spec,
        })
        with self._runners_lock:
            wfile = self._runner_sse_clients.get(runner_profile)
        if wfile is not None:
            try:
                wfile.write(f"data: {command}\n\n".encode())
                wfile.flush()
            except OSError:
                with self._runners_lock:
                    self._runner_sse_clients.pop(runner_profile, None)
                with self._popup_lock:
                    self._popup_futures.pop(popup_id, None)
                return {"ok": False, "error": "runner_disconnected"}
        else:
            with self._popup_lock:
                self._popup_futures.pop(popup_id, None)
            return {"ok": False, "error": "runner_not_connected"}

        total_timeout = float(ui_spec.get("timeout_s", 30)) + 5.0
        fired = ev.wait(timeout=total_timeout)
        with self._popup_lock:
            self._popup_futures.pop(popup_id, None)
            result = self._popup_results.pop(popup_id, None)
        if not fired or result is None:
            return {"ok": False, "timed_out": True, "value": None}
        return {"ok": True, "value": result.get("value"), "popup_id": popup_id}
```

Add `_runner_sse_clients: dict[str, Any]` to `__init__`. In `_handle_runner_sse`, register and deregister:

```python
        def _handle_runner_sse(self, runner_profile: str):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            if runner_profile:
                with srv._runners_lock:
                    srv._runner_sse_clients[runner_profile] = self.wfile
            try:
                while True:
                    time.sleep(15)
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except OSError:
                pass
            finally:
                if runner_profile:
                    with srv._runners_lock:
                        srv._runner_sse_clients.pop(runner_profile, None)
                        srv._connected_runners.pop(runner_profile, None)
```

Add `self._runner_sse_clients: dict[str, Any] = {}` to `__init__`.

- [ ] **Step 4: Add `runner_notify` MCP tool to `emerge_daemon.py`**

Find the tool schema list in `handle_jsonrpc` (in the `tools/list` response). Add the new tool:

```python
{
    "name": "runner_notify",
    "title": "Notify operator via runner popup",
    "description": "Show a popup on the runner machine and wait for operator response. Returns {value, timed_out}.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "runner_profile": {"type": "string", "description": "Runner profile name (e.g. mycader-1)"},
            "ui_spec": {
                "type": "object",
                "description": "Popup spec: {type, title, body, options?, timeout_s?}",
            },
        },
        "required": ["runner_profile", "ui_spec"],
    },
    "annotations": {"readOnlyHint": False},
},
```

In `_call_tool` (or wherever tools are dispatched), add handler:

```python
        if name == "runner_notify":
            runner_profile = str(arguments.get("runner_profile", "")).strip()
            ui_spec = arguments.get("ui_spec", {})
            if not isinstance(ui_spec, dict):
                return {"isError": True, "content": [{"type": "text", "text": "ui_spec must be an object"}]}
            http_srv = getattr(self, "_http_server", None)
            if http_srv is None:
                return {"isError": True, "content": [{"type": "text", "text": "runner_notify requires HTTP daemon mode"}]}
            result = http_srv.request_popup(runner_profile, ui_spec)
            return {"content": [{"type": "text", "text": json.dumps(result)}]}
```

- [ ] **Step 5: Run tests**

```bash
python -m pytest tests/test_runner_push.py tests/test_daemon_http.py -q
```

Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add scripts/daemon_http.py scripts/emerge_daemon.py
git commit -m "feat: runner_notify MCP tool + popup correlation via SSE"
```

---

### Task 8: Remote runner dispatches SSE commands → `show_notify`

**Context:** When the runner receives an SSE `notify` command from the daemon, it must call `show_notify(ui_spec)` and POST the result back to `/runner/popup-result`. This runs in a background thread in `remote_runner.py`.

**Files:**
- Modify: `scripts/remote_runner.py`

- [ ] **Step 1: Write the failing test**

```python
# Append to tests/test_runner_push.py:

def test_runner_sse_dispatches_notify_and_posts_result(tmp_path):
    """RunnerExecutor SSE listener receives notify command and posts result."""
    srv = _make_server_with_files(tmp_path)

    runner_config = tmp_path / "runner-config.json"
    runner_config.write_text(json.dumps({
        "team_lead_url": f"http://localhost:{srv.port}",
        "runner_profile": "test-runner",
    }))

    popup_calls = []

    class _PatchedExecutor:
        """Minimal stub for show_notify."""
        _team_lead_url = f"http://localhost:{srv.port}"
        _runner_profile = "test-runner"

        def show_notify(self, ui_spec):
            popup_calls.append(ui_spec)
            return {"value": "接管"}

    # Simulate SSE push from daemon side (inject a fake notify command)
    popup_id = "test-abc"
    ev = threading.Event()
    with srv._popup_lock:
        srv._popup_futures[popup_id] = ev

    # Push command directly to runner SSE wfile (we need a connected runner)
    # Use the RunnerSSEClient helper
    from scripts.remote_runner import RunnerSSEClient
    client = RunnerSSEClient(
        team_lead_url=f"http://localhost:{srv.port}",
        runner_profile="test-runner",
        executor_show_notify=_PatchedExecutor().show_notify,
    )
    # Inject the notify command synchronously
    client._dispatch_command({"type": "notify", "popup_id": popup_id,
                               "ui_spec": {"type": "choice", "title": "Test"}})
    time.sleep(0.2)

    assert len(popup_calls) == 1
    assert popup_calls[0]["type"] == "choice"
    fired = ev.wait(timeout=1)
    assert fired
    srv.stop()
```

- [ ] **Step 2: Run to confirm it fails**

```bash
python -m pytest tests/test_runner_push.py::test_runner_sse_dispatches_notify_and_posts_result -q
```

Expected: `ImportError: cannot import name 'RunnerSSEClient'`

- [ ] **Step 3: Add `RunnerSSEClient` to `scripts/remote_runner.py`**

```python
class RunnerSSEClient:
    """Connects to daemon SSE channel, dispatches received commands.

    Runs in a background daemon thread. Auto-reconnects on disconnect.
    """

    def __init__(
        self,
        team_lead_url: str,
        runner_profile: str,
        executor_show_notify,
    ) -> None:
        self._url = team_lead_url.rstrip("/")
        self._runner_profile = runner_profile
        self._show_notify = executor_show_notify
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="RunnerSSEClient"
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        import urllib.request as _ur
        import urllib.error as _ue
        backoff = 1.0
        while not self._stop.is_set():
            try:
                url = (f"{self._url}/runner/sse"
                       f"?runner_profile={self._runner_profile}")
                req = _ur.Request(url, headers={"Accept": "text/event-stream"})
                with _ur.urlopen(req, timeout=None) as resp:
                    backoff = 1.0
                    buf = ""
                    while not self._stop.is_set():
                        chunk = resp.read(1)
                        if not chunk:
                            break
                        buf += chunk.decode("utf-8", errors="replace")
                        if "\n\n" in buf:
                            parts = buf.split("\n\n")
                            buf = parts[-1]
                            for part in parts[:-1]:
                                for line in part.splitlines():
                                    if line.startswith("data: "):
                                        try:
                                            cmd = json.loads(line[6:])
                                            threading.Thread(
                                                target=self._dispatch_command,
                                                args=(cmd,), daemon=True
                                            ).start()
                                        except json.JSONDecodeError:
                                            pass
            except (_ue.URLError, OSError):
                if not self._stop.is_set():
                    self._stop.wait(timeout=min(backoff, 30))
                    backoff = min(backoff * 2, 30)

    def _dispatch_command(self, cmd: dict) -> None:
        cmd_type = cmd.get("type")
        if cmd_type == "notify":
            popup_id = cmd.get("popup_id", "")
            ui_spec = cmd.get("ui_spec", {})
            try:
                result = self._show_notify(ui_spec)
            except Exception:
                result = {"value": None}
            # POST result back to daemon
            self._post_result(popup_id, result)

    def _post_result(self, popup_id: str, result: dict) -> None:
        import urllib.request as _ur
        import urllib.error as _ue
        payload = {"popup_id": popup_id, "value": result.get("value")}
        body = json.dumps(payload).encode()
        req = _ur.Request(
            f"{self._url}/runner/popup-result",
            data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with _ur.urlopen(req, timeout=5):
                pass
        except (_ue.URLError, OSError):
            pass
```

- [ ] **Step 4: Wire `RunnerSSEClient` into `RunnerHTTPHandler` startup**

In `remote_runner.py`, at the bottom in the `__main__` block (or wherever the server starts), after creating `RunnerExecutor`, launch the SSE client:

```python
def _start_sse_client(executor: RunnerExecutor) -> None:
    if executor._team_lead_url and executor._runner_profile:
        client = RunnerSSEClient(
            team_lead_url=executor._team_lead_url,
            runner_profile=executor._runner_profile,
            executor_show_notify=executor.show_notify,
        )
        client.start()
        # Also POST runner/online to register
        import urllib.request as _ur
        import urllib.error as _ue
        try:
            import socket as _sock
            machine_id = _sock.gethostname()
            body = json.dumps({
                "runner_profile": executor._runner_profile,
                "machine_id": machine_id,
            }).encode()
            req = _ur.Request(
                f"{executor._team_lead_url}/runner/online",
                data=body, headers={"Content-Type": "application/json"}
            )
            with _ur.urlopen(req, timeout=5):
                pass
        except (_ue.URLError, OSError):
            pass
```

Call `_start_sse_client(executor)` at the end of server startup.

- [ ] **Step 5: Run all tests**

```bash
python -m pytest tests/test_runner_push.py -q
```

Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add scripts/remote_runner.py
git commit -m "feat: RunnerSSEClient dispatches daemon commands to show_notify, posts results"
```

---

## Phase D — Unified Event Streams

### Task 9: `watch_emerge.py` — unified tail watcher

**Context:** Replace `watch_patterns.py` and `watch_pending.py` with a single `watch_emerge.py` that tails `events.jsonl`, `events-{profile}.jsonl`, or `events-local.jsonl` depending on mode. Uses `follow=True` tail (reads new lines appended since last position).

**Files:**
- Create: `scripts/watch_emerge.py`
- Modify: `scripts/pending_actions.py` (add new formatters)
- Create: `tests/test_watch_emerge.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_watch_emerge.py
from __future__ import annotations
import json, subprocess, sys, time
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _write_event(path: Path, event: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


def test_watch_emerge_global_prints_runner_discovered(tmp_path):
    """watch_emerge.py tails events.jsonl and prints runner_discovered events."""
    events_file = tmp_path / "events.jsonl"

    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "watch_emerge.py"),
         "--state-root", str(tmp_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    time.sleep(0.3)

    _write_event(events_file, {
        "type": "runner_discovered",
        "ts_ms": 1000,
        "runner_profile": "mycader-1",
        "machine_id": "wkst-A",
    })
    time.sleep(0.5)
    proc.terminate()
    out = proc.stdout.read().decode()
    assert "runner_discovered" in out or "mycader-1" in out


def test_watch_emerge_runner_mode_tails_profile_file(tmp_path):
    events_file = tmp_path / "events-mycader-1.jsonl"

    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "watch_emerge.py"),
         "--runner-profile", "mycader-1",
         "--state-root", str(tmp_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    time.sleep(0.3)

    _write_event(events_file, {
        "type": "pattern_alert",
        "ts_ms": 1000,
        "runner_profile": "mycader-1",
        "stage": "canary",
        "intent_signature": "hypermesh.mesh.batch",
    })
    time.sleep(0.5)
    proc.terminate()
    out = proc.stdout.read().decode()
    assert "canary" in out or "hypermesh" in out


def test_watch_emerge_exits_on_sigterm(tmp_path):
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "watch_emerge.py"),
         "--state-root", str(tmp_path)],
        stdout=subprocess.PIPE,
    )
    time.sleep(0.2)
    proc.terminate()
    proc.wait(timeout=3)
    assert proc.returncode is not None
```

- [ ] **Step 2: Run to confirm they fail**

```bash
python -m pytest tests/test_watch_emerge.py -q 2>&1 | head -10
```

Expected: `FileNotFoundError` or similar (script doesn't exist yet)

- [ ] **Step 3: Add new formatters to `scripts/pending_actions.py`**

```python
def format_runner_discovered(data: dict) -> str:
    profile = data.get("runner_profile", "?")
    machine = data.get("machine_id", "?")
    ts = data.get("ts_ms", 0)
    return f"[RunnerDiscovered] runner={profile} machine={machine} ts={ts}"


def format_runner_online(data: dict) -> str:
    profile = data.get("runner_profile", "?")
    return f"[RunnerOnline] runner={profile} is ready"


def format_runner_event(data: dict) -> str:
    profile = data.get("runner_profile", "?")
    etype = data.get("type", "?")
    ts = data.get("ts_ms", 0)
    return f"[RunnerEvent] runner={profile} type={etype} ts={ts}"
```

- [ ] **Step 4: Create `scripts/watch_emerge.py`**

```python
#!/usr/bin/env python3
"""Unified emerge event stream watcher.

Tails events.jsonl (global), events-{profile}.jsonl (per-runner), or
events-local.jsonl (local) and prints formatted lines to stdout.

Launch via CC's Monitor tool:
    Monitor(command="python3 .../watch_emerge.py", persistent=true)
    Monitor(command="python3 .../watch_emerge.py --runner-profile mycader-1", persistent=true)
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.pending_actions import (  # noqa: E402
    format_pending_actions,
    format_pattern_alert,
    format_runner_discovered,
    format_runner_online,
    format_runner_event,
)

_stop = False


def _on_signal(signum, frame) -> None:
    global _stop
    _stop = True


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


def _format_event(event: dict) -> str | None:
    etype = event.get("type", "")
    if etype == "runner_discovered":
        return format_runner_discovered(event)
    if etype == "runner_online":
        return format_runner_online(event)
    if etype == "runner_event":
        return format_runner_event(event)
    if etype in ("pattern_alert", "local_pattern_alert"):
        return format_pattern_alert(event)
    if etype == "cockpit_action":
        actions = event.get("actions", [])
        if actions:
            return format_pending_actions(actions)
        return None
    # Unknown type: print raw
    return f"[Event] {json.dumps(event)}"


def _state_root(override: str | None = None) -> Path:
    if override:
        return Path(override)
    env = os.environ.get("EMERGE_STATE_ROOT") or os.environ.get("CLAUDE_PLUGIN_DATA")
    if env:
        return Path(env)
    return Path.home() / ".emerge" / "repl"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Watch emerge event stream.")
    p.add_argument("--runner-profile", default="",
                   help="Runner profile to watch (watches events-{profile}.jsonl)")
    p.add_argument("--local", action="store_true",
                   help="Watch local events (events-local.jsonl)")
    p.add_argument("--state-root", default="",
                   help="Override state root directory")
    return p.parse_args()


def run_tail(path: Path, sleep_s: float = 0.5) -> None:
    """Tail-follow path, print formatted events."""
    path.parent.mkdir(parents=True, exist_ok=True)
    offset = 0
    # Start from end of file if it already exists
    if path.exists():
        offset = path.stat().st_size

    while not _stop:
        try:
            if not path.exists():
                time.sleep(sleep_s)
                continue
            current_size = path.stat().st_size
            if current_size < offset:
                offset = 0  # file truncated/rotated
            if current_size > offset:
                with path.open("r", encoding="utf-8") as f:
                    f.seek(offset)
                    new_data = f.read()
                offset = current_size
                for line in new_data.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    formatted = _format_event(event)
                    if formatted is not None:
                        print(formatted, flush=True)
        except OSError:
            pass
        time.sleep(sleep_s)


if __name__ == "__main__":
    args = _parse_args()
    root = _state_root(args.state_root)
    if args.local:
        target = root / "events-local.jsonl"
    elif args.runner_profile.strip():
        profile = args.runner_profile.strip()
        target = root / f"events-{profile}.jsonl"
    else:
        target = root / "events.jsonl"
    run_tail(target)
```

- [ ] **Step 5: Run tests**

```bash
python -m pytest tests/test_watch_emerge.py -q
```

Expected: `3 passed`

- [ ] **Step 6: Commit**

```bash
git add scripts/watch_emerge.py scripts/pending_actions.py tests/test_watch_emerge.py
git commit -m "feat: watch_emerge.py unified event stream watcher (3 modes)"
```

---

### Task 10: Shim old watch scripts; daemon writes cockpit_action to events.jsonl

**Context:** `watch_patterns.py` and `watch_pending.py` should remain functional (backward compat) but delegate to `watch_emerge.py`. Also, when cockpit submits actions (currently written to `pending-actions.json`), the daemon's EventRouter handler should also append a `cockpit_action` event to `events.jsonl` so the global Monitor catches it.

**Files:**
- Modify: `scripts/watch_patterns.py`
- Modify: `scripts/watch_pending.py`
- Modify: `scripts/emerge_daemon.py` (`_on_pending_actions` appends to events.jsonl)

- [ ] **Step 1: Write the failing test**

```python
# Append to tests/test_watch_emerge.py:

def test_watch_patterns_shim_delegates_to_watch_emerge(tmp_path):
    """watch_patterns.py --runner-profile delegates to watch_emerge.py."""
    events_file = tmp_path / "events-mycader-1.jsonl"

    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "watch_patterns.py"),
         "--runner-profile", "mycader-1",
         "--state-root", str(tmp_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    time.sleep(0.3)
    _write_event(events_file, {
        "type": "pattern_alert",
        "ts_ms": 1000,
        "stage": "canary",
        "intent_signature": "hypermesh.mesh.batch",
        "meta": {"occurrences": 5, "window_minutes": 10, "machine_ids": ["wkst"]},
    })
    time.sleep(0.5)
    proc.terminate()
    out = proc.stdout.read().decode()
    assert "canary" in out or "hypermesh" in out
```

- [ ] **Step 2: Run to confirm it fails**

```bash
python -m pytest tests/test_watch_emerge.py::test_watch_patterns_shim_delegates_to_watch_emerge -q
```

Expected: FAIL (watch_patterns.py still uses old file-based watcher, not events.jsonl)

- [ ] **Step 3: Update `scripts/watch_patterns.py` to be a shim**

Replace the entire `if __name__ == "__main__":` block:

```python
if __name__ == "__main__":
    args = _parse_args()
    profile = args.runner_profile.strip()
    # Shim: delegate to watch_emerge.py
    import importlib.util as _iu
    _emerge = ROOT / "scripts" / "watch_emerge.py"
    _spec = _iu.spec_from_file_location("watch_emerge", _emerge)
    _mod = _iu.module_from_spec(_spec)  # type: ignore[arg-type]
    _spec.loader.exec_module(_mod)  # type: ignore[union-attr]
    import sys as _sys
    _new_args = ["watch_emerge"]
    if profile:
        _new_args += ["--runner-profile", profile]
    if hasattr(args, "state_root") and args.state_root:
        _new_args += ["--state-root", args.state_root]
    _sys.argv = _new_args
    _mod._parse_args = lambda: _mod._parse_args.__wrapped__() if hasattr(_mod._parse_args, "__wrapped__") else _mod._parse_args()
    # Re-parse and run
    _args2 = _mod._parse_args()
    _root = _mod._state_root(_args2.state_root)
    if profile:
        _target = _root / f"events-{profile}.jsonl"
    else:
        _target = _root / "events.jsonl"
    _mod.run_tail(_target)
```

Actually, simpler shim:

```python
if __name__ == "__main__":
    args = _parse_args()
    profile = args.runner_profile.strip()
    # Shim: delegate to watch_emerge.py for unified event stream
    import subprocess as _sp, sys as _sys
    _emerge = str(ROOT / "scripts" / "watch_emerge.py")
    cmd = [_sys.executable, _emerge]
    if profile:
        cmd += ["--runner-profile", profile]
    # Pass through any --state-root if present (added for testing)
    if hasattr(args, "state_root") and getattr(args, "state_root", ""):
        cmd += ["--state-root", args.state_root]
    _sp.execv(_sys.executable, cmd)
```

Add `--state-root` to `_parse_args()` in `watch_patterns.py`:

```python
def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Watch emerge pattern alerts for one runner.")
    p.add_argument("--runner-profile", default="", ...)
    p.add_argument("--state-root", default="", help="Override state root (for testing)")
    return p.parse_args()
```

- [ ] **Step 4: Update `scripts/watch_pending.py` similarly**

```python
if __name__ == "__main__":
    import subprocess as _sp, sys as _sys
    _emerge = str(ROOT / "scripts" / "watch_emerge.py")
    _sp.execv(_sys.executable, [_sys.executable, _emerge])
```

- [ ] **Step 5: Run tests**

```bash
python -m pytest tests/test_watch_emerge.py -q
```

Expected: all 4 pass

- [ ] **Step 6: Commit**

```bash
git add scripts/watch_patterns.py scripts/watch_pending.py scripts/emerge_daemon.py
git commit -m "feat: watch_patterns/watch_pending shims → watch_emerge.py; cockpit_action events"
```

---

## Phase E — Cockpit Auto-start + Monitors Tab

### Task 11: Daemon auto-starts cockpit

**Context:** When `run_http()` starts, it should also spawn `repl_admin.py serve --port 0` as a subprocess and write the cockpit URL to `~/.emerge/cockpit-url.txt`. Simplify `commands/cockpit.md`.

**Files:**
- Modify: `scripts/emerge_daemon.py` (add `_ensure_cockpit()` in `run_http`)
- Modify: `commands/cockpit.md`

- [ ] **Step 1: Add `_ensure_cockpit` to `emerge_daemon.py`**

```python
def _ensure_cockpit(plugin_root: Path) -> str | None:
    """Start cockpit server if not already running. Returns URL or None."""
    import subprocess as _sub, sys as _sys
    pid_path = Path.home() / ".emerge" / "cockpit.pid"
    if pid_path.exists():
        try:
            import json as _j
            info = _j.loads(pid_path.read_text())
            os.kill(int(info["pid"]), 0)
            return f"http://localhost:{info['port']}"
        except (OSError, KeyError, ValueError):
            pid_path.unlink(missing_ok=True)
    # Start cockpit
    repl_admin = plugin_root / "scripts" / "repl_admin.py"
    proc = _sub.Popen(
        [_sys.executable, str(repl_admin), "serve", "--port", "0"],
        stdout=_sub.PIPE, stderr=_sub.DEVNULL,
        start_new_session=True,
    )
    # Read URL from first line of stdout
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            text = line.decode("utf-8", errors="replace").strip()
            if "http://localhost:" in text:
                url = text.split("http://localhost:")[-1]
                url = f"http://localhost:{url.split()[0]}"
                url_path = Path.home() / ".emerge" / "cockpit-url.txt"
                url_path.write_text(url, encoding="utf-8")
                return url
    except Exception:
        pass
    return None
```

In `run_http()`, call `_ensure_cockpit(daemon._root)` and print the URL.

- [ ] **Step 2: Simplify `commands/cockpit.md`**

Replace the entire content with:

```markdown
# /emerge:cockpit — Open Cockpit Dashboard

The cockpit server is started automatically by the daemon. This command opens it.

## Steps

1. **Get cockpit URL**:
   ```bash
   cat ~/.emerge/cockpit-url.txt
   ```
   If missing: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" serve --port 0`

2. **Print URL to user**:
   Report: "Cockpit running at <URL>"

3. **Start global Monitor** (team lead CC session):
   ```
   Monitor(command="python3 ${CLAUDE_PLUGIN_ROOT}/scripts/watch_emerge.py",
           description="emerge event stream — global",
           persistent=true)
   ```

4. **Start per-runner Monitors** for each connected runner profile:
   ```
   Monitor(command="python3 ${CLAUDE_PLUGIN_ROOT}/scripts/watch_emerge.py --runner-profile {profile}",
           description="emerge event stream — {profile}",
           persistent=true)
   ```
   Only start if runners are configured:
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" runner-status --pretty`

5. **Close the cockpit**: when operator says close/exit:
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" serve-stop`
```

- [ ] **Step 3: Run tests**

```bash
python -m pytest tests -q 2>&1 | tail -5
```

Expected: all pass

- [ ] **Step 4: Commit**

```bash
git add scripts/emerge_daemon.py commands/cockpit.md
git commit -m "feat: daemon auto-starts cockpit on run_http(); cockpit.md simplified"
```

---

### Task 12: `/api/control-plane/monitors` endpoint

**Context:** `repl_admin.py` needs a new endpoint that returns the current connected-runners state. The data lives in `daemon_http.DaemonHTTPServer._connected_runners`, but cockpit is a separate process. We bridge via a state file `~/.emerge/runner-monitor-state.json` written by the daemon on connection changes.

**Files:**
- Modify: `scripts/daemon_http.py` (write `runner-monitor-state.json` on connect/disconnect)
- Modify: `scripts/repl_admin.py` (add `GET /api/control-plane/monitors`)
- Create: `tests/test_cockpit_monitors.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_cockpit_monitors.py
from __future__ import annotations
import json, time, urllib.request
import pytest
from pathlib import Path


def _get(port, path):
    with urllib.request.urlopen(f"http://localhost:{port}{path}", timeout=5) as r:
        return json.loads(r.read())


def test_monitors_endpoint_returns_empty_when_no_state(tmp_path):
    """GET /api/control-plane/monitors returns empty runners list when no state file."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.repl_admin import cmd_control_plane_monitors
    result = cmd_control_plane_monitors(state_root=tmp_path)
    assert result["runners"] == []


def test_monitors_endpoint_reads_state_file(tmp_path):
    state = {
        "runners": [
            {"runner_profile": "mycader-1", "connected": True,
             "connected_at_ms": 1000, "last_event_ts_ms": 2000,
             "machine_id": "wkst-A", "last_alert": None}
        ]
    }
    (tmp_path / "runner-monitor-state.json").write_text(json.dumps(state))
    from scripts.repl_admin import cmd_control_plane_monitors
    result = cmd_control_plane_monitors(state_root=tmp_path)
    assert len(result["runners"]) == 1
    assert result["runners"][0]["runner_profile"] == "mycader-1"
```

- [ ] **Step 2: Run to confirm they fail**

```bash
python -m pytest tests/test_cockpit_monitors.py -q 2>&1 | head -10
```

Expected: `ImportError: cannot import name 'cmd_control_plane_monitors'`

- [ ] **Step 3: Add `cmd_control_plane_monitors` to `repl_admin.py`**

```python
def cmd_control_plane_monitors(state_root: "Path | None" = None) -> dict:
    """Return connected runner monitor state from runner-monitor-state.json."""
    from scripts.policy_config import default_exec_root
    root = state_root or Path(default_exec_root()).expanduser()
    state_path = root / "runner-monitor-state.json"
    if not state_path.exists():
        return {"runners": [], "team_active": False}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        return {
            "runners": data.get("runners", []),
            "team_active": bool(data.get("team_active", False)),
        }
    except (OSError, json.JSONDecodeError):
        return {"runners": [], "team_active": False}
```

Add to the GET handler in `_CockpitHandler.do_GET`:

```python
        elif path == "/api/control-plane/monitors":
            self._json(cmd_control_plane_monitors())
```

- [ ] **Step 4: Add `_write_monitor_state` to `DaemonHTTPServer`**

```python
    def _write_monitor_state(self) -> None:
        """Write current runner state to runner-monitor-state.json for cockpit."""
        import json as _j, time as _t
        with self._runners_lock:
            runners = [
                {
                    "runner_profile": profile,
                    "connected": True,
                    "connected_at_ms": info.get("connected_at_ms", 0),
                    "last_event_ts_ms": info.get("last_event_ts_ms", 0),
                    "machine_id": info.get("machine_id", ""),
                    "last_alert": info.get("last_alert"),
                }
                for profile, info in self._connected_runners.items()
            ]
        state = {"runners": runners, "team_active": False,
                 "updated_ts_ms": int(_t.time() * 1000)}
        path = self._state_root / "runner-monitor-state.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            import tempfile as _tf, os as _os
            with _tf.NamedTemporaryFile("w", dir=path.parent, delete=False,
                                        suffix=".tmp", encoding="utf-8") as tf:
                _j.dump(state, tf)
                tf_path = tf.name
            _os.replace(tf_path, path)
        except OSError:
            pass
```

Call `_write_monitor_state()` from `_on_runner_online` and from `_handle_runner_sse`'s `finally` block.

- [ ] **Step 5: Run tests**

```bash
python -m pytest tests/test_cockpit_monitors.py -q
```

Expected: `2 passed`

- [ ] **Step 6: Commit**

```bash
git add scripts/daemon_http.py scripts/repl_admin.py tests/test_cockpit_monitors.py
git commit -m "feat: /api/control-plane/monitors endpoint + runner-monitor-state.json"
```

---

### Task 13: Cockpit Monitors tab + CLAUDE.md / README.md updates

**Context:** Add Monitors tab to `cockpit_shell.html`. Update `CLAUDE.md` and `README.md` for all architecture changes.

**Files:**
- Modify: `scripts/cockpit_shell.html`
- Modify: `CLAUDE.md`
- Modify: `README.md`
- Modify: `commands/monitor.md`

- [ ] **Step 1: Add Monitors tab to `cockpit_shell.html`**

In `renderTabs()` (around line 1128), in the global tabs block, add Monitors tab before Audit:

```javascript
  html += `<div class="tab ${currentTab === 'monitors' ? 'active' : ''}" onclick="switchTab('monitors')" style="font-size:11px;opacity:0.7">Monitors</div>`;
```

In `renderCurrentTab()`, add a case for `monitors`:

```javascript
  if (currentTab === 'monitors') {
    renderMonitorsTab();
    return;
  }
```

Add `renderMonitorsTab` function:

```javascript
async function renderMonitorsTab() {
  const main = document.getElementById('main-content');
  main.innerHTML = '<div style="padding:16px;color:#8b949e">Loading monitor state…</div>';
  try {
    const resp = await fetch('/api/control-plane/monitors');
    const data = await resp.json();
    const runners = data.runners || [];
    if (!runners.length) {
      main.innerHTML = '<div style="padding:24px;color:#8b949e;text-align:center">No runners connected.<br>Use <code>/emerge:monitor</code> to start agents-team mode.</div>';
      return;
    }
    let html = '<div style="padding:16px">';
    html += `<div style="margin-bottom:12px;font-size:12px;color:#8b949e">${runners.length} runner(s) connected</div>`;
    html += '<table style="width:100%;border-collapse:collapse;font-size:12px">';
    html += '<thead><tr>';
    for (const h of ['Runner', 'Machine', 'Connected', 'Last Event', 'Last Alert']) {
      html += `<th style="text-align:left;padding:6px 10px;background:#161b22;border-bottom:1px solid #30363d;color:#8b949e;font-size:10px;text-transform:uppercase">${h}</th>`;
    }
    html += '</tr></thead><tbody>';
    const now = Date.now();
    for (const r of runners) {
      const connectedSec = r.connected_at_ms ? Math.round((now - r.connected_at_ms) / 1000) : 0;
      const lastEventSec = r.last_event_ts_ms ? Math.round((now - r.last_event_ts_ms) / 1000) : null;
      const alertBadge = r.last_alert
        ? `<span style="background:#0d2000;color:#3fb950;border:1px solid #2a4a2a;padding:1px 6px;border-radius:3px;font-size:10px">${escHtml(r.last_alert.stage)}: ${escHtml(r.last_alert.intent_signature || '')}</span>`
        : '<span style="color:#8b949e">none</span>';
      html += `<tr>
        <td style="padding:6px 10px;border-bottom:1px solid #21262d">
          <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${r.connected ? '#3fb950' : '#6e7681'};margin-right:6px"></span>
          ${escHtml(r.runner_profile)}
        </td>
        <td style="padding:6px 10px;border-bottom:1px solid #21262d">${escHtml(r.machine_id || '')}</td>
        <td style="padding:6px 10px;border-bottom:1px solid #21262d">${connectedSec}s ago</td>
        <td style="padding:6px 10px;border-bottom:1px solid #21262d">${lastEventSec !== null ? lastEventSec + 's ago' : '—'}</td>
        <td style="padding:6px 10px;border-bottom:1px solid #21262d">${alertBadge}</td>
      </tr>`;
    }
    html += '</tbody></table></div>';
    main.innerHTML = html;
  } catch (e) {
    main.innerHTML = `<div style="padding:16px;color:#f85149">Error loading monitors: ${escHtml(String(e))}</div>`;
  }
}
```

Add SSE listener for `monitors_updated` to auto-refresh:

```javascript
// In the SSE event handler (where 'pending' events are handled):
if (evt.monitors_updated) {
  if (currentTab === 'monitors') renderMonitorsTab();
}
```

In `repl_admin.py`, in `_on_runner_online` flow, after writing `runner-monitor-state.json`:

```python
_sse_broadcast({"monitors_updated": True, "ts_ms": int(time.time() * 1000)})
```

Trigger this broadcast from cockpit when `runner-monitor-state.json` changes. Simplest: daemon writes the file, then the cockpit state endpoint returns fresh data on SSE refresh.

- [ ] **Step 2: Update `commands/monitor.md` — use `runner_notify` instead of direct HTTP**

Find the popup lines in the watcher prompt and replace:

```
     call POST /notify on the runner:
         ui_spec: {"type": "choice", ...}
```

With:

```
     call MCP tool runner_notify(runner_profile="{profile}", ui_spec={...})
```

The full canary section becomes:

```
     - stage=canary   → runner_notify(
           runner_profile="{profile}",
           ui_spec={"type": "choice", "title": "emerge — 可以接管了",
                    "body": "[{intent_signature}] 已见 {occurrences} 次，接管？",
                    "options": ["接管", "跳过", "停止学习"],
                    "timeout_s": 15})
         接管 → icc_exec(intent_signature=<value from alert JSON field "intent_signature">)
         停止学习 → repl_admin pipeline-set --pipeline-key <key> --set frozen=true
```

AI uncertainty:

```
   AI uncertainty or knowledge distillation → runner_notify(
       runner_profile="{profile}",
       ui_spec={"type": "input", "title": "emerge — 需要确认", "body": "<question>"})
```

- [ ] **Step 3: Update `CLAUDE.md`**

Find the **Architecture** section and update the following bullets:

```markdown
**Single control plane HTTP mode**: `EmergeDaemon` now runs as a persistent HTTP MCP server
(`scripts/daemon_http.py` `DaemonHTTPServer`, port 8789, PID file `~/.emerge/daemon.pid`).
CC sessions connect via `plugin.json` `url: "http://localhost:8789/mcp"`. `SessionStart`
hook launches daemon via `--ensure-running` if not already running. All CC sessions
(team lead + watcher subagents) share one daemon instance.
```

Update **Key Invariants** with:

```markdown
- **Daemon HTTP persistence**: daemon runs as single persistent process. `plugin.json`
  uses `url:` not `command:`. `--ensure-running` flag: checks PID file, exits if already
  running. Per-session SSE channels at `GET /mcp` enable `_elicit()` in HTTP mode via
  thread-local session routing (`daemon_http._tl.session_id`).
- **Runner push architecture**: runners connect `GET /runner/sse?runner_profile=<p>`,
  report online via `POST /runner/online`, forward events via `POST /runner/event`.
  `OperatorMonitor` HTTP poll loop removed — events arrive via HTTP push. Popup commands
  sent via SSE; results returned via `POST /runner/popup-result` with correlation ID.
- **Unified event streams**: `~/.emerge/repl/events.jsonl` (global), 
  `events-{profile}.jsonl` (per-runner), `events-local.jsonl` (local). Single
  `watch_emerge.py` script handles all three modes. Old watch scripts are shims.
- **Monitors tab**: cockpit reads `~/.emerge/repl/runner-monitor-state.json` (written by
  daemon on runner connect/disconnect) via `GET /api/control-plane/monitors`. SSE
  `monitors_updated` triggers auto-refresh.
```

- [ ] **Step 4: Update `README.md`** — component table row for daemon, new Monitors tab row

Find the component table and update/add:

| Component | Update |
|---|---|
| `emerge_daemon.py` | Add: "HTTP MCP server mode (port 8789), runner SSE hub, `runner_notify` tool" |
| `daemon_http.py` | New row: "DaemonHTTPServer — HTTP transport, runner connections, popup correlation" |
| `watch_emerge.py` | New row: "Unified event stream watcher (replaces watch_patterns + watch_pending shims)" |
| Cockpit tabs | Add Monitors tab to tab list |

- [ ] **Step 5: Bump version in `plugin.json` to `0.3.67`**

```bash
# Already done in Task 2 above — verify
grep version .claude-plugin/plugin.json
```

Expected: `"version": "0.3.67"`

- [ ] **Step 6: Run full test suite**

```bash
python -m pytest tests -q 2>&1 | tail -10
```

Expected: all tests pass

- [ ] **Step 7: Final commit**

```bash
git add scripts/cockpit_shell.html commands/monitor.md CLAUDE.md README.md .claude-plugin/plugin.json
git commit -m "feat: cockpit Monitors tab, CLAUDE.md/README.md architecture updates, version 0.3.67"
```

---

## Self-Review

**Spec coverage check:**

| Spec section | Task |
|---|---|
| 1.2 DaemonHTTPServer | Task 1 |
| 1.3 PID file singleton | Task 2 |
| 1.4 plugin.json `url:` | Task 2 |
| 1.5 SessionStart ensure-running | Task 2 |
| HTTP mode `_write_mcp_push` | Task 3 |
| 2.1 Runner startup SSE + /runner/online | Task 4, 5 |
| 2.2 Runner event forwarding | Task 5 |
| 2.3 Popup correlation | Task 7, 8 |
| 2.4 SSE command format | Task 8 |
| 2.5 runner-bootstrap --team-lead-url | Task 6 |
| 2.6 _connected_runners tracking | Task 4 |
| 3.1-3.4 Unified event streams | Task 9, 10 |
| 4.1 Cockpit auto-start | Task 11 |
| 4.2 Controls injection | ⚠️ Not in this plan — daemon controls injection requires reading all connectors + generating HTML; deferred to separate plan to keep scope |
| 4.3 /emerge:cockpit simplified | Task 11 |
| 5.1 /api/control-plane/monitors | Task 12 |
| 5.2 Monitors tab | Task 13 |

**Note on deferred item:** Cockpit auto controls injection (spec §4.2) is omitted here because it requires LLM-driven HTML generation logic (vertical-specific) that is better done as a separate thin plan once the rest is working.

**Type consistency:** `runner_profile` string, `machine_id` string, `popup_id` hex string, `_connected_runners: dict[str, dict]` — consistent across tasks 4, 7, 12, 13. `runner_notify` tool uses `runner_profile` + `ui_spec` — consistent with Task 7 `request_popup` signature.
