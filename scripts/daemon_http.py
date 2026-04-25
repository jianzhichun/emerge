from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import threading
import time
import uuid
from collections import OrderedDict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from scripts.policy_config import events_root
from scripts.event_appender import EventAppender
from scripts.runner_state_service import RunnerStateService
from scripts.sse_hub import SSEHub
from scripts.distiller import Distiller

_KEEPALIVE_INTERVAL_S = 20.0
_RUNNER_DISCONNECT_GRACE_MS = 45_000


class _LRUSet:
    """Bounded-size message-id dedup set with LRU eviction."""

    def __init__(self, values: list[str] | None = None, *, maxsize: int = 100_000) -> None:
        self._d: OrderedDict[str, None] = OrderedDict()
        self._maxsize = max(1, int(maxsize))
        for value in values or []:
            self.add(value)

    def __contains__(self, key: str) -> bool:
        if key in self._d:
            self._d.move_to_end(key)
            return True
        return False

    def __len__(self) -> int:
        return len(self._d)

    def add(self, key: str) -> None:
        if key in self._d:
            self._d.move_to_end(key)
            return
        self._d[key] = None
        if len(self._d) > self._maxsize:
            self._d.popitem(last=False)


def _parse_multipart(content_type: str, body: bytes) -> dict:
    """Parse multipart/form-data. Returns {field_name: (data, filename, mime)}.

    Uses get_filename() (not get_param) so RFC 2231 encoded names (e.g. UTF-8
    Chinese filenames) are decoded correctly instead of returned as raw encoded strings.
    """
    import email as _email
    import email.policy as _ep
    raw = f"MIME-Version: 1.0\r\nContent-Type: {content_type}\r\n\r\n".encode() + body
    msg = _email.message_from_bytes(raw, policy=_ep.compat32)
    parts: dict = {}
    payload = msg.get_payload()
    if not isinstance(payload, list):
        return parts
    for part in payload:
        if not hasattr(part, "get_param"):
            continue
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename() or part.get_param("filename", header="content-disposition")
        data = part.get_payload(decode=True) or b""
        if name:
            parts[name] = (data, filename, part.get_content_type())
    return parts


def _validate_machine_id(machine_id: str) -> None:
    """Reject machine_id values that could escape the event root via path traversal."""
    if not machine_id or machine_id != machine_id.strip():
        raise ValueError("machine_id is required and must not have leading/trailing whitespace")
    p = Path(machine_id)
    if p.name != machine_id or ".." in machine_id or "/" in machine_id or "\\" in machine_id:
        raise ValueError(f"Invalid machine_id: {machine_id!r}")


def resolve_daemon_bind(override: str | None = None) -> str:
    """Resolve bind address for ThreadingHTTPServer (CLI override, else EMERGE_DAEMON_BIND)."""
    raw = override if override is not None else os.environ.get("EMERGE_DAEMON_BIND", "0.0.0.0")
    raw = (raw or "").strip()
    if not raw:
        raw = "0.0.0.0"
    try:
        ipaddress.ip_address(raw)
    except ValueError as e:
        raise ValueError(
            "EMERGE_DAEMON_BIND / --bind must be a valid IP address "
            f"(e.g. 127.0.0.1 or 0.0.0.0); got {raw!r}"
        ) from e
    return raw


def _runtime_fingerprint() -> str:
    """Return plugin version as the restart signal — daemon restarts when version bumps."""
    root = Path(__file__).resolve().parents[1]
    plugin_json = root / ".claude-plugin" / "plugin.json"
    try:
        import json as _json
        return _json.loads(plugin_json.read_text(encoding="utf-8")).get("version", "unknown")
    except OSError:
        return "unknown"


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
        self._state_root = state_root or (Path.home() / ".emerge" / "state")
        try:
            from scripts.watcher_profiles import materialize_active_profiles

            materialize_active_profiles(self._state_root)
        except Exception:
            pass
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        # Runner monitor metadata is managed by an extracted service.
        self._runner_state = RunnerStateService()
        # Backward-compatibility aliases for existing tests/admin code paths.
        self._connected_runners = self._runner_state.runners
        self._runners_lock = self._runner_state.lock
        # runner_profile → {"wfile": ..., "lock": Lock()} for SSE command push.
        # Per-entry lock ensures concurrent runner_notify calls never interleave writes.
        self._runner_sse_clients: dict[str, Any] = {}
        self._runner_clients_lock = threading.Lock()
        # popup_id → threading.Event
        self._popup_futures: dict[str, threading.Event] = {}
        # Timestamp of the last POST /mcp request (CC tool call)
        self._last_mcp_ts: float = 0.0
        self._popup_results: dict[str, dict] = {}
        self._popup_lock = threading.Lock()
        # Pattern detection: per-runner sliding-window event buffers
        from scripts.pattern_detector import PatternDetector as _PatternDetector
        from scripts.orchestrator.suggestion_aggregator import SuggestionAggregator
        self._detector = _PatternDetector()
        self._runner_event_buffers: dict[str, deque] = {}
        self._runner_buffers_lock = threading.Lock()
        self._runner_seen_message_ids = _LRUSet(self._load_seen_message_ids())
        self._runner_seen_lock = threading.Lock()
        self._suggestion_aggregator = SuggestionAggregator(
            state_root=self._state_root,
            emit_cockpit_action=self._emit_cockpit_action,
        )
        self._synthesis_agent = self._make_synthesis_agent()
        # Cockpit UI + /api/* when served on the same port as MCP (see InProcessCockpitBridge)
        self._cockpit_sse_clients: list[Any] = []
        self._cockpit_sse_lock = threading.Lock()
        self._cockpit_sse_hub = SSEHub(queue_size=64)
        self._cockpit_injected_html: dict[str, Any] = {}
        self._cockpit_inject_lock = threading.Lock()
        self._event_appender = EventAppender(flush_interval_s=0.05, batch_size=64)
        self._runner_sse_hub = SSEHub(queue_size=64)
        self._request_count = 0
        self._request_error_count = 0

    def _make_synthesis_agent(self):
        if not hasattr(self._daemon, "call_tool"):
            return None
        try:
            from scripts.synthesis_agent import SynthesisAgent
            return SynthesisAgent(
                state_root=self._state_root,
                exec_tool=lambda args: self._daemon.call_tool("icc_exec", args),
            )
        except Exception:
            return None

    def _emit_cockpit_action(self, action: dict) -> None:
        try:
            if hasattr(self._daemon, "_emit_crystallize_cockpit_action"):
                self._daemon._emit_crystallize_cockpit_action(action)
        except Exception:
            pass

    def _seen_message_ids_path(self) -> Path:
        return events_root(self._state_root) / "runner-message-ids.jsonl"

    def _load_seen_message_ids(self) -> list[str]:
        path = self._seen_message_ids_path()
        if not path.exists():
            return []
        seen: list[str] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    seen.append(line)
        except OSError:
            return []
        return seen[-10000:]

    def _remember_message_id(self, message_id: str) -> None:
        if not message_id:
            return
        try:
            path = self._seen_message_ids_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(message_id + "\n")
        except OSError:
            pass

    def cockpit_broadcast(self, event: dict) -> None:
        """Push SSE event to cockpit browsers connected to /api/sse/status."""
        data = f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode()
        self._cockpit_sse_hub.broadcast(data)

    def register_cockpit_sse_client(self, client_id: str, wfile: Any) -> None:
        self._cockpit_sse_hub.register(client_id, wfile)

    def unregister_cockpit_sse_client(self, client_id: str) -> None:
        self._cockpit_sse_hub.unregister(client_id)

    def connected_runners_snapshot(self) -> list[dict]:
        return self._runner_state.snapshot()

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
        self._event_appender.stop()
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
        self._runner_state.on_online(runner_profile, machine_id, now_ms)
        logging.warning("runner online: profile=%s machine_id=%s", runner_profile, machine_id)
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
        orig_type = str(payload.get("type", "")).strip()
        message_id = str(payload.get("message_id", "")).strip()
        first_seen = True
        if message_id:
            with self._runner_seen_lock:
                if message_id in self._runner_seen_message_ids:
                    first_seen = False
                else:
                    self._runner_seen_message_ids.add(message_id)
                    self._remember_message_id(message_id)
        if machine_id:
            _validate_machine_id(machine_id)
            machine_dir = self._event_root / machine_id
            self._event_appender.append_wait(
                machine_dir / "events.jsonl",
                payload,
                ensure_ascii=False,
            )
        if runner_profile:
            import re as _re2
            if not _re2.fullmatch(r"[a-zA-Z0-9_.-]+", runner_profile) or len(runner_profile) > 64:
                runner_profile = ""  # invalid profile, skip per-runner event file
        if runner_profile:
            self._runner_state.on_event(runner_profile, ts_ms)
            preserved_types = {
                "operator_message",
                "runner_subagent_message",
                "pattern_suggestion",
                "evidence_report",
                "bridge_outcome_report",
            }
            _written_type = orig_type if orig_type in preserved_types else "runner_event"
            self._append_event(events_root(self._state_root) / f"events-{runner_profile}.jsonl", {
                "type": _written_type,
                "ts_ms": ts_ms,
                "runner_profile": runner_profile,
                **{k: v for k, v in payload.items()
                   if k not in ("runner_profile", "type")},
            })

        if first_seen:
            if orig_type == "evidence_report":
                self._apply_evidence_report(payload, runner_profile=runner_profile, ts_ms=ts_ms)
            elif orig_type == "bridge_outcome_report":
                self._apply_bridge_outcome_report(payload, ts_ms=ts_ms)
            elif orig_type in ("runner_subagent_message", "pattern_suggestion"):
                self._process_runner_suggestion(payload, runner_profile=runner_profile)

        # Pattern detection on runner push events (skip operator chat messages)
        skip_detector = {
            "operator_message",
            "runner_subagent_message",
            "pattern_suggestion",
            "evidence_report",
            "bridge_outcome_report",
        }
        if runner_profile and orig_type not in skip_detector:
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
                event_path = events_root(self._state_root) / f"events-{runner_profile}.jsonl"
                pending = {
                    "type": "pattern_pending_synthesis",
                    "ts_ms": ts_ms,
                    "runner_profile": runner_profile,
                    "intent_signature": Distiller._normalise(summary.intent_signature),
                    "source_intent_signature": summary.intent_signature,
                    "meta": {
                        "occurrences": summary.occurrences,
                        "window_minutes": round(summary.window_minutes, 1),
                        "machine_ids": summary.machine_ids,
                        "detector_signals": summary.detector_signals,
                    },
                }
                self._append_event(event_path, pending)
                agent = getattr(self, "_synthesis_agent", None)
                if agent is not None:
                    try:
                        agent.process_pattern(
                            summary=summary,
                            runner_profile=runner_profile,
                            events=snapshot,
                            event_path=event_path,
                        )
                    except Exception:
                        pass
                self._runner_state.set_alert(
                    runner_profile,
                    {
                        "stage": stage,
                        "intent_signature": summary.intent_signature,
                        "ts_ms": ts_ms,
                    },
                )

            # Single _write_monitor_state call captures both last_event_ts_ms and last_alert
            self._write_monitor_state()
            if summaries:
                self._notify_cockpit_broadcast({"monitors_updated": True})

    def _apply_evidence_report(self, payload: dict, *, runner_profile: str, ts_ms: int) -> None:
        sig = str(payload.get("intent_signature", "")).strip()
        if not sig:
            return
        try:
            self._daemon._policy_engine.apply_evidence(
                sig,
                success=bool(payload.get("success", False)),
                anchor_type=str(payload.get("anchor_type", "self_report") or "self_report"),
                evidence_unit_id=str(payload.get("evidence_unit_id") or payload.get("message_id") or ""),
                verify_observed=bool(payload.get("verify_observed", False)),
                verify_passed=bool(payload.get("verify_passed", False)),
                human_fix=bool(payload.get("human_fix", False)),
                is_degraded=bool(payload.get("is_degraded", False)),
                description=str(payload.get("description", "") or ""),
                is_read_only=payload.get("is_read_only") if payload.get("is_read_only") is not None else None,
                target_profile=str(payload.get("target_profile") or runner_profile or "default"),
                execution_path=str(payload.get("execution_path") or "runner"),
                policy_action=payload.get("policy_action"),
                policy_enforced=bool(payload.get("policy_enforced", False)),
                stop_triggered=bool(payload.get("stop_triggered", False)),
                rollback_executed=bool(payload.get("rollback_executed", False)),
                ts_ms=int(payload.get("ts_ms") or ts_ms),
            )
        except Exception:
            logging.exception("failed to apply runner evidence report")

    def _apply_bridge_outcome_report(self, payload: dict, *, ts_ms: int) -> None:
        sig = str(payload.get("intent_signature", "")).strip()
        if not sig:
            return
        try:
            row_keys = payload.get("row_keys_sample")
            self._daemon._policy_engine.record_bridge_outcome(
                sig,
                success=bool(payload.get("success", False)),
                reason=str(payload.get("reason", "") or ""),
                exception_class=str(payload.get("exception_class", "") or ""),
                demotion_reason=str(payload.get("demotion_reason", "bridge_broken") or "bridge_broken"),
                non_empty=payload.get("non_empty"),
                ts_ms=int(payload.get("ts_ms") or ts_ms),
                row_keys_sample=frozenset(row_keys) if isinstance(row_keys, list) else None,
            )
        except Exception:
            logging.exception("failed to apply runner bridge outcome report")

    def _process_runner_suggestion(self, payload: dict, *, runner_profile: str) -> None:
        suggestion = dict(payload.get("payload") or {}) if isinstance(payload.get("payload"), dict) else dict(payload)
        suggestion.setdefault("runner_profile", runner_profile or payload.get("runner_profile", ""))
        suggestion.setdefault("machine_id", payload.get("machine_id", ""))
        if payload.get("kind") and "kind" not in suggestion:
            suggestion["kind"] = payload.get("kind")
        try:
            self._suggestion_aggregator.on_suggestion(suggestion)
        except Exception:
            logging.exception("failed to aggregate runner suggestion")

    def _on_popup_result(self, payload: dict) -> None:
        popup_id = str(payload.get("popup_id", "")).strip()
        if not popup_id:
            return
        with self._popup_lock:
            if popup_id not in self._popup_futures:
                return  # already timed out — discard stale result to prevent leak
            self._popup_results[popup_id] = payload
            ev = self._popup_futures.get(popup_id)
        if ev:
            ev.set()

    def _sse_write(self, runner_profile: str, data: bytes) -> str:
        """Write bytes to runner SSE stream. Returns "" on success
        or an error string ("runner_not_connected" / "runner_disconnected")."""
        with self._runner_clients_lock:
            entry = self._runner_sse_clients.get(runner_profile)
        if entry is None:
            return "runner_not_connected"
        if self._runner_sse_hub.send(runner_profile, data):
            return ""
        else:
            with self._runner_clients_lock:
                cur = self._runner_sse_clients.get(runner_profile)
                if cur is not None and cur.get("connection_id") == entry.get("connection_id"):
                    self._runner_sse_clients.pop(runner_profile)
            self._runner_state.mark_sse_disconnected(
                runner_profile,
                int(time.time() * 1000),
                _RUNNER_DISCONNECT_GRACE_MS,
            )
            self._write_monitor_state()
            self._notify_cockpit_broadcast({"monitors_updated": True})
            logging.warning("runner sse write failed; marked disconnected: profile=%s", runner_profile)
            return "runner_disconnected"

    def request_popup(self, runner_profile: str, ui_spec: dict, timeout_s: float = 30.0) -> dict:
        """Send popup to runner via SSE, wait for result. Blocks calling thread."""
        if ui_spec.get("type") == "toast":
            # Fire-and-forget: popup_id="" sentinel — runner never posts back for toasts.
            command = json.dumps({"type": "notify", "popup_id": "", "ui_spec": ui_spec})
            err = self._sse_write(runner_profile, f"data: {command}\n\n".encode())
            if err:
                return {"ok": False, "error": err}
            return {"ok": True}
        popup_id = uuid.uuid4().hex
        ev = threading.Event()
        with self._popup_lock:
            self._popup_futures[popup_id] = ev
        # upload_url is intentionally NOT injected here; runner derives it from team_lead_url.
        command = json.dumps({"type": "notify", "popup_id": popup_id, "ui_spec": ui_spec})
        err = self._sse_write(runner_profile, f"data: {command}\n\n".encode())
        if err:
            with self._popup_lock:
                self._popup_futures.pop(popup_id, None)
            return {"ok": False, "error": err}
        # Give the runner enough time to show the popup even if it was queued behind
        # another in-flight popup (30 s buffer on top of the UI-level timeout).
        total_timeout = float(ui_spec.get("timeout_s", 30)) + 30.0
        fired = ev.wait(timeout=total_timeout)
        with self._popup_lock:
            self._popup_futures.pop(popup_id, None)
            result = self._popup_results.pop(popup_id, None)
        if not fired or result is None:
            return {"ok": False, "timed_out": True, "value": None}
        return {"ok": True, "value": result.get("value"), "attachments": result.get("attachments", []), "popup_id": popup_id}

    def _append_event(self, path: Path, event: dict) -> None:
        self._event_appender.append_wait(path, event, ensure_ascii=False)

    def _write_monitor_state(self) -> None:
        """Write current runner state to runner-monitor-state.json for cockpit."""
        path = events_root(self._state_root) / "runner-monitor-state.json"
        self._runner_state.write_monitor_state(path)


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
                    # SIGKILL not available on Windows; SIGTERM there calls
                    # TerminateProcess, so a second SIGTERM is the best we can do.
                    _kill = getattr(signal, "SIGKILL", signal.SIGTERM)
                    os.kill(pid, _kill)
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
            req_id = getattr(self, "_request_id", "")
            out = dict(payload)
            if req_id and "request_id" not in out:
                out["request_id"] = req_id
            if code >= 400:
                srv._request_error_count += 1
            body = json.dumps(out).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if req_id:
                self.send_header("X-Request-Id", req_id)
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
            self._request_id = uuid.uuid4().hex[:12]
            srv._request_count += 1
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
            elif path == "/health/deep":
                connected = srv._runner_state.counts()
                with srv._runner_clients_lock:
                    sse_registered = len(srv._runner_sse_clients)
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "metrics": {
                            "requests_total": srv._request_count,
                            "request_errors": srv._request_error_count,
                            "runner_connected": connected,
                            "runner_sse_registered": sse_registered,
                            "runner_sse_hub_clients": srv._runner_sse_hub.client_count(),
                            "cockpit_sse_hub_clients": srv._cockpit_sse_hub.client_count(),
                            "event_appender_queue_depth": srv._event_appender.queue_depth(),
                        },
                    },
                )
            elif path == "/runner/sse":
                import urllib.parse as _up2
                qs2 = _up2.parse_qs(_up2.urlparse(self.path).query)
                profile = qs2.get("runner_profile", [""])[0].strip()
                mid_sse = qs2.get("machine_id", [""])[0].strip()
                self._handle_runner_sse(profile, mid_sse)
            elif path == "/runner-dist/runner.zip":
                from scripts.admin.runner import _build_runner_zip
                _plugin_root = Path(__file__).resolve().parents[1]
                data = _build_runner_zip(_plugin_root)
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", 'attachment; filename="runner.zip"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
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
                req_host = self.headers.get("Host", "").strip()
                if req_host and not req_host.startswith(("0.0.0.0", "127.0.0.1")):
                    team_lead_url = f"http://{req_host}".rstrip("/")
                else:
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

        def _handle_runner_sse(self, runner_profile: str, machine_id: str = ""):
            import re as _re_sse
            if runner_profile and not _re_sse.fullmatch(r"[a-zA-Z0-9_.-]+", runner_profile):
                self._send_json(400, {"error": "invalid runner_profile"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            my_wfile = self.wfile
            connection_id = uuid.uuid4().hex[:8]
            my_entry: dict = {
                "connection_id": connection_id,
                "connected_at_ms": int(time.time() * 1000),
            }
            if runner_profile:
                now_ms = int(time.time() * 1000)
                with srv._runner_clients_lock:
                    prev_entry = srv._runner_sse_clients.get(runner_profile)
                    srv._runner_sse_clients[runner_profile] = my_entry
                    srv._runner_sse_hub.register(runner_profile, my_wfile)
                srv._runner_state.on_sse_connected(runner_profile, machine_id, now_ms)
                srv._write_monitor_state()
                srv._notify_cockpit_broadcast({"monitors_updated": True})
                if prev_entry is not None:
                    logging.warning(
                        "runner sse replace: profile=%s old_conn=%s new_conn=%s machine_id=%s",
                        runner_profile,
                        prev_entry.get("connection_id", "unknown"),
                        connection_id,
                        machine_id or runner_profile,
                    )
                else:
                    logging.warning(
                        "runner sse connect: profile=%s conn=%s machine_id=%s",
                        runner_profile,
                        connection_id,
                        machine_id or runner_profile,
                    )
            try:
                while True:
                    time.sleep(15)
                    my_wfile.write(b": keepalive\n\n")
                    my_wfile.flush()
            except OSError:
                pass
            finally:
                if runner_profile:
                    with srv._runner_clients_lock:
                        # Only evict if we're still the registered entry — a reconnect
                        # may have already replaced our entry before this finally runs.
                        cur = srv._runner_sse_clients.get(runner_profile)
                        if cur is my_entry:
                            srv._runner_sse_clients.pop(runner_profile)
                            srv._runner_sse_hub.unregister(runner_profile)
                            now_ms = int(time.time() * 1000)
                            removed, age_ms = srv._runner_state.mark_sse_disconnected(
                                runner_profile, now_ms, _RUNNER_DISCONNECT_GRACE_MS
                            )
                            logging.warning(
                                "runner sse disconnect: profile=%s conn=%s removed=%s last_seen_age_ms=%d",
                                runner_profile,
                                connection_id,
                                str(bool(removed)).lower(),
                                age_ms,
                            )
                            do_notify = True
                        else:
                            do_notify = False
                    if do_notify:
                        srv._write_monitor_state()
                        srv._notify_cockpit_broadcast({"monitors_updated": True})

        def do_POST(self):  # noqa: N802
            self._request_id = uuid.uuid4().hex[:12]
            srv._request_count += 1
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
            elif path == "/runner/upload":
                import mimetypes as _mt
                content_type = self.headers.get("Content-Type", "")
                if "multipart/form-data" not in content_type:
                    self._send_json(400, {"error": "multipart/form-data required"})
                    return
                parts = _parse_multipart(content_type, body)
                if "file" not in parts:
                    self._send_json(400, {"error": "no file provided"})
                    return
                file_data, filename, mime = parts["file"]
                max_bytes = int(os.environ.get("EMERGE_UPLOAD_MAX_BYTES", str(50 * 1024 * 1024)))
                if len(file_data) > max_bytes:
                    self._send_json(413, {"error": "file too large"})
                    return
                safe_name = Path(filename or "upload").name or "upload"
                file_id = str(uuid.uuid4())
                upload_dir = srv._state_root / "uploads" / file_id
                upload_dir.mkdir(parents=True, exist_ok=True)
                dest = upload_dir / safe_name
                dest.write_bytes(file_data)
                if not mime or mime == "application/octet-stream":
                    guessed, _ = _mt.guess_type(safe_name)
                    mime = guessed or "application/octet-stream"
                self._send_json(200, {"file_id": file_id, "path": dest.as_posix(), "mime": mime})
            else:
                self._send_json(404, {"ok": False, "error": "not_found"})

    return _Handler
