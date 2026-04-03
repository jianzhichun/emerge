from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.pipeline_engine import PipelineEngine  # noqa: E402
from scripts.policy_config import (  # noqa: E402
    PROMOTE_MAX_HUMAN_FIX_RATE,
    PROMOTE_MIN_ATTEMPTS,
    PROMOTE_MIN_SUCCESS_RATE,
    PROMOTE_MIN_VERIFY_RATE,
    ROLLBACK_CONSECUTIVE_FAILURES,
    STABLE_MIN_ATTEMPTS,
    STABLE_MIN_SUCCESS_RATE,
    STABLE_MIN_VERIFY_RATE,
    WINDOW_SIZE,
    derive_profile_token,
    derive_session_id,
    default_repl_root,
)
from scripts.runner_client import RunnerRouter  # noqa: E402
from scripts.repl_state import ReplState  # noqa: E402


class ReplDaemon:
    def __init__(self, root: Path | None = None) -> None:
        resolved_root = root or ROOT
        state_root = Path(os.environ.get("REPL_STATE_ROOT", str(default_repl_root()))).expanduser().resolve()
        self._base_session_id = derive_session_id(
            os.environ.get("REPL_SESSION_ID"), resolved_root
        )
        self._state_root = state_root
        self._repl_by_profile: dict[str, ReplState] = {}
        self.pipeline = PipelineEngine(root=resolved_root)
        self._root = resolved_root
        self._script_roots = self._resolve_script_roots()
        self._runner_router = RunnerRouter.from_env()
        from scripts.policy_config import load_settings, default_emerge_home
        from scripts.metrics import get_sink
        try:
            _settings = load_settings()
        except Exception:
            _settings = {}
        _default_metrics_path = default_emerge_home() / "metrics.jsonl"
        self._sink = get_sink(_settings, default_path=_default_metrics_path)

    def _try_l15_promote(self, arguments: dict[str, Any]) -> dict[str, Any] | None:
        intent_signature = str(arguments.get("intent_signature", "")).strip()
        script_ref = str(arguments.get("script_ref", "")).strip()
        base_pipeline_id = str(arguments.get("base_pipeline_id", "")).strip()
        if not (intent_signature and script_ref and base_pipeline_id):
            return None

        key = f"l15::{base_pipeline_id}::{intent_signature}::{script_ref}"
        session_dir = self._state_root / self._base_session_id
        candidates_path = session_dir / "candidates.json"
        candidates_data = self._load_json_object(candidates_path, root_key="candidates")
        candidate = candidates_data.get("candidates", {}).get(key)
        if not isinstance(candidate, dict):
            return None
        if str(candidate.get("status", "explore")) != "stable":
            return None

        pipelines_path = session_dir / "pipelines-registry.json"
        pipelines_data = self._load_json_object(pipelines_path, root_key="pipelines")
        pipeline_entry = pipelines_data.get("pipelines", {}).get(f"pipeline::{base_pipeline_id}")
        if not isinstance(pipeline_entry, dict):
            return None
        if str(pipeline_entry.get("status", "explore")) not in ("canary", "stable"):
            return None

        parts = base_pipeline_id.split(".", 2)
        if len(parts) != 3:
            return None
        connector, mode, name = parts
        try:
            if mode == "write":
                result = self.pipeline.run_write({**arguments, "connector": connector, "pipeline": name})
            else:
                result = self.pipeline.run_read({**arguments, "connector": connector, "pipeline": name})
        except Exception:
            return None
        result["l15_promoted"] = True
        try:
            self._sink.emit("l15.promoted", {"key": key, "pipeline_id": base_pipeline_id})
        except Exception:
            pass
        return result

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "icc_exec":
            # L1.5 promotion: if candidate is stable and pipeline is ready, redirect
            promoted = self._try_l15_promote(arguments)
            if promoted is not None:
                response = {"isError": False, "content": [{"type": "text", "text": json.dumps(promoted)}]}
                try:
                    tool_for_event = "icc_read" if promoted.get("pipeline_id", "").split(".")[1] == "read" else "icc_write"
                    self._record_pipeline_event(
                        tool_name=tool_for_event,
                        arguments=arguments,
                        result=promoted,
                        is_error=False,
                    )
                except Exception:
                    pass
                return response
            try:
                mode = str(arguments.get("mode", "inline_code"))
                target_profile = str(arguments.get("target_profile", "default"))
                candidate_key = self._resolve_exec_candidate_key(
                    arguments=arguments,
                    target_profile=target_profile,
                )
                sampled_in_policy = self._should_sample(candidate_key)
                _exec_client = self._runner_router.find_client(arguments) if self._runner_router else None
                if _exec_client is not None:
                    result = _exec_client.call_tool("icc_exec", arguments)
                else:
                    code = self._resolve_exec_code(mode=mode, arguments=arguments)
                    repl = self._get_repl(target_profile)
                    result = repl.exec_code(
                        code,
                        metadata={
                            "mode": mode,
                            "target_profile": target_profile,
                            "intent_signature": arguments.get("intent_signature", ""),
                            "script_ref": arguments.get("script_ref", ""),
                        },
                        inject_vars={"__args": arguments.get("script_args", {})},
                    )
                try:
                    self._record_exec_event(
                        arguments=arguments,
                        result=result,
                        target_profile=target_profile,
                        mode=mode,
                        sampled_in_policy=sampled_in_policy,
                        candidate_key=candidate_key,
                    )
                except Exception as exc:
                    self._append_warning_text(result, f"policy bookkeeping failed: {exc}")
                if "isError" not in result:
                    result["isError"] = False
                return result
            except Exception as exc:
                return {
                    "isError": True,
                    "content": [{"type": "text", "text": f"icc_exec failed: {exc}"}],
                }
        if name == "icc_read":
            try:
                _read_client = self._runner_router.find_client(arguments) if self._runner_router else None
                if _read_client is not None:
                    result = _read_client.call_tool("icc_read", arguments)
                    text = str(result.get("content", [{}])[0].get("text", ""))
                    payload = json.loads(text)
                    if not isinstance(payload, dict):
                        raise ValueError("runner icc_read payload must be an object")
                    result = payload
                else:
                    result = self.pipeline.run_read(arguments)
                response = {
                    "isError": False,
                    "content": [{"type": "text", "text": json.dumps(result)}],
                }
                try:
                    self._record_pipeline_event(
                        tool_name=name,
                        arguments=arguments,
                        result=result,
                        is_error=False,
                    )
                except Exception as exc:
                    self._append_warning_text(response, f"policy bookkeeping failed: {exc}")
                return response
            except Exception as exc:
                try:
                    self._record_pipeline_event(
                        tool_name=name,
                        arguments=arguments,
                        result={},
                        is_error=True,
                        error_text=str(exc),
                    )
                except Exception:
                    pass
                return {
                    "isError": True,
                    "content": [{"type": "text", "text": f"icc_read failed: {exc}"}],
                }
        if name == "icc_write":
            try:
                _write_client = self._runner_router.find_client(arguments) if self._runner_router else None
                if _write_client is not None:
                    result = _write_client.call_tool("icc_write", arguments)
                    text = str(result.get("content", [{}])[0].get("text", ""))
                    payload = json.loads(text)
                    if not isinstance(payload, dict):
                        raise ValueError("runner icc_write payload must be an object")
                    result = payload
                else:
                    result = self.pipeline.run_write(arguments)
                response = {
                    "isError": False,
                    "content": [{"type": "text", "text": json.dumps(result)}],
                }
                try:
                    self._record_pipeline_event(
                        tool_name=name,
                        arguments=arguments,
                        result=result,
                        is_error=False,
                    )
                except Exception as exc:
                    self._append_warning_text(response, f"policy bookkeeping failed: {exc}")
                return response
            except Exception as exc:
                try:
                    self._record_pipeline_event(
                        tool_name=name,
                        arguments=arguments,
                        result={},
                        is_error=True,
                        error_text=str(exc),
                    )
                except Exception:
                    pass
                return {
                    "isError": True,
                    "content": [{"type": "text", "text": f"icc_write failed: {exc}"}],
                }
        if name == "icc_reconcile":
            delta_id = str(arguments.get("delta_id", "")).strip()
            outcome = str(arguments.get("outcome", "")).strip()
            if not delta_id:
                return {"isError": True, "content": [{"type": "text", "text": "icc_reconcile: delta_id is required"}]}
            if outcome not in ("confirm", "correct", "retract"):
                return {"isError": True, "content": [{"type": "text", "text": f"icc_reconcile: outcome must be confirm/correct/retract, got {outcome!r}"}]}
            from scripts.policy_config import default_hook_state_root
            from scripts.state_tracker import load_tracker, save_tracker
            state_path = Path(os.environ.get("CLAUDE_PLUGIN_DATA", str(default_hook_state_root()))) / "state.json"
            tracker = load_tracker(state_path)
            tracker.reconcile_delta(delta_id, outcome)
            save_tracker(state_path, tracker)
            td = tracker.to_dict()
            return {"isError": False, "content": [{"type": "text", "text": json.dumps({
                "delta_id": delta_id,
                "outcome": outcome,
                "verification_state": td.get("verification_state", "unverified"),
                "goal": td.get("goal", ""),
            })}]}
        return {"isError": True, "content": [{"type": "text", "text": f"Unknown tool: {name}"}]}

    def handle_jsonrpc(self, request: dict[str, Any]) -> dict[str, Any]:
        req_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params") or {}
        if not isinstance(params, dict):
            params = {}

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {
                        "tools": {},
                        "resources": {"subscribe": False},
                        "prompts": {},
                        "logging": {},
                    },
                    "serverInfo": {"name": "emerge", "version": "0.2.0"},
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
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "tools": [
                        {
                            "name": "icc_exec",
                            "description": "Execute Python code in a persistent REPL with policy flywheel tracking",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "code": {"type": "string", "description": "Python code to execute (inline_code mode)"},
                                    "mode": {"type": "string", "enum": ["inline_code", "script_ref"], "default": "inline_code"},
                                    "target_profile": {"type": "string", "description": "Execution profile / remote runner key", "default": "default"},
                                    "intent_signature": {"type": "string", "description": "Stable identifier for this exec pattern (e.g. zwcad.read.state)"},
                                    "script_ref": {"type": "string", "description": "Path to script file (script_ref mode)"},
                                    "script_args": {"type": "object", "description": "Arguments injected as __args in script scope"},
                                    "base_pipeline_id": {"type": "string", "description": "Pipeline id for L1.5 promotion routing (e.g. mock.read.layers)"},
                                },
                                "required": [],
                            },
                        },
                        {
                            "name": "icc_read",
                            "description": "Run a read pipeline and return structured rows with verification",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "connector": {"type": "string", "description": "Connector name (e.g. zwcad, mock)"},
                                    "pipeline": {"type": "string", "description": "Pipeline name (e.g. state, layers)"},
                                    "target_profile": {"type": "string", "description": "Remote runner key if applicable"},
                                },
                                "required": ["connector", "pipeline"],
                            },
                        },
                        {
                            "name": "icc_write",
                            "description": "Run a write pipeline with verification and rollback/stop policy enforcement",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "connector": {"type": "string", "description": "Connector name (e.g. zwcad, mock)"},
                                    "pipeline": {"type": "string", "description": "Pipeline name (e.g. apply-change, add-wall)"},
                                    "target_profile": {"type": "string", "description": "Remote runner key if applicable"},
                                },
                                "required": ["connector", "pipeline"],
                            },
                        },
                        {
                            "name": "icc_reconcile",
                            "description": "Reconcile a state tracker delta — confirm, correct, or retract a recorded observation. Not auto-recommended; call directly when needed.",
                            "_internal": True,
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "delta_id": {"type": "string", "description": "ID of the delta to reconcile"},
                                    "outcome": {"type": "string", "enum": ["confirm", "correct", "retract"], "description": "Reconciliation outcome"},
                                },
                                "required": ["delta_id", "outcome"],
                            },
                        },
                    ]
                },
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
                            "description": "StateTracker goal, deltas, and risks",
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
        static = [
            {
                "uri": "policy://current",
                "name": "Pipeline policy registry",
                "mimeType": "application/json",
                "description": "Current session pipeline lifecycle tracking (explore→canary→stable)",
            },
            {
                "uri": "runner://status",
                "name": "Runner health summary",
                "mimeType": "application/json",
                "description": "Remote runner connectivity and health for all configured endpoints",
            },
            {
                "uri": "state://deltas",
                "name": "State tracker deltas",
                "mimeType": "application/json",
                "description": "Session goal, recorded deltas, and open risks",
            },
        ]
        for connector_root in self.pipeline._connector_roots:
            if not connector_root.exists():
                continue
            for meta in connector_root.glob("*/pipelines/*/*.yaml"):
                parts = meta.relative_to(connector_root).parts
                if len(parts) == 4:
                    connector, _, mode, name_yaml = parts
                    name = name_yaml[:-5]
                    uri = f"pipeline://{connector}/{mode}/{name}"
                    static.append({"uri": uri, "name": f"{connector} {mode} pipeline: {name}", "mimeType": "application/json", "description": f"Pipeline metadata for {connector}/{mode}/{name}"})
        return static

    def _read_resource(self, uri: str) -> dict[str, Any]:
        if uri == "policy://current":
            session_dir = self._state_root / self._base_session_id
            path = session_dir / "pipelines-registry.json"
            data = self._load_json_object(path, root_key="pipelines")
            return {"uri": uri, "mimeType": "application/json", "text": json.dumps(data)}
        if uri == "runner://status":
            router = RunnerRouter.from_env()
            summary = router.health_summary() if router else {"configured": False, "any_reachable": False}
            return {"uri": uri, "mimeType": "application/json", "text": json.dumps(summary)}
        if uri == "state://deltas":
            from scripts.policy_config import default_hook_state_root
            from scripts.state_tracker import load_tracker
            state_path = Path(os.environ.get("CLAUDE_PLUGIN_DATA", str(default_hook_state_root()))) / "state.json"
            tracker = load_tracker(state_path)
            return {"uri": uri, "mimeType": "application/json", "text": json.dumps(tracker.to_dict())}
        if uri.startswith("pipeline://"):
            rest = uri[len("pipeline://"):]
            parts = rest.split("/", 2)
            if len(parts) == 3:
                connector, mode, name = parts
                for connector_root in self.pipeline._connector_roots:
                    meta = connector_root / connector / "pipelines" / mode / f"{name}.yaml"
                    if meta.exists():
                        data = PipelineEngine._load_metadata(meta)
                        return {"uri": uri, "mimeType": "application/json", "text": json.dumps(data)}
        raise KeyError(f"Resource not found: {uri}")

    _PROMPTS = [
        {
            "name": "icc_explore",
            "description": "Explore a new vertical using icc_exec with policy tracking",
            "arguments": [
                {"name": "vertical", "description": "Name of the vertical (e.g. zwcad)", "required": True},
                {"name": "goal", "description": "What to explore", "required": False},
            ],
        },
        {
            "name": "icc_promote",
            "description": "Promote an exec history into a formalized pipeline",
            "arguments": [
                {"name": "intent_signature", "description": "Intent signature of the exec", "required": True},
                {"name": "script_ref", "description": "Path to the script that was executed", "required": True},
                {"name": "connector", "description": "Target connector name", "required": True},
            ],
        },
    ]

    def _get_prompt(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "icc_explore":
            vertical = str(arguments.get("vertical", "<vertical>"))
            goal = str(arguments.get("goal", "explore the vertical"))
            content = (
                f"Use icc_exec to explore the {vertical} vertical. Goal: {goal}.\n"
                f"Include intent_signature='<intent>' and script_ref='~/.emerge/connectors/{vertical}/pipelines/read/state.py' "
                f"in each icc_exec call so the policy flywheel can track progress.\n"
                f"When the exec is stable and consistent, use icc_read with connector='{vertical}' to verify the pipeline works."
            )
            return {"name": name, "messages": [{"role": "user", "content": content}]}
        if name == "icc_promote":
            sig = str(arguments.get("intent_signature", ""))
            ref = str(arguments.get("script_ref", ""))
            connector = str(arguments.get("connector", ""))
            content = (
                f"Promote the exec pattern '{sig}' (script: {ref}) to a formal {connector} pipeline.\n"
                f"1. Create ~/.emerge/connectors/{connector}/pipelines/read/<name>.yaml and <name>.py\n"
                f"2. Implement run_read() and verify_read() in the .py file\n"
                f"3. Call icc_read with connector='{connector}' to verify it works\n"
                f"4. The intent_signature in the yaml must match '{sig}'"
            )
            return {"name": name, "messages": [{"role": "user", "content": content}]}
        raise KeyError(f"Prompt not found: {name}")

    def _get_repl(self, target_profile: str) -> ReplState:
        normalized = (target_profile or "default").strip() or "default"
        profile_key = "__default__" if normalized == "default" else derive_profile_token(normalized)
        if profile_key not in self._repl_by_profile:
            if normalized == "default":
                session_id = self._base_session_id
            else:
                session_id = f"{self._base_session_id}__{profile_key}"
            self._repl_by_profile[profile_key] = ReplState(
                state_root=self._state_root, session_id=session_id
            )
        return self._repl_by_profile[profile_key]

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

    def _record_exec_event(
        self,
        *,
        arguments: dict[str, Any],
        result: dict[str, Any],
        target_profile: str,
        mode: str,
        sampled_in_policy: bool,
        candidate_key: str,
    ) -> None:
        is_error = bool(result.get("isError"))
        intent_signature = str(arguments.get("intent_signature", ""))
        script_ref = str(arguments.get("script_ref", ""))
        base_pipeline_id = str(arguments.get("base_pipeline_id", "")).strip()
        trusted_verify_passed = not is_error
        trusted_human_fix = False
        event = {
            "ts_ms": int(time.time() * 1000),
            "source": "exec",
            "mode": mode,
            "target_profile": target_profile,
            "intent_signature": intent_signature,
            "script_ref": script_ref,
            "base_pipeline_id": base_pipeline_id,
            "verify_passed": trusted_verify_passed,
            "human_fix": trusted_human_fix,
            "is_error": is_error,
            "sampled_in_policy": sampled_in_policy,
        }
        session_dir = self._state_root / self._base_session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        events_path = session_dir / "exec-events.jsonl"
        with events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=True) + "\n")
            f.flush()
            os.fsync(f.fileno())

        try:
            self._sink.emit(
                "exec.call",
                {
                    "intent_signature": arguments.get("intent_signature", ""),
                    "target_profile": arguments.get("target_profile", "default"),
                    "is_error": is_error,
                    "session_id": self._base_session_id,
                },
            )
        except Exception:
            pass

        if not intent_signature:
            return
        key = candidate_key
        registry_path = session_dir / "candidates.json"
        registry = self._load_json_object(registry_path, root_key="candidates")
        entry = registry["candidates"].get(
            key,
            {
                "source": "exec",
                "target_profile": target_profile,
                "intent_signature": intent_signature,
                "script_ref": script_ref or "<inline>",
                "attempts": 0,
                "successes": 0,
                "verify_passes": 0,
                "human_fixes": 0,
                "degraded_count": 0,
                "consecutive_failures": 0,
                "recent_outcomes": [],
                "total_calls": 0,
                "last_ts_ms": 0,
            },
        )
        entry["total_calls"] = int(entry.get("total_calls", 0)) + 1
        if is_error:
            sampled_in_policy = True
        if sampled_in_policy:
            entry["attempts"] += 1
            if not is_error:
                entry["successes"] += 1
            if trusted_verify_passed:
                entry["verify_passes"] += 1
            if trusted_human_fix:
                entry["human_fixes"] += 1
        is_degraded = False
        failed_attempt = (is_error or is_degraded) and sampled_in_policy
        if sampled_in_policy and is_degraded:
            entry["degraded_count"] += 1
        if sampled_in_policy:
            entry["consecutive_failures"] = (
                int(entry.get("consecutive_failures", 0)) + 1 if failed_attempt else 0
            )
            recent = list(entry.get("recent_outcomes", []))
            recent.append(0 if failed_attempt else 1)
            entry["recent_outcomes"] = recent[-WINDOW_SIZE:]
        entry["last_ts_ms"] = event["ts_ms"]
        registry["candidates"][key] = entry

        self._atomic_write_json(registry_path, registry)
        self._update_pipeline_registry(session_dir=session_dir, candidate_key=key, entry=entry)

    def _record_pipeline_event(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
        is_error: bool,
        error_text: str = "",
    ) -> None:
        session_dir = self._state_root / self._base_session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        connector = str(arguments.get("connector", "mock"))
        mode = "read" if tool_name == "icc_read" else "write"
        pipeline = str(arguments.get("pipeline", "layers" if mode == "read" else "add-wall"))
        pipeline_id = str(result.get("pipeline_id", f"{connector}.{mode}.{pipeline}"))
        intent_signature = str(result.get("intent_signature", ""))
        target_profile = str(arguments.get("target_profile", "default"))
        verify_passed = str(result.get("verification_state", "")).lower() == "verified"
        trusted_human_fix = False
        key = self._resolve_pipeline_candidate_key(arguments=arguments, pipeline_id=pipeline_id)
        sampled_in_policy = self._should_sample(key)
        if is_error:
            sampled_in_policy = True

        event = {
            "ts_ms": int(time.time() * 1000),
            "source": "pipeline",
            "tool_name": tool_name,
            "pipeline_id": pipeline_id,
            "target_profile": target_profile,
            "intent_signature": intent_signature,
            "script_ref": pipeline_id,
            "verify_passed": verify_passed,
            "human_fix": trusted_human_fix,
            "is_error": is_error,
            "sampled_in_policy": sampled_in_policy,
            "error": error_text,
        }

        events_path = session_dir / "pipeline-events.jsonl"
        with events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=True) + "\n")
            f.flush()
            os.fsync(f.fileno())

        try:
            _mode = "read" if "read" in tool_name else "write"
            self._sink.emit(
                f"pipeline.{_mode}",
                {
                    "pipeline_id": pipeline_id,
                    "is_error": is_error,
                    "session_id": self._base_session_id,
                },
            )
        except Exception:
            pass

        registry_path = session_dir / "candidates.json"
        registry = self._load_json_object(registry_path, root_key="candidates")
        entry = registry["candidates"].get(
            key,
            {
                "source": "l15_composed" if key.startswith("l15::") else "pipeline",
                "pipeline_id": pipeline_id,
                "target_profile": target_profile,
                "intent_signature": intent_signature or pipeline_id,
                "script_ref": pipeline_id,
                "attempts": 0,
                "successes": 0,
                "verify_passes": 0,
                "human_fixes": 0,
                "degraded_count": 0,
                "consecutive_failures": 0,
                "recent_outcomes": [],
                "total_calls": 0,
                "policy_enforced_count": 0,
                "stop_triggered_count": 0,
                "rollback_executed_count": 0,
                "last_policy_action": "none",
                "last_ts_ms": 0,
            },
        )
        policy_enforced = bool(result.get("policy_enforced", False))
        stop_triggered = bool(result.get("stop_triggered", False))
        rollback_executed = bool(result.get("rollback_executed", False))
        if policy_enforced:
            entry["policy_enforced_count"] = int(entry.get("policy_enforced_count", 0)) + 1
        if stop_triggered:
            entry["stop_triggered_count"] = int(entry.get("stop_triggered_count", 0)) + 1
        if rollback_executed:
            entry["rollback_executed_count"] = int(entry.get("rollback_executed_count", 0)) + 1
        if rollback_executed:
            entry["last_policy_action"] = "rollback"
        elif stop_triggered:
            entry["last_policy_action"] = "stop"
        else:
            entry["last_policy_action"] = "none"
        entry["total_calls"] = int(entry.get("total_calls", 0)) + 1
        if sampled_in_policy:
            entry["attempts"] += 1
            if not is_error:
                entry["successes"] += 1
            if event["verify_passed"]:
                entry["verify_passes"] += 1
            if trusted_human_fix:
                entry["human_fixes"] += 1
        is_degraded = str(result.get("verification_state", "")).lower() == "degraded"
        failed_attempt = (is_error or is_degraded) and sampled_in_policy
        if sampled_in_policy and is_degraded:
            entry["degraded_count"] += 1
        if sampled_in_policy:
            entry["consecutive_failures"] = (
                int(entry.get("consecutive_failures", 0)) + 1 if failed_attempt else 0
            )
            recent = list(entry.get("recent_outcomes", []))
            recent.append(0 if failed_attempt else 1)
            entry["recent_outcomes"] = recent[-WINDOW_SIZE:]
        entry["last_ts_ms"] = event["ts_ms"]
        registry["candidates"][key] = entry

        self._atomic_write_json(registry_path, registry)
        self._update_pipeline_registry(session_dir=session_dir, candidate_key=key, entry=entry)

    def _update_pipeline_registry(
        self,
        *,
        session_dir: Path,
        candidate_key: str,
        entry: dict[str, Any],
    ) -> None:
        registry_path = session_dir / "pipelines-registry.json"
        registry = self._load_json_object(registry_path, root_key="pipelines")
        pipeline = registry["pipelines"].get(
            candidate_key,
            {
                "status": "explore",
                "rollout_pct": 0,
                "last_transition_reason": "init",
                "attempts_at_transition": 0,
            },
        )

        attempts = int(entry.get("attempts", 0))
        if attempts == 0:
            attempts = 1
        success_rate = float(entry.get("successes", 0)) / attempts
        verify_rate = float(entry.get("verify_passes", 0)) / attempts
        human_fix_rate = float(entry.get("human_fixes", 0)) / attempts
        consecutive_failures = int(entry.get("consecutive_failures", 0))
        recent_outcomes = list(entry.get("recent_outcomes", []))
        window_attempts = len(recent_outcomes)
        window_success_rate = (
            sum(recent_outcomes) / window_attempts if window_attempts else 0.0
        )

        status = str(pipeline.get("status", "explore"))
        transitioned = False
        reason = "no_change"

        if status == "explore":
            should_promote = (
                attempts >= PROMOTE_MIN_ATTEMPTS
                and success_rate >= PROMOTE_MIN_SUCCESS_RATE
                and verify_rate >= PROMOTE_MIN_VERIFY_RATE
                and human_fix_rate <= PROMOTE_MAX_HUMAN_FIX_RATE
                and consecutive_failures == 0
            )
            if should_promote:
                status = "canary"
                transitioned = True
                reason = "promotion_threshold_met"
                pipeline["rollout_pct"] = 20
        elif status == "canary":
            if consecutive_failures >= ROLLBACK_CONSECUTIVE_FAILURES:
                status = "explore"
                transitioned = True
                reason = "two_consecutive_failures"
                pipeline["rollout_pct"] = 0
            else:
                should_stabilize = (
                    attempts >= STABLE_MIN_ATTEMPTS
                    and success_rate >= STABLE_MIN_SUCCESS_RATE
                    and verify_rate >= STABLE_MIN_VERIFY_RATE
                    and consecutive_failures == 0
                )
                if should_stabilize:
                    status = "stable"
                    transitioned = True
                    reason = "stable_threshold_met"
                    pipeline["rollout_pct"] = 100
        elif status == "stable":
            if consecutive_failures >= ROLLBACK_CONSECUTIVE_FAILURES:
                status = "explore"
                transitioned = True
                reason = "two_consecutive_failures"
                pipeline["rollout_pct"] = 0
            elif window_attempts >= WINDOW_SIZE and window_success_rate < 0.9:
                status = "explore"
                transitioned = True
                reason = "window_failure_rate"
                pipeline["rollout_pct"] = 0

        pipeline["status"] = status
        pipeline["success_rate"] = round(success_rate, 4)
        pipeline["verify_rate"] = round(verify_rate, 4)
        pipeline["human_fix_rate"] = round(human_fix_rate, 4)
        pipeline["consecutive_failures"] = consecutive_failures
        pipeline["window_success_rate"] = round(window_success_rate, 4)
        pipeline["policy_enforced_count"] = int(entry.get("policy_enforced_count", 0))
        pipeline["stop_triggered_count"] = int(entry.get("stop_triggered_count", 0))
        pipeline["rollback_executed_count"] = int(entry.get("rollback_executed_count", 0))
        pipeline["last_policy_action"] = str(entry.get("last_policy_action", "none"))
        pipeline["updated_at_ms"] = int(time.time() * 1000)
        if transitioned:
            pipeline["last_transition_reason"] = reason
            pipeline["attempts_at_transition"] = attempts
            try:
                self._sink.emit(
                    "policy.transition",
                    {"candidate_key": candidate_key, "new_status": status, "session_id": self._base_session_id},
                )
            except Exception:
                pass

        registry["pipelines"][candidate_key] = pipeline
        self._atomic_write_json(registry_path, registry)

    @staticmethod
    def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
        fd, tmp_path = tempfile.mkstemp(prefix=f"{path.stem}-", suffix=".json", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                json.dump(data, tmp, ensure_ascii=True, indent=2)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_path, path)
            tmp_path = ""
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def _resolve_script_roots(self) -> list[Path]:
        raw = os.environ.get("REPL_SCRIPT_ROOTS", "").strip()
        if raw:
            roots = [Path(p).expanduser().resolve() for p in raw.split(",") if p.strip()]
        else:
            roots = [
                (self._root / "connectors").resolve(),
                (Path.home() / ".emerge" / "assets").resolve(),
            ]
        return roots

    def _is_allowed_script_path(self, path: Path) -> bool:
        for root in self._script_roots:
            try:
                path.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    @staticmethod
    def _candidate_key(*, target_profile: str, intent_signature: str, script_ref: str) -> str:
        return f"{target_profile}::{intent_signature}::{script_ref or '<inline>'}"

    @staticmethod
    def _pipeline_candidate_key(pipeline_id: str) -> str:
        return f"pipeline::{pipeline_id}"

    @staticmethod
    def _l15_candidate_key(pipeline_id: str, intent_signature: str, script_ref: str) -> str:
        return f"l15::{pipeline_id}::{intent_signature}::{script_ref}"

    def _resolve_exec_candidate_key(self, *, arguments: dict[str, Any], target_profile: str) -> str:
        intent_signature = str(arguments.get("intent_signature", "")).strip()
        script_ref = str(arguments.get("script_ref", "")).strip() or "<inline>"
        base_pipeline_id = str(arguments.get("base_pipeline_id", "")).strip()
        if base_pipeline_id and intent_signature:
            return self._l15_candidate_key(base_pipeline_id, intent_signature, script_ref)
        return self._candidate_key(
            target_profile=target_profile,
            intent_signature=intent_signature,
            script_ref=script_ref,
        )

    def _resolve_pipeline_candidate_key(self, *, arguments: dict[str, Any], pipeline_id: str) -> str:
        exec_signature = str(arguments.get("exec_signature", "")).strip()
        script_ref = str(arguments.get("script_ref", "")).strip()
        if exec_signature and script_ref:
            return self._l15_candidate_key(pipeline_id, exec_signature, script_ref)
        return self._pipeline_candidate_key(pipeline_id)

    def _should_sample(self, candidate_key: str) -> bool:
        if "::" not in candidate_key:
            return True
        session_dir = self._state_root / self._base_session_id
        path = session_dir / "pipelines-registry.json"
        if not path.exists():
            return True
        data = self._load_json_object(path, root_key="pipelines")
        pipeline = data.get("pipelines", {}).get(candidate_key)
        if not isinstance(pipeline, dict):
            return True
        status = str(pipeline.get("status", "explore"))
        if status != "canary":
            return True
        try:
            rollout_pct = int(pipeline.get("rollout_pct", 0))
        except Exception:
            rollout_pct = 0
        rollout_pct = max(0, min(100, rollout_pct))
        if rollout_pct <= 0:
            return False

        candidates_path = session_dir / "candidates.json"
        total_calls = 0
        if candidates_path.exists():
            cand = self._load_json_object(candidates_path, root_key="candidates")
            entry = cand.get("candidates", {}).get(candidate_key, {})
            if isinstance(entry, dict):
                total_calls = int(entry.get("total_calls", 0))
        next_call = total_calls + 1
        return ((next_call - 1) % 100) < rollout_pct

    @staticmethod
    def _append_warning_text(result: dict[str, Any], warning: str) -> None:
        content = result.get("content")
        if isinstance(content, list) and content and isinstance(content[0], dict):
            current = str(content[0].get("text", ""))
            content[0]["text"] = f"{current}\n\nwarning:\n{warning}".strip()
            return
        result["content"] = [{"type": "text", "text": f"warning:\n{warning}"}]

    @staticmethod
    def _load_json_object(path: Path, *, root_key: str) -> dict[str, Any]:
        if not path.exists():
            return {root_key: {}}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {root_key: {}}
        if not isinstance(data, dict):
            raise ValueError(f"{path.name} must be a JSON object")
        if root_key not in data or not isinstance(data[root_key], dict):
            data[root_key] = {}
        return data


def run_stdio() -> None:
    daemon = ReplDaemon()
    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue
        try:
            req = json.loads(text)
            resp = daemon.handle_jsonrpc(req)
        except Exception as exc:  # pragma: no cover
            resp = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32603, "message": str(exc)},
            }
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    run_stdio()
