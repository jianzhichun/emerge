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
    default_exec_root,
    default_hook_state_root,
    load_json_object,
    pin_plugin_data_path_if_present,
)
from scripts.crystallizer import PipelineCrystallizer  # noqa: E402
from scripts.runner_client import RunnerRouter  # noqa: E402
from scripts.exec_session import ExecSession  # noqa: E402
from scripts.observer_plugin import AdapterRegistry  # noqa: E402
from scripts.admin.actions import ActionRegistry  # noqa: E402
_stdout_lock = threading.Lock()


class EmergeDaemon:
    _SERVER_MAX_PROTOCOL_VERSION = "2025-11-25"

    def __init__(self, root: Path | None = None) -> None:
        resolved_root = root or ROOT
        pin_plugin_data_path_if_present()
        state_root = Path(
            os.environ.get("EMERGE_STATE_ROOT") or str(default_exec_root())
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
        # candidates.json and pipelines-registry.json. Always held for the
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
        self._operator_monitor: "OperatorMonitor | None" = None
        self._event_router = None
        self._elicit_events: dict[str, threading.Event] = {}
        self._elicit_results: dict[str, dict] = {}
        from scripts.span_tracker import SpanTracker
        _hook_state_root = Path(default_hook_state_root())
        self._span_tracker = SpanTracker(
            state_root=self._state_root,
            hook_state_root=_hook_state_root,
        )
        self._open_spans: dict[str, Any] = {}  # span_id → SpanRecord; in-process cache
        self._intent_gate: set[str] = self._load_intent_gate()  # intents confirmed as genuinely new
        from scripts.mcp.flywheel_recorder import FlywheelRecorder
        self._flywheel = FlywheelRecorder(
            state_root=lambda: self._state_root,
            session_id=lambda: self._base_session_id,
            registry_lock=self._registry_lock,
            sink=lambda: self._sink,
            pipeline=lambda: self.pipeline,
            write_mcp_push=lambda p: self._write_mcp_push(p),
            auto_crystallize=lambda **kw: self._auto_crystallize(**kw),
        )
        from scripts.mcp.resources import McpResourceHandler
        self._resource_handler = McpResourceHandler(
            state_root=lambda: self._state_root,
            pipeline=lambda: self.pipeline,
            span_tracker=self._span_tracker,
            hook_state_path=self._hook_state_path,
        )
        from scripts.mcp.span_handler import SpanHandlers
        self._span_handlers = SpanHandlers(
            span_tracker=self._span_tracker,
            open_spans=self._open_spans,
            intent_gate=self._intent_gate,
            save_intent_gate=self._save_intent_gate,
            generate_skeleton=lambda **kw: self._generate_span_skeleton(**kw),
            sink=lambda: self._sink,
            run_pipeline=self._span_run_pipeline,
            record_pipeline_event=self._flywheel.record_pipeline_event,
            tool_error=self._tool_error,
            tool_ok_json=self._tool_ok_json,
            elicit=lambda *a, **kw: self._elicit(*a, **kw),
            is_http_mode=lambda: getattr(self, "_http_mode", False),
        )
        self._register_adapter_actions()

    def _cockpit_broadcast(self, event: dict) -> None:
        """Forward event to cockpit SSE clients (no-op if not in HTTP mode)."""
        http_srv = getattr(self, "_http_server", None)
        if http_srv is None:
            return
        http_srv._notify_cockpit_broadcast(event)

    def _register_adapter_actions(self) -> None:
        """Load adapters once at daemon boot and let them register custom action specs."""
        registry = AdapterRegistry()
        for item in registry.list_plugins():
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            plugin = registry.get_plugin(name)
            try:
                plugin.register_actions(ActionRegistry)
            except Exception:
                # Adapter extensions are optional and must not block daemon startup.
                continue

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
        """
        _rr = self._get_runner_router()
        _client = _rr.find_client(arguments) if _rr else None
        if _client is not None:
            return self._run_pipeline_remotely(mode, arguments, _client), "remote"
        if mode == "write":
            return self.pipeline.run_write(arguments), "local"
        return self.pipeline.run_read(arguments), "local"

    def _try_flywheel_bridge(self, arguments: dict[str, Any]) -> dict[str, Any] | None:
        base_pipeline_id = str(arguments.get("base_pipeline_id", "")).strip()
        # Fall back to intent_signature — with unified keys they are the same thing.
        # This means the bridge fires automatically once an intent reaches stable,
        # without requiring CC to explicitly pass base_pipeline_id.
        if not base_pipeline_id:
            base_pipeline_id = str(arguments.get("intent_signature", "")).strip()
        if not base_pipeline_id:
            return None

        # Key is the pipeline_id itself — no prefixes, no runner dimension
        key = base_pipeline_id
        pipelines_path = self._state_root / "pipelines-registry.json"
        pipelines_data = load_json_object(pipelines_path, root_key="pipelines")
        bridge_entry = pipelines_data.get("pipelines", {}).get(key)
        if not isinstance(bridge_entry, dict):
            return None
        if str(bridge_entry.get("status", "explore")) != "stable":
            return None

        parts = base_pipeline_id.split(".", 2)
        if len(parts) != 3:
            return None
        connector, mode, name = parts
        pipeline_args = {**arguments, "connector": connector, "pipeline": name}
        try:
            _rr = self._get_runner_router()
            _client = _rr.find_client(arguments) if _rr else None
            if _client is not None:
                result = self._run_pipeline_remotely(mode, pipeline_args, _client)
            elif mode == "write":
                result = self.pipeline.run_write(pipeline_args)
            else:
                result = self.pipeline.run_read(pipeline_args)
        except Exception as _bridge_exc:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "flywheel bridge failed for %s (%s), falling back to LLM: %s",
                base_pipeline_id, mode, _bridge_exc,
            )
            # Store failure info for icc_exec to inject into the response
            self._last_bridge_failure = {
                "pipeline_id": base_pipeline_id,
                "mode": mode,
                "reason": str(_bridge_exc),
            }
            # Increment consecutive_failures directly in the registry so the policy engine
            # can downgrade stable→explore if the bridge keeps failing, without polluting
            # the recent_outcomes window (which would cause spurious window-failure downgrades).
            try:
                _reg_path = self._state_root / "pipelines-registry.json"
                with self._registry_lock:
                    _reg = load_json_object(_reg_path, root_key="pipelines")
                    _pe = _reg["pipelines"].get(base_pipeline_id)
                    if isinstance(_pe, dict):
                        _pe["consecutive_failures"] = int(_pe.get("consecutive_failures", 0)) + 1
                        _reg["pipelines"][base_pipeline_id] = _pe
                        atomic_write_json(_reg_path, _reg)
            except Exception:
                pass
            return None
        result["bridge_promoted"] = True
        try:
            self._sink.emit("flywheel.bridge.promoted", {"pipeline_id": base_pipeline_id})
        except Exception:
            pass
        return result

    def _crystallize(self, **kwargs: Any) -> dict[str, Any]:
        return PipelineCrystallizer(self._state_root).crystallize(**kwargs)

    def _auto_crystallize(self, **kwargs: Any) -> None:
        PipelineCrystallizer(self._state_root).auto_crystallize(**kwargs)

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
                result = self.pipeline.run_read(arguments) if mode == "read" else self.pipeline.run_write(arguments)
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
        "icc_reconcile":    "_handle_icc_reconcile",
        "icc_hub":          "_handle_icc_hub",
        "runner_notify":    "_handle_runner_notify",
    }

    _WRITE_TOOLS = frozenset({
        "icc_exec", "icc_span_open", "icc_span_close", "icc_span_approve",
        "icc_crystallize", "icc_reconcile", "icc_hub",
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
        # Flywheel bridge: stable candidate → zero-LLM redirect
        promoted = self._try_flywheel_bridge(arguments)
        if promoted is not None:
            response = {"isError": False, "content": [{"type": "text", "text": json.dumps(promoted)}]}
            try:
                _rr = self._get_runner_router()
                _client = _rr.find_client(arguments) if _rr else None
                self._flywheel.record_pipeline_event(
                    tool_name="icc_exec",
                    arguments=arguments,
                    result=promoted,
                    is_error=False,
                    execution_path="remote" if _client is not None else "local",
                )
            except Exception:
                pass
            _bsig = str(arguments.get("intent_signature", "")).strip()
            if _bsig and not arguments.get("no_replay"):
                self._write_operator_event(_bsig, is_error=False)
            return response
        try:
            mode = str(arguments.get("mode", "inline_code"))
            target_profile = str(arguments.get("target_profile", "default"))
            candidate_key = self._flywheel.resolve_exec_candidate_key(
                arguments=arguments,
                target_profile=target_profile,
            )
            sampled_in_policy = self._flywheel.should_sample(candidate_key)
            _rr = self._get_runner_router()
            _exec_client = _rr.find_client(arguments) if _rr else None
            execution_path = "remote" if _exec_client is not None else "local"
            if _exec_client is not None:
                remote_args = dict(arguments)
                if mode == "script_ref":
                    remote_args["code"] = self._resolve_exec_code(mode=mode, arguments=arguments)
                    remote_args["mode"] = "inline_code"
                result = _exec_client.call_tool("icc_exec", remote_args)
            else:
                code = self._resolve_exec_code(mode=mode, arguments=arguments)
                repl = self._get_session(target_profile)
                result = repl.exec_code(
                    code,
                    metadata={
                        "mode": mode,
                        "target_profile": target_profile,
                        "intent_signature": arguments.get("intent_signature", ""),
                        "script_ref": arguments.get("script_ref", ""),
                        "no_replay": bool(arguments.get("no_replay", False)),
                    },
                    inject_vars={"__args": arguments.get("script_args", {})},
                    result_var=str(arguments.get("result_var", "")).strip() or None,
                )
            try:
                self._flywheel.record_exec_event(
                    arguments=arguments,
                    result=result,
                    target_profile=target_profile,
                    mode=mode,
                    execution_path=execution_path,
                    sampled_in_policy=sampled_in_policy,
                    candidate_key=candidate_key,
                )
            except Exception as exc:
                self._append_warning_text(result, f"policy bookkeeping failed: {exc}")
            _sig = str(arguments.get("intent_signature", ""))
            if _sig and not arguments.get("no_replay"):
                self._write_operator_event(_sig, is_error=bool(result.get("isError")))
            if "isError" not in result:
                result["isError"] = False
            _bf = getattr(self, "_last_bridge_failure", None)
            if _bf:
                self._last_bridge_failure = None
                self._append_warning_text(
                    result,
                    f"bridge fallback: {_bf['pipeline_id']} ({_bf['mode']}) failed: "
                    f"{_bf['reason']}. Falling back to LLM inference.",
                )
            return result
        except Exception as exc:
            return self._tool_error(f"icc_exec failed: {exc}")

    def _handle_icc_crystallize(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            intent_signature = str(arguments.get("intent_signature", "")).strip()
            connector = str(arguments.get("connector", "")).strip()
            pipeline_name = str(arguments.get("pipeline_name", "")).strip()
            mode = str(arguments.get("mode", "read")).strip()
            target_profile = str(arguments.get("target_profile", "default")).strip()
            if not all([intent_signature, connector, pipeline_name, mode]):
                return self._tool_error(
                    "icc_crystallize: intent_signature, connector, pipeline_name, and mode are required"
                )
            if mode not in ("read", "write"):
                return self._tool_error(
                    f"icc_crystallize: mode must be 'read' or 'write', got {mode!r}"
                )
            return self._crystallize(
                intent_signature=intent_signature,
                connector=connector,
                pipeline_name=pipeline_name,
                mode=mode,
                target_profile=target_profile,
            )
        except Exception as exc:
            return self._tool_error(f"icc_crystallize failed: {exc}")

    def _handle_icc_reconcile(self, arguments: dict[str, Any]) -> dict[str, Any]:
        delta_id = str(arguments.get("delta_id", "")).strip()
        outcome = str(arguments.get("outcome", "")).strip()
        intent_signature = str(arguments.get("intent_signature", "")).strip()
        if not delta_id:
            return self._tool_error("icc_reconcile: delta_id is required")
        if outcome not in ("confirm", "correct", "retract"):
            return self._tool_error(
                f"icc_reconcile: 'outcome' must be confirm/correct/retract, got {outcome!r}"
            )
        from scripts.state_tracker import load_tracker, save_tracker
        state_path = self._hook_state_path()
        tracker = load_tracker(state_path)
        tracker.reconcile_delta(delta_id, outcome)
        save_tracker(state_path, tracker)
        td = tracker.to_dict()
        if outcome == "correct" and intent_signature:
            self._flywheel.increment_human_fix(intent_signature)
        return self._tool_ok_json({
            "delta_id": delta_id,
            "outcome": outcome,
            "intent_signature": intent_signature or None,
            "verification_state": td.get("verification_state", "unverified"),
        })

    def _handle_runner_notify(self, arguments: dict[str, Any]) -> dict[str, Any]:
        runner_profile = str(arguments.get("runner_profile", "")).strip()
        ui_spec = arguments.get("ui_spec", {})
        if not runner_profile:
            return self._tool_error("runner_notify: runner_profile is required")
        if not isinstance(ui_spec, dict):
            return self._tool_error("runner_notify: ui_spec must be an object")
        http_srv = getattr(self, "_http_server", None)
        if http_srv is None:
            return self._tool_error("runner_notify requires HTTP daemon mode (--http flag)")
        result = http_srv.request_popup(runner_profile, ui_spec)
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}

    def _handle_icc_hub(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from scripts.mcp.hub_handler import handle_icc_hub
        return handle_icc_hub(
            arguments,
            tool_error=self._tool_error,
            tool_ok_json=self._tool_ok_json,
            elicit=self._elicit,
            http_mode=getattr(self, "_http_mode", False),
        )

    def handle_jsonrpc(self, request: dict[str, Any]) -> dict[str, Any]:
        req_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params") or {}
        if not isinstance(params, dict):
            params = {}

        if method == "initialize":
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
                        "elicitation": {},
                    },
                    "serverInfo": {"name": "emerge", "version": self._version},
                },
            }

        if method == "ping":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}

        if method == "logging/setLevel":
            # acknowledge but take no action (logging is daemon-managed)
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}

        if method.startswith("notifications/"):
            # MCP notifications are one-way; do NOT send a response
            return None

        if method == "tools/list":
            from scripts.mcp.schemas import get_tool_schemas
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": get_tool_schemas()},
            }

        if method == "tools/call":
            name = params.get("name", "")
            arguments = params.get("arguments", {}) or {}
            if not isinstance(arguments, dict):
                arguments = {}
            result = self.call_tool(name, arguments)
            return {"jsonrpc": "2.0", "id": req_id, "result": result}

        if method == "resources/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"resources": self._list_resources()}}

        if method == "resources/read":
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

        if method == "resources/templates/list":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "resourceTemplates": [
                        {
                            "uriTemplate": "pipeline://{connector}/{mode}/{name}",
                            "name": "Pipeline metadata",
                            "description": "Read pipeline YAML metadata by connector/mode/name",
                            "mimeType": "application/json",
                        },
                        {
                            "uriTemplate": "policy://current",
                            "name": "Policy registry",
                            "description": "Current session pipeline lifecycle state",
                            "mimeType": "application/json",
                        },
                        {
                            "uriTemplate": "runner://status",
                            "name": "Runner status",
                            "description": "Remote runner health summary",
                            "mimeType": "application/json",
                        },
                        {
                            "uriTemplate": "state://deltas",
                            "name": "State deltas",
                            "description": "StateTracker deltas and risks",
                            "mimeType": "application/json",
                        },
                    ]
                },
            }

        if method == "prompts/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"prompts": self._PROMPTS}}

        if method == "prompts/get":
            pname = params.get("name", "")
            pargs = params.get("arguments") or {}
            try:
                prompt = self._get_prompt(pname, pargs)
                return {"jsonrpc": "2.0", "id": req_id, "result": prompt}
            except KeyError as exc:
                return {"jsonrpc": "2.0", "id": req_id,
                        "error": {"code": -32602, "message": str(exc)}}

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }

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
        if profile_key not in self._sessions_by_profile:
            if normalized == "default":
                session_id = self._base_session_id
            else:
                session_id = f"{self._base_session_id}__{profile_key}"
            self._sessions_by_profile[profile_key] = ExecSession(
                state_root=self._state_root, session_id=session_id
            )
        return self._sessions_by_profile[profile_key]

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
            adapter_root=Path.home() / ".emerge" / "adapters",
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

    def _write_mcp_push(self, payload: dict) -> None:
        """Write a JSON-RPC notification/request to stdout for CC to receive."""
        line = json.dumps(payload) + "\n"
        with _stdout_lock:
            sys.stdout.write(line)
            sys.stdout.flush()

    def _elicit(
        self,
        message: str,
        schema: dict,
        timeout: float = 60.0,
    ) -> dict | None:
        """Send elicitations/create to CC; block current thread until response.

        Must be called from a worker thread (not the main stdio loop).
        Returns the content dict from the response, or None on timeout.

        In HTTP mode: CC does not maintain persistent SSE channel, returns None immediately.
        """
        if getattr(self, "_http_mode", False):
            return None  # HTTP mode: no server→client push channel
        import uuid
        elicit_id = f"elicit-{uuid.uuid4().hex[:8]}"
        event = threading.Event()
        self._elicit_events[elicit_id] = event
        self._write_mcp_push({
            "jsonrpc": "2.0",
            "id": elicit_id,
            "method": "elicitations/create",
            "params": {"message": message, "requestedSchema": schema},
        })
        fired = event.wait(timeout=timeout)
        if not fired:
            self._elicit_events.pop(elicit_id, None)
            self._elicit_results.pop(elicit_id, None)
            return None
        return self._elicit_results.pop(elicit_id, None)


def run_http(port: int = 8789, bind_host: str | None = None) -> None:
    """Start emerge daemon in HTTP MCP server mode with in-process cockpit."""
    import atexit
    import threading as _threading
    from scripts.daemon_http import DaemonHTTPServer

    daemon = EmergeDaemon()
    daemon._http_mode = True  # disable _elicit() blocking
    daemon.start_operator_monitor()
    daemon.start_event_router()
    atexit.register(daemon.stop_operator_monitor)
    atexit.register(daemon.stop_event_router)

    pid_path = Path.home() / ".emerge" / "daemon.pid"
    srv = DaemonHTTPServer(
        daemon=daemon, port=port, pid_path=pid_path, bind_host=bind_host
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
        help="Bind address for HTTP MCP (overrides EMERGE_DAEMON_BIND; default 127.0.0.1)",
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
