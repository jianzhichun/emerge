from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from scripts.policy_config import events_root

_KEEPALIVE_INTERVAL_S = 20.0

def _validate_machine_id(machine_id: str) -> None:
    """Reject machine_id values that could escape the event root via path traversal."""
    if not machine_id or machine_id != machine_id.strip():
        raise ValueError("machine_id is required and must not have leading/trailing whitespace")
    p = Path(machine_id)
    if p.name != machine_id or ".." in machine_id or "/" in machine_id or "\\" in machine_id:
        raise ValueError(f"Invalid machine_id: {machine_id!r}")


def resolve_daemon_bind(override: str | None = None) -> str:
    """Resolve bind address for ThreadingHTTPServer (CLI override, else EMERGE_DAEMON_BIND)."""
    raw = override if override is not None else os.environ.get("EMERGE_DAEMON_BIND", "127.0.0.1")
    raw = (raw or "").strip()
    if not raw:
        raw = "127.0.0.1"
    try:
        ipaddress.ip_address(raw)
    except ValueError as e:
        raise ValueError(
            "EMERGE_DAEMON_BIND / --bind must be a valid IP address "
            f"(e.g. 127.0.0.1 or 0.0.0.0); got {raw!r}"
        ) from e
    return raw


def _runtime_fingerprint() -> str:
    """Hash key runtime files so ensure-running can detect stale daemon code."""
    root = Path(__file__).resolve().parents[1]
    watched = (
        root / "scripts" / "daemon_http.py",
        root / "scripts" / "emerge_daemon.py",
        root / "scripts" / "admin" / "cockpit.py",
        root / "scripts" / "admin" / "control_plane.py",
        root / "scripts" / "admin" / "cockpit" / "dist" / "index.html",
    )
    h = hashlib.sha1()
    for p in watched:
        try:
            st = p.stat()
            h.update(f"{p.name}:{st.st_mtime_ns}:{st.st_size}".encode("utf-8"))
        except OSError:
            h.update(f"{p.name}:missing".encode("utf-8"))
    return h.hexdigest()


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
        bind_host: str | None = None,
    ) -> None:
        self._daemon = daemon
        self._bind_host = resolve_daemon_bind(bind_host)
        self._port = port
        self._pid_path = pid_path or (Path.home() / ".emerge" / "daemon.pid")
        self._event_root = event_root or (Path.home() / ".emerge" / "operator-events")
        self._state_root = state_root or (Path.home() / ".emerge" / "repl")
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        # Connected runners: runner_profile → {connected_at_ms, last_event_ts_ms, machine_id, last_alert}
        self._connected_runners: dict[str, dict] = {}
        self._runners_lock = threading.Lock()
        # runner_profile → wfile for SSE command push
        self._runner_sse_clients: dict[str, Any] = {}
        # popup_id → threading.Event
        self._popup_futures: dict[str, threading.Event] = {}
        # Timestamp of the last POST /mcp request (CC tool call)
        self._last_mcp_ts: float = 0.0
        self._popup_results: dict[str, dict] = {}
        self._popup_lock = threading.Lock()
        # Pattern detection: per-runner sliding-window event buffers
        from scripts.pattern_detector import PatternDetector as _PatternDetector
        self._detector = _PatternDetector()
        self._runner_event_buffers: dict[str, deque] = {}
        self._runner_buffers_lock = threading.Lock()
        # Cockpit UI + /api/* when served on the same port as MCP (see InProcessCockpitBridge)
        self._cockpit_sse_clients: list[Any] = []
        self._cockpit_sse_lock = threading.Lock()
        self._cockpit_injected_html: dict[str, Any] = {}
        self._cockpit_inject_lock = threading.Lock()

    def cockpit_broadcast(self, event: dict) -> None:
        """Push SSE event to cockpit browsers connected to /api/sse/status."""
        data = f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode()
        with self._cockpit_sse_lock:
            dead = []
            for wfile in self._cockpit_sse_clients:
                try:
                    wfile.write(data)
                    wfile.flush()
                except OSError:
                    dead.append(wfile)
            for wfile in dead:
                self._cockpit_sse_clients.remove(wfile)

    def _notify_cockpit_broadcast(self, event: dict) -> None:
        """Tests may set daemon._cockpit_server to a mock; else use merged cockpit SSE."""
        cockpit = getattr(self._daemon, "_cockpit_server", None)
        if cockpit is not None:
            cockpit.broadcast(event)
        else:
            self.cockpit_broadcast(event)

    @property
    def port(self) -> int:
        if self._server is None:
            return self._port
        return self._server.server_address[1]

    @property
    def bind_host(self) -> str:
        return self._bind_host

    def start(self) -> None:
        handler = _make_handler(self)
        self._server = ThreadingHTTPServer((self._bind_host, self._port), handler)
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
            json.dumps({
                "pid": os.getpid(),
                "host": self._bind_host,
                "port": self.port,
                "version": str(getattr(self._daemon, "_version", "0.0.0")),
                "code_fingerprint": _runtime_fingerprint(),
            }),
            encoding="utf-8",
        )

    def handle_post_mcp(self, body: bytes, session_id: str | None) -> dict | None:
        """Dispatch a JSON-RPC request to the daemon.

        Returns None when the request is a notification (no `id`) so the HTTP
        layer can respond with 202 Accepted and an empty body. JSON-RPC 2.0
        requires notifications to be one-way — returning a fabricated result
        dict for a missing id violates the spec.
        """
        try:
            req = json.loads(body)
        except json.JSONDecodeError as exc:
            return {"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": f"Parse error: {exc}"}}
        req_id = req.get("id")
        try:
            return self._daemon.handle_jsonrpc(req)
        except Exception as exc:
            if req_id is None:
                return None  # notification — swallow exception, return 202
            return {"jsonrpc": "2.0", "id": req_id,
                    "error": {"code": -32603, "message": str(exc)}}

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
        self._append_event(events_root(self._state_root) / "events.jsonl", {
            "type": "runner_discovered",
            "ts_ms": now_ms,
            "runner_profile": runner_profile,
            "machine_id": machine_id,
        })
        self._append_event(events_root(self._state_root) / f"events-{runner_profile}.jsonl", {
            "type": "runner_online",
            "ts_ms": now_ms,
            "runner_profile": runner_profile,
            "machine_id": machine_id,
        })
        self._write_monitor_state()
        self._notify_cockpit_broadcast({"monitors_updated": True})

    def _on_runner_event(self, payload: dict) -> None:
        runner_profile = str(payload.get("runner_profile", "")).strip()
        machine_id = str(payload.get("machine_id", "")).strip()
        ts_ms = int(time.time() * 1000)
        if machine_id:
            _validate_machine_id(machine_id)
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
            _orig_type = payload.get("type", "")
            _written_type = _orig_type if _orig_type == "operator_message" else "runner_event"
            self._append_event(events_root(self._state_root) / f"events-{runner_profile}.jsonl", {
                "type": _written_type,
                "ts_ms": ts_ms,
                "runner_profile": runner_profile,
                **{k: v for k, v in payload.items()
                   if k not in ("runner_profile", "type")},
            })

        # Pattern detection on runner push events (skip operator chat messages)
        if runner_profile and payload.get("type") != "operator_message":
            window_ms = self._detector.FREQ_WINDOW_MS
            with self._runner_buffers_lock:
                buf = self._runner_event_buffers.setdefault(runner_profile, deque())
                buf.append({
                    **{k: v for k, v in payload.items()
                       if k not in ("runner_profile", "ts_ms", "machine_id")},
                    "ts_ms": ts_ms,
                    "machine_id": machine_id or runner_profile,
                })
                while buf and ts_ms - buf[0].get("ts_ms", 0) > window_ms:
                    buf.popleft()
                snapshot = list(buf)

            summaries = self._detector.ingest(snapshot)
            for summary in summaries:
                try:
                    stage = self._daemon._span_tracker.get_policy_status(
                        summary.intent_signature
                    )
                except Exception:
                    stage = summary.policy_stage  # fallback: "explore"

                alert = {
                    "type": "pattern_alert",
                    "ts_ms": ts_ms,
                    "runner_profile": runner_profile,
                    "stage": stage,
                    "intent_signature": summary.intent_signature,
                    "meta": {
                        "occurrences": summary.occurrences,
                        "window_minutes": round(summary.window_minutes, 1),
                        "machine_ids": summary.machine_ids,
                        "detector_signals": summary.detector_signals,
                    },
                }
                self._append_event(
                    events_root(self._state_root) / f"events-{runner_profile}.jsonl", alert
                )
                with self._runners_lock:
                    if runner_profile in self._connected_runners:
                        self._connected_runners[runner_profile]["last_alert"] = {
                            "stage": stage,
                            "intent_signature": summary.intent_signature,
                            "ts_ms": ts_ms,
                        }

            # Single _write_monitor_state call captures both last_event_ts_ms and last_alert
            self._write_monitor_state()
            if summaries:
                self._notify_cockpit_broadcast({"monitors_updated": True})

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
        if ui_spec.get("type") == "toast":
            # Fire-and-forget: popup_id="" is an intentional sentinel — runner never posts back
            # a popup-result for toasts, so there is nothing to correlate.
            command = json.dumps({"type": "notify", "popup_id": "", "ui_spec": ui_spec})
            with self._runners_lock:
                wfile = self._runner_sse_clients.get(runner_profile)
            if wfile is None:
                return {"ok": False, "error": "runner_not_connected"}
            try:
                wfile.write(f"data: {command}\n\n".encode())
                wfile.flush()
                return {"ok": True}
            except OSError:
                with self._runners_lock:
                    self._runner_sse_clients.pop(runner_profile, None)
                return {"ok": False, "error": "runner_disconnected"}
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
        state = {"runners": runners, "team_active": len(runners) > 0,
                 "updated_ts_ms": int(time.time() * 1000)}
        path = events_root(self._state_root) / "runner-monitor-state.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with _tf.NamedTemporaryFile("w", dir=path.parent, delete=False,
                                        suffix=".tmp", encoding="utf-8") as tf:
                json.dump(state, tf)
                tf.flush()
                os.fsync(tf.fileno())
                tf_path = tf.name
            os.replace(tf_path, path)
        except OSError:
            pass


def ensure_running_or_launch(
    pid_path: Path | None = None,
    port: int = 8789,
    daemon_factory: Any = None,
    bind_host: str | None = None,
) -> str:
    """Check if daemon is running via PID file.
    Returns 'already_running', 'launched', or 'not_running'.
    """
    import signal

    expected_fp = _runtime_fingerprint()

    pid_path = pid_path or (Path.home() / ".emerge" / "daemon.pid")
    if pid_path.exists():
        try:
            info = json.loads(pid_path.read_text(encoding="utf-8"))
            pid = int(info["pid"])
            os.kill(pid, 0)
            if pid == os.getpid():
                # Called in-process (tests / embeddings). Never self-terminate.
                return "already_running"
            running_fp = str(info.get("code_fingerprint", "") or "")
            if running_fp and running_fp == expected_fp:
                return "already_running"
            # Stale daemon code: stop old process so caller can start a fresh one.
            try:
                os.kill(pid, signal.SIGTERM)
                for _ in range(10):
                    time.sleep(0.1)
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                else:
                    os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            pid_path.unlink(missing_ok=True)
            return "not_running"
        except (ProcessLookupError, PermissionError, KeyError, ValueError, json.JSONDecodeError):
            pid_path.unlink(missing_ok=True)
    if daemon_factory is None:
        return "not_running"
    daemon_obj = daemon_factory()
    srv = DaemonHTTPServer(
        daemon=daemon_obj, port=port, pid_path=pid_path, bind_host=bind_host
    )
    srv.start()
    return "launched"


def _is_cockpit_http_path(path: str) -> bool:
    """True for cockpit routes. Avoids matching /apis, /apix, etc. (prefix /api alone is wrong)."""
    if path in ("/", "/index.html"):
        return True
    if path == "/api" or path.startswith("/api/"):
        return True
    return path.startswith("/assets/")


def _coerce_request_target_to_path(handler: Any) -> None:
    """RFC 7230 absolute-form request-target is a full URL; urlparse().path can be ''.

    Browsers may send ``GET http://host:port HTTP/1.1`` with no path segment — normalize to ``/``
    so Cockpit and route tables see the root path.
    """
    import urllib.parse as _up
    raw = handler.path
    if raw.startswith(("http://", "https://")):
        p = _up.urlparse(raw)
        path = p.path or "/"
        handler.path = path + (("?" + p.query) if p.query else "")


def _make_handler(srv: "DaemonHTTPServer"):
    from scripts.admin.cockpit import InProcessCockpitBridge, _CockpitHandler

    _bridge = InProcessCockpitBridge(srv._daemon, srv)

    class _Handler(_CockpitHandler):
        def log_message(self, *args): pass

        def _send_json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):  # noqa: N802
            import urllib.parse as _up
            _coerce_request_target_to_path(self)
            path = _up.urlparse(self.path).path or "/"
            if _is_cockpit_http_path(path):
                self._cockpit = _bridge
                return _CockpitHandler.do_OPTIONS(self)
            self.send_error(404)

        def do_GET(self):  # noqa: N802
            import urllib.parse as _up
            _coerce_request_target_to_path(self)
            path = _up.urlparse(self.path).path or "/"
            if _is_cockpit_http_path(path):
                self._cockpit = _bridge
                return _CockpitHandler.do_GET(self)
            self._cockpit = None
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
            elif path == "/runner-dist/runner.tar.gz":
                from scripts.admin.runner import _build_runner_tarball
                _plugin_root = Path(__file__).resolve().parents[1]
                data = _build_runner_tarball(_plugin_root)
                self.send_response(200)
                self.send_header("Content-Type", "application/gzip")
                self.send_header("Content-Disposition", 'attachment; filename="runner.tar.gz"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif path == "/runner-dist/runner.tar.gz.sha256":
                from scripts.admin.runner import _build_runner_tarball
                _plugin_root = Path(__file__).resolve().parents[1]
                data = _build_runner_tarball(_plugin_root)
                digest = hashlib.sha256(data).hexdigest()
                body = f"{digest}  runner.tar.gz\n".encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path in ("/runner-install.sh", "/runner-install.ps1"):
                import urllib.parse as _up_ri
                qs_ri = _up_ri.parse_qs(_up_ri.urlparse(self.path).query)
                try:
                    runner_port = int((qs_ri.get("port") or ["8787"])[0])
                except ValueError:
                    runner_port = 8787
                from scripts.admin.runner import (
                    _generate_runner_install_ps1,
                    _generate_runner_install_sh,
                )
                from scripts.admin.shared import _detect_lan_ip
                lan_ip = _detect_lan_ip()
                dport = srv.port
                team_lead_url = f"http://{lan_ip}:{dport}".rstrip("/")
                if path.endswith(".sh"):
                    body = _generate_runner_install_sh(
                        team_lead_url=team_lead_url,
                        runner_port=runner_port,
                    ).encode("utf-8")
                    ctype = "text/x-sh; charset=utf-8"
                    fname = "runner-install.sh"
                else:
                    body = _generate_runner_install_ps1(
                        team_lead_url=team_lead_url,
                        runner_port=runner_port,
                    ).encode("utf-8")
                    ctype = "text/plain; charset=utf-8"
                    fname = "runner-install.ps1"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._send_json(404, {"ok": False, "error": "not_found"})

        def _handle_sse_mcp(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            session_id = uuid.uuid4().hex
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
                    srv._notify_cockpit_broadcast({"monitors_updated": True})

        def do_POST(self):  # noqa: N802
            import urllib.parse as _up
            _coerce_request_target_to_path(self)
            parsed = _up.urlparse(self.path)
            path = parsed.path or "/"
            qs = _up.parse_qs(parsed.query)
            if _is_cockpit_http_path(path):
                self._cockpit = _bridge
                return _CockpitHandler.do_POST(self)
            self._cockpit = None
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b""
            if path == "/mcp":
                srv._last_mcp_ts = time.time()
                session_id = qs.get("session_id", [None])[0]
                resp = srv.handle_post_mcp(body, session_id)
                if resp is None:
                    # JSON-RPC notification — respond 202 Accepted with no body.
                    self.send_response(202)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
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
