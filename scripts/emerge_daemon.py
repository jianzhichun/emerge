from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.pipeline_engine import PipelineEngine, PipelineMissingError  # noqa: E402
from scripts.policy_config import (  # noqa: E402
    atomic_write_json,
    derive_profile_token,
    derive_session_id,
    default_state_root,
    default_hook_state_root,
    load_json_object,
    session_idle_ttl_s,
)
from scripts.crystallizer import PipelineCrystallizer  # noqa: E402
from scripts.intent_registry import IntentRegistry  # noqa: E402
from scripts.mcp.span_handler import CompositeBridgeUnavailable, SpanHandlers  # noqa: E402
from scripts.runner_client import RunnerRouter  # noqa: E402
from scripts.exec_session import ExecSession  # noqa: E402


class EmergeDaemon:
    _SERVER_MAX_PROTOCOL_VERSION = "2025-11-25"

    def __init__(self, root: Path | None = None) -> None:
        resolved_root = root or ROOT
        state_root = Path(
            os.environ.get("EMERGE_STATE_ROOT") or str(default_state_root())
        ).expanduser().resolve()
        # Session ID is derived from the active project directory (CWD when CC starts
        # the daemon), not from the plugin installation directory. This matches the
        # derivation used by repl_admin and PostToolUse hooks.
        _project_root = Path(os.environ.get("EMERGE_PROJECT_ROOT", "")).resolve() if os.environ.get("EMERGE_PROJECT_ROOT") else Path.cwd()
        self._base_session_id = derive_session_id(
            os.environ.get("EMERGE_SESSION_ID"),
            _project_root,
        )
        self._state_root = state_root
        self._sessions_by_profile: dict[str, ExecSession] = {}
        self.pipeline = PipelineEngine(root=resolved_root)
        self._root = resolved_root
        self._script_roots = self._resolve_script_roots()
        # Coarse lock protecting concurrent read-modify-write operations on
        # candidates.json and state/registry/intents.json. Always held for the
        # full load→mutate→save cycle to prevent lost updates.
        self._registry_lock = threading.Lock()
        # Cache for RunnerRouter — rebuilt only when runner-map.json changes on disk.
        # Preserves the original "pick up config added after start" guarantee via mtime check.
        self._runner_router_cache: "RunnerRouter | None" = RunnerRouter.from_env()
        self._runner_router_config_mtime: float = self._read_runner_config_mtime()
        from scripts.policy_config import load_settings, default_emerge_home
        from scripts.metrics import get_sink
        try:
            _settings = load_settings()
        except Exception:
            _settings = {}
        _default_metrics_path = default_emerge_home() / "metrics.jsonl"
        self._sink = get_sink(_settings, default_path=_default_metrics_path)
        try:
            _plugin_manifest = resolved_root / ".claude-plugin" / "plugin.json"
            self._version = json.loads(_plugin_manifest.read_text(encoding="utf-8")).get("version", "0.0.0")
        except Exception:
            self._version = "0.0.0"
        self._operator_monitor: Any | None = None
        self._event_router = None
        from scripts.policy_engine import PolicyEngine
        from scripts.span_tracker import SpanTracker
        from scripts.mcp.flywheel_recorder import FlywheelRecorder
        _hook_state_root = Path(default_hook_state_root())
        # Single PolicyEngine instance shared across all evidence producers.
        # This is the *only* writer for state/registry/intents.json stage
        # fields — span close
        # (SpanTracker), icc_exec/pipeline events (FlywheelRecorder), and
        # icc_reconcile all flow through this one engine.
        self._policy_engine = PolicyEngine(
            state_root=lambda: self._state_root,
            lock=self._registry_lock,
            sink=lambda: self._sink,
            auto_crystallize=lambda **kw: self._auto_crystallize(**kw),
            has_synthesizable_wal=lambda sig, tp: self._flywheel.has_synthesizable_wal_entry(sig, tp),
            write_mcp_push=lambda _p: None,
            session_id=lambda: self._base_session_id,
        )
        self._span_tracker = SpanTracker(
            state_root=self._state_root,
            hook_state_root=_hook_state_root,
            policy_engine=self._policy_engine,
        )
        self._open_spans: dict[str, Any] = {}  # span_id → SpanRecord; in-process cache
        self._intent_gate: set[str] = self._load_intent_gate()  # intents confirmed as genuinely new
        self._flywheel = FlywheelRecorder(
            state_root=lambda: self._state_root,
            session_id=lambda: self._base_session_id,
            registry_lock=self._registry_lock,
            sink=lambda: self._sink,
            pipeline=lambda: self.pipeline,
            write_mcp_push=lambda _p: None,
            auto_crystallize=lambda **kw: self._auto_crystallize(**kw),
            policy_engine=self._policy_engine,
        )
        from scripts.mcp.resources import McpResourceHandler
        self._resource_handler = McpResourceHandler(
            state_root=lambda: self._state_root,
            pipeline=lambda: self.pipeline,
            span_tracker=self._span_tracker,
            hook_state_path=self._hook_state_path,
        )
        self._span_handlers = SpanHandlers(
            span_tracker=self._span_tracker,
            open_spans=self._open_spans,
            intent_gate=self._intent_gate,
            save_intent_gate=self._save_intent_gate,
            generate_skeleton=lambda **kw: self._generate_span_skeleton(**kw),
            sink=lambda: self._sink,
            run_pipeline=self._span_run_pipeline,
            record_pipeline_event=self._flywheel.record_pipeline_event,
            record_bridge_outcome=self._policy_engine.record_bridge_outcome,
            emit_cockpit_action=self._emit_crystallize_cockpit_action,
            tool_error=self._tool_error,
            tool_ok_json=self._tool_ok_json,
        )
        from scripts.mcp.bridge import FlywheelBridge
        self._bridge = FlywheelBridge(
            state_root=lambda: self._state_root,
            get_runner_router=lambda: self._get_runner_router(),
            run_remotely=lambda mode, args, client: self._run_pipeline_remotely(mode, args, client),
            run_local_read=lambda args: self.pipeline.run_read(args),
            run_local_write=lambda args: self.pipeline.run_write(args),
            run_local_workflow=lambda args: self.pipeline.run_workflow(args),
            record_bridge_outcome=self._policy_engine.record_bridge_outcome,
            sink_emit=lambda name, payload: self._sink.emit(name, payload),
        )
        # Wire dispatch through daemon so _try_flywheel_bridge remains the
        # canonical entry point for both composite children and icc_exec.
        self._bridge._child_dispatch = lambda args: self._try_flywheel_bridge(args)
        from scripts.mcp.tool_handlers import ToolHandlers
        self._tool_handlers = ToolHandlers(
            bridge=self._bridge,
            flywheel=self._flywheel,
            policy_engine=self._policy_engine,
            crystallize_fn=self._crystallize,
            get_session=self._get_session,
            resolve_exec_code=self._resolve_exec_code,
            get_runner_router=self._get_runner_router,
            run_connector_pipeline=self._run_connector_pipeline,
            run_pipeline_remotely=self._run_pipeline_remotely,
            state_root=lambda: self._state_root,
            write_operator_event=self._write_operator_event,
            append_warning_text=self._append_warning_text,
            get_http_server=lambda: getattr(self, "_http_server", None),
            sink_emit=lambda name, payload: self._sink.emit(name, payload),
            tool_error=self._tool_error,
            tool_ok_json=self._tool_ok_json,
        )
        # Route icc_exec bridge through daemon so _try_flywheel_bridge remains
        # the canonical entry point (monkey-patches in tests propagate here).
        self._tool_handlers._try_bridge_fn = lambda args: self._try_flywheel_bridge(args)

    def _cockpit_broadcast(self, event: dict) -> None:
        """Forward event to cockpit SSE clients (no-op if not in HTTP mode)."""
        http_srv = getattr(self, "_http_server", None)
        if http_srv is None:
            return
        http_srv._notify_cockpit_broadcast(event)

    def _hook_state_path(self) -> Path:
        return Path(default_hook_state_root()) / "state.json"

    def _intent_gate_path(self) -> Path:
        return self._state_root / "intent-gate.json"

    def _load_intent_gate(self) -> set[str]:
        try:
            data = json.loads(self._intent_gate_path().read_text(encoding="utf-8"))
            if isinstance(data, list):
                return set(str(x) for x in data)
        except Exception:
            pass
        return set()

    def _save_intent_gate(self) -> None:
        from scripts.policy_config import atomic_write_json
        try:
            atomic_write_json(self._intent_gate_path(), sorted(self._intent_gate))
        except Exception:
            pass

    def _read_runner_config_mtime(self) -> float:
        """Return mtime of runner-map.json, or 0.0 if file doesn't exist."""
        try:
            p = RunnerRouter.persisted_config_path()
            return p.stat().st_mtime if p.exists() else 0.0
        except Exception:
            return 0.0

    def _get_runner_router(self) -> "RunnerRouter | None":
        """Return cached RunnerRouter, rebuilding only when runner-map.json changes.

        Original contract preserved: config added after daemon start is picked up
        via mtime-based invalidation — zero disk reads when config unchanged.
        """
        current_mtime = self._read_runner_config_mtime()
        if current_mtime != self._runner_router_config_mtime:
            self._runner_router_cache = RunnerRouter.from_env()
            self._runner_router_config_mtime = current_mtime
        return self._runner_router_cache

    def _span_run_pipeline(self, mode: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """Execute a pipeline for SpanHandlers and return (result_dict, execution_path).

        Exists separately from _run_connector_pipeline because the two have different
        calling contracts:
        - _span_run_pipeline: used by SpanHandlers; returns (result, path) tuple;
          does NOT record pipeline events (caller records them).
        - _run_connector_pipeline: used by icc_exec; records events internally;
          returns a full MCP response dict, not a bare result.
        Do NOT consolidate — different return types and recording semantics are
        load-bearing for the flywheel bridge and span promotion paths.

        Composite intents (``composed_from`` non-empty) have no standalone pipeline
        module — the span bridge must use the same flywheel path as ``icc_exec``:
        ``_try_flywheel_bridge`` → ``_run_composite_bridge``.
        """
        intent_sig = str(arguments.get("intent_signature", "")).strip()
        if intent_sig:
            entry = IntentRegistry.get(self._state_root, intent_sig)
            if isinstance(entry, dict) and (entry.get("composed_from") or []):
                br = self._try_flywheel_bridge({**arguments, "intent_signature": intent_sig})
                if br is not None:
                    return br, "composite"
                raise CompositeBridgeUnavailable()
        _rr = self._get_runner_router()
        _client = _rr.find_client(arguments) if _rr else None
        if _client is not None:
            return self._run_pipeline_remotely(mode, arguments, _client), "remote"
        if mode == "write":
            return self.pipeline.run_write(arguments), "local"
        return self.pipeline.run_read(arguments), "local"

    # -- bridge failure classification shims (delegate to FlywheelBridge) --

    @staticmethod
    def _classify_bridge_failure(
        result: Any, mode: str, has_non_empty_baseline: bool,
        row_keys_sample: "frozenset[str] | None" = None,
    ) -> "dict[str, str] | None":
        from scripts.mcp.bridge import FlywheelBridge
        return FlywheelBridge._classify_bridge_failure(
            result, mode, has_non_empty_baseline, row_keys_sample,
        )

    @staticmethod
    def _classify_bridge_success_non_empty(result: Any, mode: str) -> "bool | None":
        from scripts.mcp.bridge import FlywheelBridge
        return FlywheelBridge._classify_bridge_success_non_empty(result, mode)

    @staticmethod
    def _extract_row_keys_sample(result: Any, mode: str) -> "frozenset[str] | None":
        from scripts.mcp.bridge import FlywheelBridge
        return FlywheelBridge._extract_row_keys_sample(result, mode)

    def _try_flywheel_bridge(self, arguments: dict[str, Any]) -> dict[str, Any] | None:
        return self._bridge.try_bridge(arguments)

    def _run_composite_bridge(
        self, composite_id: str, children: list[str], arguments: dict[str, Any],
    ) -> dict[str, Any] | None:
        return self._bridge._run_composite_bridge(composite_id, children, arguments)

    @property
    def _last_bridge_failure(self) -> "dict[str, Any] | None":
        """Proxy to FlywheelBridge.last_failure for test and backward compatibility."""
        return self._bridge.last_failure

    @_last_bridge_failure.setter
    def _last_bridge_failure(self, value: "dict[str, Any] | None") -> None:
        self._bridge.last_failure = value

    def _emit_crystallize_cockpit_action(self, action: dict) -> None:
        """Append a crystallize.to-yaml cockpit_action event to events.jsonl."""
        try:
            events_path = self._state_root / "events" / "events.jsonl"
            events_path.parent.mkdir(parents=True, exist_ok=True)
            event = {
                "type": "cockpit_action",
                "ts_ms": int(time.time() * 1000),
                "actions": [action],
            }
            with events_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception:
            pass  # non-fatal — never break span_close

    def _crystallize(self, **kwargs: Any) -> dict[str, Any]:
        result = PipelineCrystallizer(self._state_root).crystallize(**kwargs)
        self.pipeline.invalidate_cache(
            connector=str(kwargs.get("connector", "")).strip() or None,
            mode=str(kwargs.get("mode", "")).strip() or None,
            pipeline=str(kwargs.get("pipeline_name", "")).strip() or None,
        )
        return result

    def _auto_crystallize(self, **kwargs: Any) -> None:
        PipelineCrystallizer(self._state_root).auto_crystallize(**kwargs)
        self.pipeline.invalidate_cache(
            connector=str(kwargs.get("connector", "")).strip() or None,
            mode=str(kwargs.get("mode", "")).strip() or None,
            pipeline=str(kwargs.get("pipeline_name", "")).strip() or None,
        )

    def _generate_span_skeleton(self, **kwargs: Any) -> "Path | None":
        return PipelineCrystallizer(self._state_root).generate_span_skeleton(**kwargs)

    @staticmethod
    def _tool_error(text: str) -> dict[str, Any]:
        return {"isError": True, "content": [{"type": "text", "text": text}]}

    @staticmethod
    def _tool_ok_json(payload: Any) -> dict[str, Any]:
        result: dict[str, Any] = {
            "isError": False,
            "content": [{"type": "text", "text": json.dumps(payload)}],
        }
        if isinstance(payload, dict):
            result["structuredContent"] = payload
        return result

    @staticmethod
    def _as_float(value: Any, default: float) -> float:
        try:
            return float(value)
        except Exception:
            return default

    @staticmethod
    def _as_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except Exception:
            return default

    def _run_connector_pipeline(
        self,
        *,
        tool_name: str,
        mode: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            _rr = self._get_runner_router()
            _client = _rr.find_client(arguments) if _rr else None
            if _client is not None:
                result = self._run_pipeline_remotely(mode, arguments, _client)
                execution_path = "remote"
            else:
                if mode == "read":
                    result = self.pipeline.run_read(arguments)
                elif mode == "workflow":
                    result = self.pipeline.run_workflow(arguments)
                else:
                    result = self.pipeline.run_write(arguments)
                execution_path = "local"
            response = self._tool_ok_json(result)
            try:
                self._flywheel.record_pipeline_event(
                    tool_name=tool_name,
                    arguments=arguments,
                    result=result,
                    is_error=False,
                    execution_path=execution_path,
                    mode=mode,
                )
            except Exception as exc:
                self._append_warning_text(response, f"policy bookkeeping failed: {exc}")
            return response
        except PipelineMissingError:
            connector = str(arguments.get("connector", ""))
            pipeline = str(arguments.get("pipeline", ""))
            hint = (
                f"no pipeline registered yet — use icc_exec with "
                f"intent_signature='{connector}.{mode}.{pipeline}' to explore"
            )
            _payload = {
                "pipeline_missing": True,
                "connector": connector,
                "pipeline": pipeline,
                "mode": mode,
                "fallback": "icc_exec",
                "fallback_hint": hint,
            }
            return {
                "isError": False,
                "structuredContent": _payload,
                "content": [{"type": "text", "text": f"Pipeline not found. {hint}"}],
            }
        except Exception as exc:
            try:
                self._flywheel.record_pipeline_event(
                    tool_name=tool_name,
                    arguments=arguments,
                    result={},
                    is_error=True,
                    error_text=str(exc),
                    execution_path="local",
                    mode=mode,
                )
            except Exception:
                pass
            return {
                "isError": True,
                "recovery_suggestion": "exec",
                "content": [{"type": "text", "text": f"{tool_name} failed: {exc}"}],
            }

    _TOOL_DISPATCH: dict[str, str] = {
        "icc_span_open":    "_handle_icc_span_open",
        "icc_span_close":   "_handle_icc_span_close",
        "icc_span_approve": "_handle_icc_span_approve",
        "icc_exec":         "_handle_icc_exec",
        "icc_crystallize":  "_handle_icc_crystallize",
        "icc_compose":      "_handle_icc_compose",
        "icc_reconcile":    "_handle_icc_reconcile",
        "icc_hub":          "_handle_icc_hub",
        "runner_notify":    "_handle_runner_notify",
    }

    _WRITE_TOOLS = frozenset({
        "icc_exec", "icc_span_open", "icc_span_close", "icc_span_approve",
        "icc_crystallize", "icc_compose", "icc_reconcile", "icc_hub",
    })

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handler_name = self._TOOL_DISPATCH.get(name)
        if handler_name is None:
            return self._tool_error(f"Unknown tool: {name}")
        result = getattr(self, handler_name)(arguments)
        if name in self._WRITE_TOOLS:
            self._cockpit_broadcast({"data_updated": True})
        return result

    # ------------------------------------------------------------------
    # Per-tool handlers (one method per tool — no if/elif chain)
    # ------------------------------------------------------------------

    def _handle_icc_span_open(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._span_handlers.handle_span_open(arguments)

    def _handle_icc_span_close(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._span_handlers.handle_span_close(arguments)

    def _handle_icc_span_approve(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._span_handlers.handle_span_approve(arguments)

    def _handle_icc_exec(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._tool_handlers.handle_icc_exec(arguments)

    def _handle_icc_crystallize(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._tool_handlers.handle_icc_crystallize(arguments)

    def _handle_icc_compose(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._tool_handlers.handle_icc_compose(arguments)

    def _handle_icc_reconcile(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._tool_handlers.handle_icc_reconcile(arguments)

    def _handle_runner_notify(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._tool_handlers.handle_runner_notify(arguments)

    def _handle_icc_hub(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._tool_handlers.handle_icc_hub(arguments)

    # ------------------------------------------------------------------
    # JSON-RPC dispatch
    # ------------------------------------------------------------------

    def _jsonrpc_initialize(self, req_id: Any, params: dict) -> dict[str, Any]:
        client_version = str(params.get("protocolVersion", "") or "").strip()
        # Version negotiation: respond with min(client, server_max).
        # Versions are date-based (YYYY-MM-DD) — lexicographic comparison is correct.
        _server_max = self._SERVER_MAX_PROTOCOL_VERSION
        if client_version and client_version <= _server_max:
            negotiated_version = client_version
        elif client_version and client_version > _server_max:
            negotiated_version = _server_max
        else:
            negotiated_version = "2025-03-26"  # fallback when client omits version
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": negotiated_version,
                "capabilities": {
                    "tools": {},
                    "resources": {"subscribe": True},
                    "prompts": {},
                    "logging": {},
                },
                "serverInfo": {"name": "emerge", "version": self._version},
            },
        }

    def _jsonrpc_ack(self, req_id: Any, _params: dict) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    def _jsonrpc_tools_list(self, req_id: Any, _params: dict) -> dict[str, Any]:
        from scripts.mcp.schemas import get_tool_schemas
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": get_tool_schemas()}}

    def _jsonrpc_tools_call(self, req_id: Any, params: dict) -> dict[str, Any]:
        name = params.get("name", "")
        arguments = params.get("arguments", {}) or {}
        if not isinstance(arguments, dict):
            arguments = {}
        result = self.call_tool(name, arguments)
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def _jsonrpc_resources_list(self, req_id: Any, _params: dict) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "result": {"resources": self._list_resources()}}

    def _jsonrpc_resources_read(self, req_id: Any, params: dict) -> dict[str, Any]:
        uri = params.get("uri", "")
        try:
            resource = self._read_resource(uri)
            return {"jsonrpc": "2.0", "id": req_id, "result": {"resource": resource}}
        except KeyError as exc:
            return {"jsonrpc": "2.0", "id": req_id,
                    "error": {"code": -32602, "message": str(exc)}}
        except Exception as exc:
            return {"jsonrpc": "2.0", "id": req_id,
                    "error": {"code": -32603, "message": f"Resource read error: {exc}"}}

    def _jsonrpc_resources_templates_list(self, req_id: Any, _params: dict) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"resourceTemplates": self._resource_handler.list_resource_templates()},
        }

    def _jsonrpc_prompts_list(self, req_id: Any, _params: dict) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "result": {"prompts": self._PROMPTS}}

    def _jsonrpc_prompts_get(self, req_id: Any, params: dict) -> dict[str, Any]:
        pname = params.get("name", "")
        pargs = params.get("arguments") or {}
        try:
            prompt = self._get_prompt(pname, pargs)
            return {"jsonrpc": "2.0", "id": req_id, "result": prompt}
        except KeyError as exc:
            return {"jsonrpc": "2.0", "id": req_id,
                    "error": {"code": -32602, "message": str(exc)}}

    # Dispatch table — built lazily so methods are bound to the daemon instance.
    # Methods starting with "notifications/" return None (one-way, no response).
    _JSONRPC_DISPATCH_NAMES: dict[str, str] = {
        "initialize": "_jsonrpc_initialize",
        "ping": "_jsonrpc_ack",
        "logging/setLevel": "_jsonrpc_ack",
        "tools/list": "_jsonrpc_tools_list",
        "tools/call": "_jsonrpc_tools_call",
        "resources/list": "_jsonrpc_resources_list",
        "resources/read": "_jsonrpc_resources_read",
        "resources/templates/list": "_jsonrpc_resources_templates_list",
        "prompts/list": "_jsonrpc_prompts_list",
        "prompts/get": "_jsonrpc_prompts_get",
    }

    def handle_jsonrpc(self, request: dict[str, Any]) -> dict[str, Any] | None:
        req_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params") or {}
        if not isinstance(params, dict):
            params = {}

        # MCP notifications are one-way — no response.
        if method.startswith("notifications/"):
            return None

        handler_name = self._JSONRPC_DISPATCH_NAMES.get(method)
        if handler_name is None:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }
        return getattr(self, handler_name)(req_id, params)

    def _list_resources(self) -> list[dict[str, Any]]:
        return self._resource_handler.list_resources()

    def _read_resource(self, uri: str) -> dict[str, Any]:
        return self._resource_handler.read_resource(uri)

    def _get_connector_intents(self, connector: str) -> dict[str, Any]:
        return self._resource_handler.get_connector_intents(connector)

    def _build_intents_section(self, connector: str) -> str:
        return self._resource_handler.build_intents_section(connector)

    @property
    def _PROMPTS(self) -> list[dict[str, Any]]:
        return self._resource_handler._PROMPTS

    def _get_prompt(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._resource_handler.get_prompt(name, arguments)

    def _get_session(self, target_profile: str) -> ExecSession:
        normalized = (target_profile or "default").strip() or "default"
        profile_key = "__default__" if normalized == "default" else derive_profile_token(normalized)
        # Evict idle sessions before handing back an instance. Eviction only
        # drops the in-memory handle; WAL + checkpoint on disk survive so the
        # next call for the same profile rehydrates the Python namespace.
        self._evict_idle_sessions()
        if profile_key not in self._sessions_by_profile:
            if normalized == "default":
                session_id = self._base_session_id
            else:
                session_id = f"{self._base_session_id}__{profile_key}"
            self._sessions_by_profile[profile_key] = ExecSession(
                state_root=self._state_root, session_id=session_id
            )
        return self._sessions_by_profile[profile_key]

    def _evict_idle_sessions(self, *, now_ms: int | None = None) -> list[str]:
        """Drop cached ExecSessions inactive longer than ``session_idle_ttl_s``.

        Returns the list of evicted profile keys so callers/tests can assert on
        eviction behaviour. Poisoned sessions are kept in-cache so callers see
        the explicit error message until the background thread exits.
        """
        ttl_s = session_idle_ttl_s()
        if ttl_s <= 0:
            return []
        current_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        ttl_ms = ttl_s * 1000
        evicted: list[str] = []
        for key, sess in list(self._sessions_by_profile.items()):
            if sess._poisoned_thread is not None:  # noqa: SLF001
                continue
            last = sess.last_active_at_ms
            if last and (current_ms - last) > ttl_ms:
                del self._sessions_by_profile[key]
                evicted.append(key)
        return evicted

    def _resolve_exec_code(self, mode: str, arguments: dict[str, Any]) -> str:
        if mode == "script_ref":
            ref = str(arguments.get("script_ref", "")).strip()
            if not ref:
                raise ValueError("script_ref is required when mode=script_ref")
            script_path = Path(ref)
            if not script_path.is_absolute():
                script_path = (self._root / script_path).resolve()
            else:
                script_path = script_path.resolve()
            if not self._is_allowed_script_path(script_path):
                raise PermissionError(
                    f"script_ref path is outside allowed roots: {script_path}"
                )
            return script_path.read_text(encoding="utf-8")
        return str(arguments.get("code", ""))

    def _run_pipeline_remotely(self, mode: str, arguments: dict[str, Any], client: Any) -> dict[str, Any]:
        from scripts.mcp.remote_executor import run_pipeline_remotely
        return run_pipeline_remotely(self.pipeline, client, mode, arguments)

    def _write_operator_event(self, intent_signature: str, *, is_error: bool) -> None:
        """Write a cc_executed event to the local EventBus.

        Uses session_role='monitor_sub' so PatternDetector filters it out,
        preventing AI self-monitoring loops. The event exists for audit purposes
        and to close the observability gap: humans see CC takeovers in the event log.
        """
        try:
            import socket as _socket
            machine_id = _socket.gethostname()
            event_dir = Path.home() / ".emerge" / "operator-events" / machine_id
            event_dir.mkdir(parents=True, exist_ok=True)
            event_path = event_dir / "events.jsonl"
            event = {
                "event_type": "cc_executed",
                "session_role": "monitor_sub",
                "intent_signature": intent_signature,
                "status": "error" if is_error else "ok",
                "ts_ms": int(time.time() * 1000),
                "machine_id": machine_id,
            }
            with event_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=True) + "\n")
        except Exception:
            pass  # non-fatal — EventBus write must never break execution

    def _resolve_script_roots(self) -> list[Path]:
        raw = os.environ.get("EMERGE_SCRIPT_ROOTS", "").strip()
        if raw:
            roots = [Path(p).expanduser().resolve() for p in raw.split(",") if p.strip()]
        else:
            roots = [
                (self._root / "connectors").resolve(),
                (Path.home() / ".emerge" / "assets").resolve(),
            ]
        return roots

    def _is_allowed_script_path(self, path: Path) -> bool:
        resolved = path.resolve()
        for root in self._script_roots:
            try:
                resolved.relative_to(root.resolve())
                return True
            except ValueError:
                continue
        return False

    @staticmethod
    def _append_warning_text(result: dict[str, Any], warning: str) -> None:
        # Append as a separate content item so existing JSON text fields are not corrupted.
        # content[0]["text"] may already be json.dumps(...); appending plaintext there breaks
        # json.loads() for any downstream consumer.
        content = result.get("content")
        if isinstance(content, list):
            content.append({"type": "text", "text": f"warning:\n{warning}"})
            return
        result["content"] = [{"type": "text", "text": f"warning:\n{warning}"}]

    # ------------------------------------------------------------------
    # OperatorMonitor lifecycle
    # ------------------------------------------------------------------

    def start_operator_monitor(self) -> None:
        """Start OperatorMonitor if runner is configured or EMERGE_OPERATOR_MONITOR=1."""
        _rr = self._get_runner_router()
        if os.environ.get("EMERGE_OPERATOR_MONITOR", "0") != "1" and _rr is None:
            return
        if self._operator_monitor is not None and self._operator_monitor.is_alive():
            return
        from scripts.operator_monitor import OperatorMonitor

        poll_s = float(os.environ.get("EMERGE_MONITOR_POLL_S", "5"))

        self._operator_monitor = OperatorMonitor(
            machines={},
            poll_interval_s=poll_s,
            event_root=Path.home() / ".emerge" / "operator-events",
            state_root=self._state_root,
        )
        self._operator_monitor.start()

    def stop_operator_monitor(self) -> None:
        if self._operator_monitor is not None:
            self._operator_monitor.stop()

    def start_event_router(self) -> None:
        """Start EventRouter to watch local operator events."""
        from scripts.event_router import EventRouter
        from pathlib import Path as _Path

        handlers: dict = {}
        # Register local operator-events handler when OperatorMonitor is active.
        # The handler delegates to OperatorMonitor.process_local_file() which owns the
        # PatternDetector + event buffer state.
        if self._operator_monitor is not None:
            event_root = _Path.home() / ".emerge" / "operator-events"
            handlers[event_root] = lambda p: self._on_local_event_file(p)

        self._event_router = EventRouter(handlers)
        self._event_router.start()

    def stop_event_router(self) -> None:
        if self._event_router is not None:
            self._event_router.stop()

    def _on_local_event_file(self, path) -> None:
        """Called by EventRouter when an operator events.jsonl file changes.

        Delegates to OperatorMonitor.process_local_file() which owns the
        PatternDetector + sliding window buffer state for local machines.
        Only registered when EMERGE_OPERATOR_MONITOR=1.
        """
        if self._operator_monitor is None:
            return
        if path.name != "events.jsonl":
            return
        try:
            self._operator_monitor.process_local_file(path)
        except Exception:
            pass

def run_http(port: int = 8789, bind_host: str | None = None) -> None:
    """Start emerge daemon in HTTP MCP server mode with in-process cockpit."""
    import atexit
    import threading as _threading
    from scripts.daemon_http import DaemonHTTPServer

    daemon = EmergeDaemon()
    daemon.start_operator_monitor()
    daemon.start_event_router()
    atexit.register(daemon.stop_operator_monitor)
    atexit.register(daemon.stop_event_router)

    pid_path = Path.home() / ".emerge" / "daemon.pid"
    srv = DaemonHTTPServer(
        daemon=daemon, port=port, pid_path=pid_path, bind_host=bind_host,
        state_root=daemon._state_root,
    )
    daemon._http_server = srv
    srv.start()
    print(
        f"Emerge daemon HTTP server running on {srv.bind_host}:{srv.port}",
        flush=True,
    )
    _ui_host = "127.0.0.1" if srv.bind_host in ("0.0.0.0", "::") else srv.bind_host
    print(
        f"[emerge] Cockpit: http://{_ui_host}:{srv.port}/ (same port as MCP)",
        flush=True,
    )

    try:
        _threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        srv.stop()


if __name__ == "__main__":
    import argparse as _ap
    _p = _ap.ArgumentParser()
    _p.add_argument("--http", action="store_true", help="Run as HTTP MCP server")
    _p.add_argument("--port", type=int, default=8789)
    _p.add_argument(
        "--bind",
        type=str,
        default=None,
        metavar="ADDR",
        help="Bind address for HTTP MCP (overrides EMERGE_DAEMON_BIND; default 0.0.0.0)",
    )
    _p.add_argument("--ensure-running", action="store_true",
                    help="Launch daemon if not already running, then exit")
    _args = _p.parse_args()
    if _args.ensure_running:
        from scripts.daemon_http import ensure_running_or_launch
        result = ensure_running_or_launch(
            pid_path=None,
            port=_args.port,
            daemon_factory=None,  # detection-only
        )
        if result == "already_running":
            print("already_running")
        else:
            # Not running — start HTTP daemon (blocks until killed)
            run_http(port=_args.port, bind_host=_args.bind)
    elif _args.http:
        run_http(port=_args.port, bind_host=_args.bind)
    else:
        run_http(port=_args.port, bind_host=_args.bind)
