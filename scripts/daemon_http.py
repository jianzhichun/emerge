from __future__ import annotations

import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

_KEEPALIVE_INTERVAL_S = 20.0

_tl = threading.local()  # _tl.session_id: str | None — set per HTTP request


def get_current_session_id() -> str | None:
    """Return the session_id of the currently-executing HTTP request thread."""
    return getattr(_tl, "session_id", None)


class DaemonHTTPServer:
    """HTTP MCP transport for EmergeDaemon (Streamable HTTP).

    POST /mcp[?session_id=<id>]  — JSON-RPC request/response
    GET  /mcp                    — SSE channel registration (returns session_id)
    /runner/*                    — Runner endpoints (Phase B)
    """

    def __init__(
        self,
        daemon: Any,
        port: int = 8789,
        pid_path: Path | None = None,
        event_root: Path | None = None,
        state_root: Path | None = None,
    ) -> None:
        self._daemon = daemon
        self._port = port
        self._pid_path = pid_path or (Path.home() / ".emerge" / "daemon.pid")
        self._event_root = event_root or (Path.home() / ".emerge" / "operator-events")
        self._state_root = state_root or (Path.home() / ".emerge" / "repl")
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        # session_id → wfile (reserved for future use; CC HTTP client doesn't maintain SSE)
        self._sse_sessions: dict[str, Any] = {}
        self._sse_lock = threading.Lock()
        # Connected runners: runner_profile → {connected_at_ms, last_event_ts_ms, machine_id, last_alert}
        self._connected_runners: dict[str, dict] = {}
        self._runners_lock = threading.Lock()
        # runner_profile → wfile for SSE command push
        self._runner_sse_clients: dict[str, Any] = {}
        # popup_id → threading.Event
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
        """Push a JSON-RPC notification to a connected CC session via SSE.

        NOTE: CC's HTTP MCP client does not maintain a persistent GET SSE channel,
        so pushes sent here will not be received. Reserved for future compatibility.
        """
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
        req_id = req.get("id")
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

    def _on_runner_online(self, runner_profile: str, machine_id: str) -> None:
        import re as _re
        if not _re.fullmatch(r"[a-zA-Z0-9_.-]+", runner_profile) or len(runner_profile) > 64:
            raise ValueError(f"invalid runner_profile: {runner_profile!r}")
        now_ms = int(time.time() * 1000)
        with self._runners_lock:
            self._connected_runners[runner_profile] = {
                "connected_at_ms": now_ms,
                "last_event_ts_ms": 0,
                "machine_id": machine_id,
                "last_alert": None,
            }
        self._append_event(self._state_root / "events.jsonl", {
            "type": "runner_discovered",
            "ts_ms": now_ms,
            "runner_profile": runner_profile,
            "machine_id": machine_id,
        })
        self._append_event(self._state_root / f"events-{runner_profile}.jsonl", {
            "type": "runner_online",
            "ts_ms": now_ms,
            "runner_profile": runner_profile,
            "machine_id": machine_id,
        })
        self._write_monitor_state()

    def _on_runner_event(self, payload: dict) -> None:
        runner_profile = str(payload.get("runner_profile", "")).strip()
        machine_id = str(payload.get("machine_id", "")).strip()
        ts_ms = int(time.time() * 1000)
        if machine_id:
            machine_dir = self._event_root / machine_id
            machine_dir.mkdir(parents=True, exist_ok=True)
            with (machine_dir / "events.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        if runner_profile:
            import re as _re2
            if not _re2.fullmatch(r"[a-zA-Z0-9_.-]+", runner_profile) or len(runner_profile) > 64:
                runner_profile = ""  # invalid profile, skip per-runner event file
        if runner_profile:
            with self._runners_lock:
                if runner_profile in self._connected_runners:
                    self._connected_runners[runner_profile]["last_event_ts_ms"] = ts_ms
            self._write_monitor_state()
            self._append_event(self._state_root / f"events-{runner_profile}.jsonl", {
                "type": "runner_event",
                "ts_ms": ts_ms,
                "runner_profile": runner_profile,
                **{k: v for k, v in payload.items()
                   if k not in ("runner_profile", "type")},
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

    def request_popup(self, runner_profile: str, ui_spec: dict, timeout_s: float = 30.0) -> dict:
        """Send popup to runner via SSE, wait for result. Blocks calling thread."""
        popup_id = uuid.uuid4().hex
        ev = threading.Event()
        with self._popup_lock:
            self._popup_futures[popup_id] = ev
        command = json.dumps({"type": "notify", "popup_id": popup_id, "ui_spec": ui_spec})
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

    def _append_event(self, path: Path, event: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _write_monitor_state(self) -> None:
        """Write current runner state to runner-monitor-state.json for cockpit."""
        import tempfile as _tf
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
                 "updated_ts_ms": int(time.time() * 1000)}
        path = self._state_root / "runner-monitor-state.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with _tf.NamedTemporaryFile("w", dir=path.parent, delete=False,
                                        suffix=".tmp", encoding="utf-8") as tf:
                json.dump(state, tf)
                tf_path = tf.name
            os.replace(tf_path, path)
        except OSError:
            pass


def ensure_running_or_launch(
    pid_path: Path | None = None,
    port: int = 8789,
    daemon_factory: Any = None,
) -> str:
    """Check if daemon is running via PID file.
    Returns 'already_running', 'launched', or 'not_running'.
    """
    pid_path = pid_path or (Path.home() / ".emerge" / "daemon.pid")
    if pid_path.exists():
        try:
            info = json.loads(pid_path.read_text(encoding="utf-8"))
            pid = int(info["pid"])
            os.kill(pid, 0)
            return "already_running"
        except (ProcessLookupError, PermissionError, KeyError, ValueError, json.JSONDecodeError):
            pid_path.unlink(missing_ok=True)
    if daemon_factory is None:
        return "not_running"
    daemon_obj = daemon_factory()
    srv = DaemonHTTPServer(daemon=daemon_obj, port=port, pid_path=pid_path)
    srv.start()
    return "launched"


def _make_handler(srv: "DaemonHTTPServer"):
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass

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
            elif path == "/runner/sse":
                import urllib.parse as _up2
                qs2 = _up2.parse_qs(_up2.urlparse(self.path).query)
                profile = qs2.get("runner_profile", [""])[0].strip()
                self._handle_runner_sse(profile)
            else:
                self._send_json(404, {"ok": False, "error": "not_found"})

        def _handle_sse_mcp(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            session_id = srv.handle_get_mcp_sse(self.wfile)
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

        def _handle_runner_sse(self, runner_profile: str):
            import re as _re_sse
            if runner_profile and not _re_sse.fullmatch(r"[a-zA-Z0-9_.-]+", runner_profile):
                self._send_json(400, {"error": "invalid runner_profile"})
                return
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
                    srv._write_monitor_state()

        def do_POST(self):  # noqa: N802
            import urllib.parse as _up
            parsed = _up.urlparse(self.path)
            path = parsed.path
            qs = _up.parse_qs(parsed.query)
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b""
            if path == "/mcp":
                session_id = qs.get("session_id", [None])[0]
                _tl.session_id = session_id
                resp = srv.handle_post_mcp(body, session_id)
                _tl.session_id = None
                self._send_json(200, resp)
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
            else:
                self._send_json(404, {"ok": False, "error": "not_found"})

    return _Handler
