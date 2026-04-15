"""Cockpit HTTP server — serves the browser UI and REST API.

Depends on scripts.admin.api for all data-manipulation logic.  No business
logic lives here — only HTTP routing, request parsing, and response
serialisation.
"""
from __future__ import annotations

import http.server
import json
import os
import re
import socketserver
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

import sys
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.admin.api import (
    _sse_clients,
    _sse_lock,
    _sse_broadcast,
    _cockpit_inject_html,
    _cockpit_list_injected_html,
    _COCKPIT_INJECTED_HTML,
    _COCKPIT_INJECT_LOCK,
    cmd_assets,
    cmd_submit_actions,
    _cmd_set_goal,
    _cmd_goal_history,
    _cmd_goal_rollback,
    _cmd_save_settings,
)
from scripts.admin.shared import _resolve_repl_root, _resolve_connector_root
from scripts.admin.control_plane import (
    _load_hook_state_summary,
    cmd_control_plane_state,
    cmd_control_plane_intents,
    cmd_control_plane_session,
    cmd_control_plane_hook_state,
    cmd_control_plane_exec_events,
    cmd_control_plane_tool_events,
    cmd_control_plane_pipeline_events,
    cmd_control_plane_spans,
    cmd_control_plane_span_candidates,
    cmd_control_plane_reflection_cache,
    cmd_control_plane_monitors,
    cmd_control_plane_delta_reconcile,
    cmd_control_plane_risk_update,
    cmd_control_plane_risk_add,
    cmd_control_plane_policy_freeze,
    cmd_control_plane_policy_unfreeze,
    cmd_control_plane_session_export,
    cmd_control_plane_session_reset,
)
from scripts.admin.pipeline import cmd_policy_status


def _make_cockpit_handler(cockpit: "CockpitHTTPServer"):
    """Return a _CockpitHandler subclass bound to cockpit's instance state."""
    class _Handler(_CockpitHandler):
        _cockpit = cockpit
    return _Handler


class _ReuseAddrTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True


class _CockpitHandler(http.server.BaseHTTPRequestHandler):
    _shell_path: "Path" = Path(__file__).parent.parent / "cockpit_shell.html"
    _cockpit: "CockpitHTTPServer | None" = None  # set by _make_cockpit_handler()

    def log_message(self, fmt: str, *args: object) -> None:  # suppress request logs
        pass

    def _cors_origin(self) -> str:
        """Return a safe CORS origin — only allow localhost, never wildcard."""
        origin = self.headers.get("Origin", "")
        if origin.startswith("http://localhost") or origin.startswith("http://127.0.0.1"):
            return origin
        return "null"  # deny cross-origin requests from other sites

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", self._cors_origin())
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._serve_shell()
        elif path == "/api/policy":
            self._json(cmd_policy_status())
        elif path == "/api/assets":
            ih = self._cockpit._injected_html if self._cockpit is not None else None
            self._json(cmd_assets(injected_html=ih))
        elif path == "/api/status":
            state_root = _resolve_repl_root()
            pending = (state_root / "pending-actions.json").exists()
            self._json({"ok": True, "pending": pending, "server_online": True})
        elif path == "/api/settings":
            from scripts.policy_config import load_settings, default_settings_path
            s = load_settings()
            self._json({"ok": True, "settings": s, "path": str(default_settings_path())})
        elif path == "/api/goal":
            self._json({"ok": True, **_load_hook_state_summary()})
        elif path == "/api/goal-history":
            raw_limit = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("limit", ["50"])[0]
            try:
                limit = max(1, min(500, int(raw_limit)))
            except Exception:
                limit = 50
            self._json(_cmd_goal_history(limit=limit))
        elif path.startswith("/api/components/"):
            self._serve_component(path)
        elif path == "/api/control-plane/state":
            self._json(cmd_control_plane_state())
        elif path == "/api/control-plane/intents":
            self._json(cmd_control_plane_intents())
        elif path == "/api/control-plane/session":
            self._json(cmd_control_plane_session())
        elif path == "/api/control-plane/hook-state":
            self._json(cmd_control_plane_hook_state())
        elif path.startswith("/api/control-plane/exec-events"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._json(cmd_control_plane_exec_events(
                limit=int(qs.get("limit", ["100"])[0]),
                since_ms=int(qs.get("since_ms", ["0"])[0]),
                intent=qs.get("intent", [""])[0],
                intent_prefix=qs.get("intent_prefix", [""])[0],
            ))
        elif path.startswith("/api/control-plane/pipeline-events"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._json(cmd_control_plane_pipeline_events(
                limit=int(qs.get("limit", ["100"])[0]),
                since_ms=int(qs.get("since_ms", ["0"])[0]),
                intent=qs.get("intent", [""])[0],
                intent_prefix=qs.get("intent_prefix", [""])[0],
            ))
        elif path.startswith("/api/control-plane/tool-events"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._json(cmd_control_plane_tool_events(
                limit=int(qs.get("limit", ["200"])[0]),
                since_ms=int(qs.get("since_ms", ["0"])[0]),
            ))
        elif path == "/api/control-plane/monitors":
            if self._cockpit is not None:
                self._json(self._cockpit.get_monitor_data())
            else:
                self._json(cmd_control_plane_monitors())
        elif path == "/api/control-plane/span-candidates":
            self._json(cmd_control_plane_span_candidates())
        elif path == "/api/control-plane/reflection-cache":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                ttl_ms = int(qs.get("ttl_ms", ["900000"])[0])
            except Exception:
                ttl_ms = 900000
            self._json(cmd_control_plane_reflection_cache(ttl_ms=ttl_ms))
        elif path.startswith("/api/control-plane/spans"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._json(cmd_control_plane_spans(
                limit=int(qs.get("limit", ["50"])[0]),
                intent=qs.get("intent", [""])[0],
                intent_prefix=qs.get("intent_prefix", [""])[0],
            ))
        elif path == "/api/sse/status":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", self._cors_origin())
            self.end_headers()
            msg = json.dumps({
                "status": "online",
                "pid": os.getpid(),
                "ts_ms": int(time.time() * 1000),
            }, ensure_ascii=False)
            self.wfile.write(f"data: {msg}\n\n".encode())
            self.wfile.flush()
            if self._cockpit is not None:
                with self._cockpit._sse_lock:
                    self._cockpit._sse_clients.append(self.wfile)
            else:
                with _sse_lock:
                    _sse_clients.append(self.wfile)
            try:
                while True:
                    time.sleep(25)
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
            except OSError:
                pass
            finally:
                if self._cockpit is not None:
                    with self._cockpit._sse_lock:
                        if self.wfile in self._cockpit._sse_clients:
                            self._cockpit._sse_clients.remove(self.wfile)
                else:
                    with _sse_lock:
                        if self.wfile in _sse_clients:
                            _sse_clients.remove(self.wfile)
            return
        else:
            self._err(404)

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        body: dict = json.loads(self.rfile.read(length)) if length else {}
        if path == "/api/submit":
            actions = body.get("actions", [])
            result = cmd_submit_actions(actions)
            self._json(result)
            if result.get("ok"):
                _ev = {"pending": True, "action_count": result.get("action_count", 0),
                       "ts_ms": int(time.time() * 1000)}
                if self._cockpit is not None:
                    self._cockpit.broadcast(_ev)
                else:
                    _sse_broadcast(_ev)
        elif path == "/api/settings":
            self._json(_cmd_save_settings(body))
        elif path == "/api/goal":
            self._json(_cmd_set_goal(body))
        elif path == "/api/goal/rollback":
            self._json(_cmd_goal_rollback(body))
        elif path == "/api/inject-component":
            connector = str(body.get("connector", "")).strip()
            html = str(body.get("html", ""))
            replace = bool(body.get("replace", False))
            slot_id: str | None = body.get("id") or None
            if slot_id is not None:
                slot_id = str(slot_id).strip() or None
            if connector and html:
                if self._cockpit is not None:
                    store = self._cockpit._injected_html
                    lock = self._cockpit._inject_lock
                else:
                    store, lock = None, None
                if replace:
                    lk = lock if lock is not None else _COCKPIT_INJECT_LOCK
                    d = store if store is not None else _COCKPIT_INJECTED_HTML
                    with lk:
                        d[connector] = [{"id": slot_id, "html": html}]
                else:
                    _cockpit_inject_html(connector, html, slot_id, store=store, lock=lock)
            self._json({"ok": True})
        elif path == "/api/control-plane/delta/reconcile":
            self._json(cmd_control_plane_delta_reconcile(
                delta_id=str(body.get("delta_id", "")),
                outcome=str(body.get("outcome", "")),
                intent_signature=str(body.get("intent_signature", "")),
            ))
        elif path == "/api/control-plane/risk/update":
            self._json(cmd_control_plane_risk_update(
                risk_id=str(body.get("risk_id", "")),
                action=str(body.get("action", "")),
                reason=str(body.get("reason", "")),
                snooze_duration_ms=int(body.get("snooze_duration_ms", 3600000)),
            ))
        elif path == "/api/control-plane/risk/add":
            self._json(cmd_control_plane_risk_add(
                text=str(body.get("text", "")),
                intent_signature=str(body.get("intent_signature", "")),
            ))
        elif path == "/api/control-plane/policy/freeze":
            self._json(cmd_control_plane_policy_freeze(key=str(body.get("key", ""))))
        elif path == "/api/control-plane/policy/unfreeze":
            self._json(cmd_control_plane_policy_unfreeze(key=str(body.get("key", ""))))
        elif path == "/api/control-plane/session/export":
            self._json(cmd_control_plane_session_export())
        elif path == "/api/control-plane/session/reset":
            full_v = body.get("full", False)
            full = bool(full_v) if isinstance(full_v, bool) else str(full_v).strip().lower() in {"1", "true", "yes", "on"}
            self._json(cmd_control_plane_session_reset(confirm=str(body.get("confirm", "")), full=full))
        else:
            self._err(404)

    def _serve_shell(self) -> None:
        if not self._shell_path.exists():
            body = b"<html><body><h1>Emerge Cockpit</h1><p>cockpit_shell.html not found</p></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = self._shell_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_component(self, path: str) -> None:
        parts = path.strip("/").split("/")  # ["api", "components", "connector", "filename"]
        if len(parts) != 4 or ".." in parts[2] or ".." in parts[3]:
            self._err(404)
            return
        connector, filename = parts[2], parts[3]
        if not filename.endswith(".html"):
            self._err(404)
            return
        try:
            connector_root = _resolve_connector_root()
            fpath = connector_root / connector / "cockpit" / filename
            fpath.resolve().relative_to(connector_root.resolve())
        except (ValueError, Exception):
            self._err(404)
            return
        if fpath.exists():
            body = fpath.read_bytes()
        else:
            m = re.fullmatch(r"injected-runtime-(\d+)\.html", filename)
            if not m:
                self._err(404)
                return
            idx = int(m.group(1))
            store = self._cockpit._injected_html if self._cockpit is not None else None
            injected = _cockpit_list_injected_html(connector, store=store)
            if not (0 <= idx < len(injected)):
                self._err(404)
                return
            body = injected[idx].encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", self._cors_origin())
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code: int) -> None:
        self.send_response(code)
        self.end_headers()


class _StandaloneDaemonStub:
    """Minimal daemon stub for CockpitHTTPServer in standalone CLI mode.
    get_monitor_data() falls back to runner-monitor-state.json."""
    _http_server = None


class CockpitHTTPServer:
    """Cockpit HTTP server that can run inside the daemon process (in-process mode)
    or standalone (CLI mode via cmd_serve).

    In in-process mode, daemon._http_server is set and get_monitor_data() reads
    _connected_runners from memory (zero file I/O). In standalone mode, fallback
    reads runner-monitor-state.json.
    """

    def __init__(
        self,
        daemon: object,
        port: int = 0,
        repl_root: Path | None = None,
        connector_root: Path | None = None,
    ) -> None:
        self._daemon = daemon
        self._port = port
        self._repl_root = repl_root or _resolve_repl_root()
        self._connector_root = connector_root or _resolve_connector_root()
        self._server: socketserver.TCPServer | None = None
        self._thread: threading.Thread | None = None
        self._sse_clients: list = []
        self._sse_lock = threading.Lock()
        self._injected_html: dict = {}
        self._inject_lock = threading.Lock()
        self.url: str | None = None

    def start(self) -> str:
        """Start cockpit HTTP server in daemon thread. Returns URL."""
        handler = _make_cockpit_handler(self)
        self._server = _ReuseAddrTCPServer(("127.0.0.1", self._port), handler)
        actual_port = self._server.server_address[1]
        self.url = f"http://localhost:{actual_port}"
        pid_path = _cockpit_pid_path(self._repl_root)
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(
            json.dumps({"pid": os.getpid(), "port": actual_port, "cwd": str(Path.cwd())}),
            encoding="utf-8",
        )
        import atexit as _atexit
        _atexit.register(self.stop)
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="CockpitHTTPServer"
        )
        self._thread.start()
        return self.url

    def stop(self) -> None:
        if self._server:
            try:
                self._server.shutdown()
            except Exception:
                pass
            self._server = None
        _cockpit_pid_path(self._repl_root).unlink(missing_ok=True)

    def broadcast(self, event: dict) -> None:
        """Push SSE event to all connected browser clients."""
        data = f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode()
        with self._sse_lock:
            dead = []
            for wfile in self._sse_clients:
                try:
                    wfile.write(data)
                    wfile.flush()
                except OSError:
                    dead.append(wfile)
            for wfile in dead:
                self._sse_clients.remove(wfile)

    def get_monitor_data(self) -> dict:
        """Return runner monitor data. Reads from daemon memory; falls back to file."""
        hsrv = getattr(self._daemon, "_http_server", None)
        if hsrv is not None:
            with hsrv._runners_lock:
                items = list(hsrv._connected_runners.items())
            runners = [
                {
                    "runner_profile": profile,
                    "connected": True,
                    "connected_at_ms": info.get("connected_at_ms", 0),
                    "last_event_ts_ms": info.get("last_event_ts_ms", 0),
                    "machine_id": info.get("machine_id", ""),
                    "last_alert": info.get("last_alert"),
                }
                for profile, info in items
            ]
            return {"runners": runners, "team_active": len(runners) > 0}
        # Standalone mode: fallback to file
        state_path = self._repl_root / "runner-monitor-state.json"
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


def _cockpit_pid_path(repl_root: Path | None = None) -> Path:
    return (repl_root or _resolve_repl_root()) / "cockpit.pid"


def cmd_serve(port: int = 0, open_browser: bool = False) -> dict:
    """Start the cockpit HTTP server. Idempotent — returns existing instance if already
    running FOR THE SAME PROJECT. If a server is running for a different project (cwd
    mismatch), it is stopped and a new one is started.
    """
    import signal as _signal
    repl_root = _resolve_repl_root()
    pid_path = _cockpit_pid_path(repl_root)
    current_cwd = str(Path.cwd())

    # Reuse existing instance if alive AND same project
    if pid_path.exists():
        try:
            info = json.loads(pid_path.read_text(encoding="utf-8"))
            existing_pid = int(info["pid"])
            existing_port = int(info["port"])
            existing_cwd = info.get("cwd", "")
            os.kill(existing_pid, 0)
            if existing_cwd == current_cwd:
                url = f"http://localhost:{existing_port}"
                if open_browser:
                    webbrowser.open(url)
                return {"ok": True, "port": existing_port, "url": url, "reused": True}
            try:
                os.kill(existing_pid, _signal.SIGTERM)
            except OSError:
                pass
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            pass
        pid_path.unlink(missing_ok=True)

    cockpit = CockpitHTTPServer(daemon=_StandaloneDaemonStub(), port=port, repl_root=repl_root)
    url = cockpit.start()
    actual_port = int(url.split(":")[-1])

    import atexit as _atexit
    _atexit.register(lambda: cockpit.broadcast({"status": "offline"}))

    if open_browser:
        webbrowser.open(url)
    return {"ok": True, "port": actual_port, "url": url, "reused": False, "cockpit": cockpit}


def cmd_serve_stop() -> dict:
    """Stop the running cockpit server by killing the pid in cockpit.pid."""
    import signal as _signal
    pid_path = _cockpit_pid_path()
    if not pid_path.exists():
        return {"ok": False, "reason": "no cockpit.pid found — server may not be running"}
    try:
        info = json.loads(pid_path.read_text(encoding="utf-8"))
        pid = int(info["pid"])
        port = int(info["port"])
        os.kill(pid, _signal.SIGTERM)
        pid_path.unlink(missing_ok=True)
        return {"ok": True, "stopped_pid": pid, "port": port}
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as e:
        pid_path.unlink(missing_ok=True)
        return {"ok": False, "reason": str(e)}
