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
import uuid
import webbrowser
from pathlib import Path

import sys
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.admin.api import (
    _cockpit_inject_html,
    _cockpit_list_injected_html,
    cmd_assets,
    _enrich_actions,
    _validate_action,
    _cmd_save_settings,
)
from scripts.admin.actions import ActionRegistry
from scripts.admin.shared import _resolve_state_root, _resolve_connector_root
from scripts.policy_config import events_root
from scripts.admin.control_plane import (
    cmd_control_plane_state,
    cmd_control_plane_sessions,
    cmd_control_plane_intent_history,
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
    cmd_control_plane_runner_events,
    cmd_control_plane_watchers,
    cmd_control_plane_delta_reconcile,
    cmd_control_plane_risk_update,
    cmd_control_plane_risk_add,
    cmd_control_plane_policy_freeze,
    cmd_control_plane_policy_unfreeze,
    cmd_control_plane_session_export,
    cmd_control_plane_session_reset,
)
from scripts.admin.pipeline import cmd_policy_status
from scripts.admin.runner import cmd_runner_install_url

_SESSION_ID_PARAM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_STATUS_TAIL_BYTES = 256 * 1024
_MAX_QUEUE_ACTIONS = 50
_MAX_INJECT_HTML_BYTES = 64 * 1024


def _load_recent_jsonl(path: Path, max_tail_bytes: int = _STATUS_TAIL_BYTES) -> list[dict]:
    """Load recent JSON lines from file tail (best-effort)."""
    if not path.exists():
        return []
    try:
        size = path.stat().st_size
        start = max(0, size - max_tail_bytes)
        with path.open("rb") as f:
            f.seek(start)
            data = f.read().decode("utf-8", errors="ignore")
        rows = []
        for line in data.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
        return rows
    except OSError:
        return []


def _latest_cockpit_dispatch_status(state_root: Path) -> dict:
    ev_root = events_root(state_root)
    events = _load_recent_jsonl(ev_root / "events.jsonl")
    acks = _load_recent_jsonl(ev_root / "cockpit-action-acks.jsonl")
    last_event = None
    for row in reversed(events):
        if row.get("type") == "cockpit_action":
            last_event = row
            break
    last_ack = acks[-1] if acks else None
    event_id = str((last_event or {}).get("event_id", "")).strip() or None
    event_ts = int((last_event or {}).get("ts_ms", 0) or 0) or None
    ack_event_id = str((last_ack or {}).get("event_id", "")).strip() or None
    ack_ts = int((last_ack or {}).get("ack_ts_ms", 0) or 0) or None
    ack_pending = bool(event_id and event_id != ack_event_id)
    ack_lag_ms = None
    if (
        event_id
        and ack_event_id
        and event_id == ack_event_id
        and event_ts is not None
        and ack_ts is not None
    ):
        ack_lag_ms = max(0, ack_ts - event_ts)
    return {
        "last_cockpit_event_id": event_id,
        "last_cockpit_event_ts_ms": event_ts,
        "last_cockpit_ack_event_id": ack_event_id,
        "last_cockpit_ack_ts_ms": ack_ts,
        "cockpit_ack_pending": ack_pending,
        "cockpit_ack_lag_ms": ack_lag_ms,
    }


def _parse_netloc_host_port(netloc: str) -> tuple[str, int | None]:
    """Split Host / Origin netloc into (host lowercased, port or None)."""
    netloc = netloc.strip()
    if not netloc:
        return ("", None)
    if netloc.startswith("["):
        end = netloc.find("]")
        if end == -1:
            return (netloc.lower(), None)
        host_inside = netloc[1:end].lower()
        tail = netloc[end + 1 :].lstrip(":")
        if tail.isdigit():
            return (host_inside, int(tail))
        return (host_inside, None)
    if ":" in netloc:
        host, _, port_s = netloc.rpartition(":")
        if port_s.isdigit():
            return (host.lower(), int(port_s))
    return (netloc.lower(), None)


def resolve_cors_allow_origin(origin: str, host_header: str) -> str:
    """Return Origin to echo in Access-Control-Allow-Origin, or the literal 'null' if disallowed.

    Allows: exact Host match (including LAN IP:port when daemon binds 0.0.0.0), and
    loopback alias equivalence (localhost vs 127.0.0.1 vs ::1) only when ports match.
    Does not use a blanket allow-list for 127.0.0.1 (would ignore Host port otherwise).
    """
    origin = (origin or "").strip()
    host_header = (host_header or "").strip()
    if not origin:
        return "null"
    try:
        p = urllib.parse.urlparse(origin)
        if p.scheme not in ("http", "https") or not p.netloc:
            return "null"
        if p.netloc.lower() == host_header.lower():
            return origin
        oh, op = _parse_netloc_host_port(p.netloc)
        hh, hp = _parse_netloc_host_port(host_header)
        if op is not None and hp is not None and op == hp:
            if oh == hh:
                return origin
            if oh in _LOOPBACK_HOSTS and hh in _LOOPBACK_HOSTS:
                return origin
    except Exception:
        pass
    return "null"


def _make_cockpit_handler(cockpit: "CockpitHTTPServer"):
    """Return a _CockpitHandler subclass bound to cockpit's instance state."""
    class _Handler(_CockpitHandler):
        _cockpit = cockpit
    return _Handler


class _ReuseAddrTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True


class _CockpitHandler(http.server.BaseHTTPRequestHandler):
    _dist_dir: "Path" = Path(__file__).parent / "cockpit" / "dist"
    _dist_index_path: "Path" = _dist_dir / "index.html"
    _cockpit: "CockpitHTTPServer | None" = None  # set by _make_cockpit_handler()

    def log_message(self, fmt: str, *args: object) -> None:  # suppress request logs
        pass

    def _cors_origin(self) -> str:
        """Return a safe CORS Allow-Origin value (see resolve_cors_allow_origin)."""
        return resolve_cors_allow_origin(
            self.headers.get("Origin", ""),
            self.headers.get("Host", ""),
        )

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", self._cors_origin())
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        qs_all = urllib.parse.parse_qs(parsed_url.query)
        session_id_q = (qs_all.get("session_id", [""])[0] or "").strip() or None
        if session_id_q and not _SESSION_ID_PARAM_RE.fullmatch(session_id_q):
            return self._json({"ok": False, "error": "invalid session_id"}, status=400)
        if path in ("/", "/index.html"):
            self._serve_shell()
        elif path.startswith("/assets/"):
            self._serve_asset(path)
        elif path == "/api/policy":
            self._json(cmd_policy_status(session_id=session_id_q))
        elif path == "/api/assets":
            self._json(cmd_assets(injected_html=self._cockpit._injected_html))
        elif path == "/api/action-types":
            self._json({"ok": True, "types": ActionRegistry.describe()})
        elif path == "/api/cockpit-sdk.js":
            self._serve_cockpit_sdk_js()
        elif path == "/api/status":
            import time as _t_status
            _CC_ACTIVE_WINDOW_S = 120
            state_root = _resolve_state_root()
            last_mcp_ts = 0.0
            if self._cockpit is not None:
                hsrv = getattr(self._cockpit, "_http_srv", None)
                if hsrv is not None:
                    last_mcp_ts = getattr(hsrv, "_last_mcp_ts", 0.0)
            cc_active = (_t_status.time() - last_mcp_ts) < _CC_ACTIVE_WINDOW_S if last_mcp_ts else False
            dispatch = _latest_cockpit_dispatch_status(state_root)
            from scripts.watchers import watcher_health_summary
            watchers = watcher_health_summary(state_root)
            self._json(
                {
                    "ok": True,
                    "pending": bool(dispatch["cockpit_ack_pending"]),
                    "server_online": True,
                    "cc_active": cc_active,
                    "watchers_healthy": bool(watchers["healthy"]),
                    "watchers_alive_count": int(watchers["alive_count"]),
                    "watchers_total": int(watchers["total"]),
                    "watchers_stale_ids": list(watchers.get("stale_watcher_ids", [])),
                    **dispatch,
                }
            )
        elif path == "/api/settings":
            from scripts.policy_config import load_settings, default_settings_path
            s = load_settings()
            self._json({"ok": True, "settings": s, "path": str(default_settings_path())})
        elif path.startswith("/api/components/"):
            self._serve_component(path)
        elif path == "/api/control-plane/state":
            self._json(cmd_control_plane_state())
        elif path == "/api/control-plane/sessions":
            _state_root = getattr(getattr(self._cockpit, "_http_srv", None), "_state_root", None)
            _current = getattr(getattr(self._cockpit, "_daemon", None), "_base_session_id", None)
            self._json(cmd_control_plane_sessions(state_root=_state_root, current_session_id=_current))
        elif path == "/api/control-plane/intents":
            self._json(cmd_control_plane_intents())
        elif path == "/api/control-plane/intent-history":
            key = (qs_all.get("intent") or qs_all.get("key") or [""])[0]
            try:
                limit = int((qs_all.get("limit") or ["0"])[0])
            except ValueError:
                limit = 0
            self._json(cmd_control_plane_intent_history(
                intent_signature=key,
                limit=limit if limit > 0 else None,
            ))
        elif path == "/api/control-plane/session":
            self._json(cmd_control_plane_session(session_id=session_id_q))
        elif path == "/api/control-plane/hook-state":
            self._json(cmd_control_plane_hook_state())
        elif path.startswith("/api/control-plane/exec-events"):
            qs = qs_all
            self._json(cmd_control_plane_exec_events(
                limit=int(qs.get("limit", ["100"])[0]),
                since_ms=int(qs.get("since_ms", ["0"])[0]),
                intent=qs.get("intent", [""])[0],
                intent_prefix=qs.get("intent_prefix", [""])[0],
                session_id=session_id_q,
            ))
        elif path.startswith("/api/control-plane/pipeline-events"):
            qs = qs_all
            self._json(cmd_control_plane_pipeline_events(
                limit=int(qs.get("limit", ["100"])[0]),
                since_ms=int(qs.get("since_ms", ["0"])[0]),
                intent=qs.get("intent", [""])[0],
                intent_prefix=qs.get("intent_prefix", [""])[0],
                session_id=session_id_q,
            ))
        elif path.startswith("/api/control-plane/tool-events"):
            qs = qs_all
            self._json(cmd_control_plane_tool_events(
                limit=int(qs.get("limit", ["200"])[0]),
                since_ms=int(qs.get("since_ms", ["0"])[0]),
                session_id=session_id_q,
            ))
        elif path == "/api/control-plane/monitors":
            if self._cockpit is not None:
                self._json(self._cockpit.get_monitor_data())
            else:
                self._json(cmd_control_plane_monitors())
        elif path == "/api/control-plane/watchers":
            state_root = _resolve_state_root()
            self._json(cmd_control_plane_watchers(state_root=state_root))
        elif path == "/api/control-plane/runner-events":
            qs_re = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            profile = (qs_re.get("profile") or [""])[0]
            try:
                limit = min(int((qs_re.get("limit") or ["20"])[0]), 100)
            except ValueError:
                limit = 20
            self._json(cmd_control_plane_runner_events(profile=profile, limit=limit))
        elif path == "/api/control-plane/runner-profiles":
            monitor_data = self._cockpit.get_monitor_data() if self._cockpit is not None else cmd_control_plane_monitors()
            known = [r["runner_profile"] for r in monitor_data.get("runners", []) if r.get("runner_profile")]
            self._json({"profiles": known})
        elif path == "/api/control-plane/runner-install-url":
            qs_riu = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                runner_port = int((qs_riu.get("runner_port") or qs_riu.get("port") or ["8787"])[0])
            except ValueError:
                runner_port = 8787
            daemon_port = 8789
            if self._cockpit is not None:
                hsrv = getattr(self._cockpit._daemon, "_http_server", None)
                if hsrv is not None:
                    daemon_port = int(hsrv.port)
            req_host = self.headers.get("Host", "").strip()
            tlu = None
            if req_host and not req_host.startswith(("0.0.0.0", "127.0.0.1", "localhost")):
                tlu = f"http://{req_host}"
            try:
                self._json(
                    cmd_runner_install_url(
                        runner_port=runner_port,
                        daemon_port=daemon_port,
                        team_lead_url=tlu,
                    )
                )
            except OSError as exc:
                self._json({"ok": False, "error": str(exc)})
        elif path == "/api/control-plane/span-candidates":
            self._json(cmd_control_plane_span_candidates())
        elif path == "/api/control-plane/reflection-cache":
            qs = qs_all
            try:
                ttl_ms = int(qs.get("ttl_ms", ["900000"])[0])
            except Exception:
                ttl_ms = 900000
            self._json(cmd_control_plane_reflection_cache(ttl_ms=ttl_ms))
        elif path.startswith("/api/control-plane/spans"):
            qs = qs_all
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
            client_id = uuid.uuid4().hex[:8]
            msg = json.dumps({
                "status": "online",
                "pid": os.getpid(),
                "ts_ms": int(time.time() * 1000),
            }, ensure_ascii=False)
            self.wfile.write(f"data: {msg}\n\n".encode())
            self.wfile.flush()
            if hasattr(self._cockpit, "register_sse_client"):
                self._cockpit.register_sse_client(client_id, self.wfile)
            else:
                with self._cockpit._sse_lock:
                    self._cockpit._sse_clients.append(self.wfile)
            try:
                while True:
                    time.sleep(25)
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
            except OSError:
                pass
            finally:
                if hasattr(self._cockpit, "unregister_sse_client"):
                    self._cockpit.unregister_sse_client(client_id)
                else:
                    with self._cockpit._sse_lock:
                        if self.wfile in self._cockpit._sse_clients:
                            self._cockpit._sse_clients.remove(self.wfile)
            return
        else:
            self._err(404)

    def do_POST(self) -> None:
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        qs_all = urllib.parse.parse_qs(parsed_url.query)
        session_id_q = (qs_all.get("session_id", [""])[0] or "").strip() or None
        if session_id_q and not _SESSION_ID_PARAM_RE.fullmatch(session_id_q):
            return self._json({"ok": False, "error": "invalid session_id"}, status=400)
        length = int(self.headers.get("Content-Length", 0))
        body: dict = json.loads(self.rfile.read(length)) if length else {}
        if path == "/api/submit":
            actions = body.get("actions", [])
            if not isinstance(actions, list) or not actions:
                result = {
                    "ok": False,
                    "error": "invalid_actions",
                    "message": "actions must be a non-empty list",
                }
                self._json(result)
                return
            if len(actions) > _MAX_QUEUE_ACTIONS:
                self._json(
                    {
                        "ok": False,
                        "error": "too_many_actions",
                        "message": f"actions exceeds limit ({_MAX_QUEUE_ACTIONS})",
                    },
                    status=400,
                )
                return
            for i, action in enumerate(actions):
                err = _validate_action(action)
                if err:
                    result = {
                        "ok": False,
                        "error": "invalid_action",
                        "message": f"action[{i}]: {err}",
                    }
                    self._json(result)
                    return
            enriched_actions = _enrich_actions(actions)
            event_id = f"cockpit-{uuid.uuid4().hex}"
            enriched_actions = [
                {**action, "action_id": f"{event_id}:{index + 1}"}
                for index, action in enumerate(enriched_actions)
            ]
            self._cockpit.write_event(
                {
                    "type": "cockpit_action",
                    "event_id": event_id,
                    "ts_ms": int(time.time() * 1000),
                    "actions": enriched_actions,
                }
            )
            result = {"ok": True, "action_count": len(enriched_actions), "event_id": event_id}
            self._json(result)
            if result.get("ok"):
                _ev = {
                    "pending": True,
                    "action_count": result.get("action_count", 0),
                    "event_id": event_id,
                    "ts_ms": int(time.time() * 1000),
                }
                self._cockpit.broadcast(_ev)
        elif path == "/api/settings":
            self._json(_cmd_save_settings(body))
        elif path == "/api/inject-component":
            connector = str(body.get("connector", "")).strip()
            html = str(body.get("html", ""))
            if len(html.encode("utf-8")) > _MAX_INJECT_HTML_BYTES:
                self._json(
                    {
                        "ok": False,
                        "error": "html_too_large",
                        "message": f"html exceeds {_MAX_INJECT_HTML_BYTES} bytes",
                    },
                    status=400,
                )
                return
            replace = bool(body.get("replace", False))
            slot_id: str | None = body.get("id") or None
            if slot_id is not None:
                slot_id = str(slot_id).strip() or None
            if connector and html:
                store = self._cockpit._injected_html
                lock = self._cockpit._inject_lock
                if replace:
                    with lock:
                        store[connector] = [{"id": slot_id, "html": html}]
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
            self._json(cmd_control_plane_session_export(session_id=session_id_q))
        elif path == "/api/control-plane/session/reset":
            full_v = body.get("full", False)
            full = bool(full_v) if isinstance(full_v, bool) else str(full_v).strip().lower() in {"1", "true", "yes", "on"}
            self._json(
                cmd_control_plane_session_reset(
                    confirm=str(body.get("confirm", "")),
                    full=full,
                    session_id=session_id_q,
                )
            )
        else:
            self._err(404)

    def _serve_shell(self) -> None:
        if not self._dist_index_path.is_file():
            body = (
                "<!doctype html><html><head><meta charset='utf-8'>"
                "<title>Emerge Cockpit</title></head><body>"
                "<h1>Emerge Cockpit</h1>"
                "<p>Build output missing: scripts/admin/cockpit/dist/index.html</p>"
                "<p>Run <code>cd scripts/admin/cockpit && npm install && npm run build</code>.</p>"
                "</body></html>"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = self._dist_index_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_cockpit_sdk_js(self) -> None:
        body = (
            "(function(){\n"
            "  if (window.emerge) { return; }\n"
            "  function post(kind, payload) {\n"
            "    window.parent.postMessage(Object.assign({type: kind}, payload || {}), window.location.origin);\n"
            "  }\n"
            "  var cache = null;\n"
            "  window.emerge = {\n"
            "    enqueue: function(action){ post('emerge:enqueue', {action: action}); },\n"
            "    dequeue: function(id){ post('emerge:dequeue', {id: id}); },\n"
            "    clear: function(){ post('emerge:clear', {}); },\n"
            "    actionTypes: function(){\n"
            "      if (cache) { return Promise.resolve(cache); }\n"
            "      return fetch('/api/action-types').then(function(r){ return r.json(); }).then(function(v){ cache = v; return v; });\n"
            "    }\n"
            "  };\n"
            "})();\n"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/javascript; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _serve_asset(self, path: str) -> None:
        raw_rel = path[len("/assets/") :]
        rel = urllib.parse.unquote(raw_rel)
        rel_path = Path(rel)
        if (
            not rel
            or rel_path.is_absolute()
            or any(part in {"", ".", ".."} for part in rel_path.parts)
        ):
            self._err(404)
            return
        assets_root = (self._dist_dir / "assets").resolve()
        try:
            fpath = (assets_root / rel_path).resolve()
            fpath.relative_to(assets_root)
        except (ValueError, OSError):
            self._err(404)
            return
        if not fpath.is_file():
            self._err(404)
            return

        suffix = fpath.suffix.lower()
        content_types = {
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".ico": "image/x-icon",
            ".map": "application/json; charset=utf-8",
        }
        body = fpath.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_types.get(suffix, "application/octet-stream"))
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
            injected = _cockpit_list_injected_html(connector, store=self._cockpit._injected_html)
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


class InProcessCockpitBridge:
    """Presents the same surface as `CockpitHTTPServer` for `_CockpitHandler` when the UI
    is served from `DaemonHTTPServer` (same TCP port as MCP, typically 8789)."""

    __slots__ = ("_daemon", "_http_srv")

    def __init__(self, daemon: object, http_srv: object) -> None:
        self._daemon = daemon
        self._http_srv = http_srv

    @property
    def _injected_html(self) -> dict:
        return self._http_srv._cockpit_injected_html

    @property
    def _inject_lock(self):
        return self._http_srv._cockpit_inject_lock

    @property
    def _sse_lock(self):
        return self._http_srv._cockpit_sse_lock

    @property
    def _sse_clients(self) -> list:
        return self._http_srv._cockpit_sse_clients

    def get_monitor_data(self) -> dict:
        hsrv = self._http_srv
        if hasattr(hsrv, "connected_runners_snapshot"):
            runners = hsrv.connected_runners_snapshot()
        else:
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

    def broadcast(self, event: dict) -> None:
        self._http_srv.cockpit_broadcast(event)

    def register_sse_client(self, client_id: str, wfile: Any) -> None:
        self._http_srv.register_cockpit_sse_client(client_id, wfile)

    def unregister_sse_client(self, client_id: str) -> None:
        self._http_srv.unregister_cockpit_sse_client(client_id)

    def write_event(self, event: dict) -> None:
        self._http_srv._append_event(events_root(self._http_srv._state_root) / "events.jsonl", event)


class _StandaloneDaemonStub:
    """Minimal daemon stub for CockpitHTTPServer in standalone CLI mode.
    get_monitor_data() falls back to runner-monitor-state.json."""
    _http_server = None


class CockpitHTTPServer:
    """Cockpit HTTP server that can run inside the daemon process (in-process mode)
    or standalone (CLI mode via cmd_serve).

    In in-process mode, daemon._http_server is set and get_monitor_data() reads
    runner snapshot from memory (zero file I/O). In standalone mode, fallback
    reads runner-monitor-state.json.
    """

    def __init__(
        self,
        daemon: object,
        port: int = 0,
        state_root: Path | None = None,
        connector_root: Path | None = None,
    ) -> None:
        self._daemon = daemon
        self._port = port
        self._state_root = state_root or _resolve_state_root()
        self._connector_root = connector_root or _resolve_connector_root()
        self._server: socketserver.TCPServer | None = None
        self._thread: threading.Thread | None = None
        self._sse_clients: list = []
        self._sse_lock = threading.Lock()
        self._injected_html: dict = {}
        self._inject_lock = threading.Lock()
        self.url: str | None = None

    def register_sse_client(self, _client_id: str, wfile: Any) -> None:
        with self._sse_lock:
            self._sse_clients.append(wfile)

    def unregister_sse_client(self, _client_id: str) -> None:
        # client id is not used in standalone mode; remove stale writers lazily in broadcast path.
        return

    def start(self) -> str:
        """Start cockpit HTTP server in daemon thread. Returns URL."""
        handler = _make_cockpit_handler(self)
        self._server = _ReuseAddrTCPServer(("127.0.0.1", self._port), handler)
        actual_port = self._server.server_address[1]
        self.url = f"http://localhost:{actual_port}"
        pid_path = _cockpit_pid_path(self._state_root)
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
        _cockpit_pid_path(self._state_root).unlink(missing_ok=True)

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

    def write_event(self, event: dict) -> None:
        path = events_root(self._state_root) / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def get_monitor_data(self) -> dict:
        """Return runner monitor data. Reads from daemon memory; falls back to file."""
        hsrv = getattr(self._daemon, "_http_server", None)
        if hsrv is not None:
            if hasattr(hsrv, "connected_runners_snapshot"):
                runners = hsrv.connected_runners_snapshot()
            else:
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
        state_path = events_root(self._state_root) / "runner-monitor-state.json"
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


def _cockpit_pid_path(state_root: Path | None = None) -> Path:
    return (state_root or _resolve_state_root()) / "cockpit.pid"


def cmd_serve(port: int = 0, open_browser: bool = False) -> dict:
    """Start the cockpit HTTP server. Idempotent — returns existing instance if already
    running FOR THE SAME PROJECT. If a server is running for a different project (cwd
    mismatch), it is stopped and a new one is started.
    """
    import signal as _signal
    state_root = _resolve_state_root()
    pid_path = _cockpit_pid_path(state_root)
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

    cockpit = CockpitHTTPServer(daemon=_StandaloneDaemonStub(), port=port, state_root=state_root)
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
