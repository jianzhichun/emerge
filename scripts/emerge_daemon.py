from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

# Canonical pipeline key format: <connector>.<mode>.<name[.subname...]>
# connector/mode/name segments: lowercase, starts with letter, alphanumerics + _ -
_PIPELINE_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]*\.(read|write)\.[a-z][a-z0-9_./-]*$")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.pipeline_engine import PipelineEngine, PipelineMissingError  # noqa: E402
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
    default_exec_root,
    default_hook_state_root,
    pin_plugin_data_path_if_present,
)
from scripts.runner_client import RunnerRouter  # noqa: E402
from scripts.exec_session import ExecSession  # noqa: E402
from scripts.goal_control_plane import (  # noqa: E402
    EVENT_ROLLBACK_REQUEST,
    EVENT_SYSTEM_GENERATE,
    EVENT_SYSTEM_REFINE,
    GoalControlPlane,
)

_stdout_lock = threading.Lock()


class _IndentedSafeDumper:
    @staticmethod
    def dump_yaml(payload: dict[str, Any]) -> str:
        import yaml  # type: ignore

        class _Dumper(yaml.SafeDumper):
            # Force block sequence items under mappings to indent by two spaces.
            def increase_indent(self, flow=False, indentless=False):  # type: ignore[override]
                return super().increase_indent(flow, False)

        return yaml.dump(
            payload,
            Dumper=_Dumper,
            sort_keys=False,
            allow_unicode=True,
        )


class _RunnerClientAdapter:
    """Calls /operator-events HTTP endpoint on the remote runner directly.
    Used by OperatorMonitor to fetch operator events from runner machines."""

    def __init__(self, base_url: str, timeout_s: float = 5.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s

    def get_events(self, machine_id: str, since_ms: int = 0) -> list[dict]:
        import urllib.parse
        import json as _j
        from scripts.runner_client import _NO_PROXY_OPENER
        url = (
            f"{self._base_url}/operator-events"
            f"?machine_id={urllib.parse.quote(machine_id)}&since_ms={since_ms}"
        )
        try:
            with _NO_PROXY_OPENER.open(url, timeout=self._timeout_s) as r:
                data = _j.loads(r.read())
            return data.get("events", [])
        except Exception:
            return []


class EmergeDaemon:
    def __init__(self, root: Path | None = None) -> None:
        resolved_root = root or ROOT
        pin_plugin_data_path_if_present()
        state_root = Path(
            os.environ.get("EMERGE_STATE_ROOT") or str(default_exec_root())
        ).expanduser().resolve()
        self._base_session_id = derive_session_id(
            os.environ.get("EMERGE_SESSION_ID"),
            resolved_root,
        )
        self._state_root = state_root
        self._sessions_by_profile: dict[str, ExecSession] = {}
        self.pipeline = PipelineEngine(root=resolved_root)
        self._root = resolved_root
        self._script_roots = self._resolve_script_roots()
        self._runner_router = RunnerRouter.from_env()  # cached; refreshed lazily via _get_runner_router()
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
        self._pending_monitor: "PendingActionMonitor | None" = None
        self._goal_control = GoalControlPlane(Path(default_hook_state_root()))
        self._goal_control.ensure_initialized()
        self._migrate_legacy_goal_once()

    def _hook_state_path(self) -> Path:
        return Path(default_hook_state_root()) / "state.json"

    def _migrate_legacy_goal_once(self) -> None:
        state_path = self._hook_state_path()
        if not state_path.exists():
            return
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(data, dict):
            return
        goal_text = str(data.get("goal", "") or "").strip()
        goal_source = str(data.get("goal_source", "legacy") or "legacy")
        if not goal_text:
            return
        try:
            self._goal_control.migrate_legacy_goal(legacy_goal=goal_text, legacy_source=goal_source)
        except Exception:
            return

    def _get_runner_router(self) -> "RunnerRouter | None":
        """Always reload from disk so runner config added after daemon start is picked up."""
        return RunnerRouter.from_env()

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
        pipelines_data = self._load_json_object(pipelines_path, root_key="pipelines")
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
        except Exception:
            return None
        result["bridge_promoted"] = True
        try:
            self._sink.emit("flywheel.bridge.promoted", {"key": base_pipeline_id, "pipeline_id": base_pipeline_id})
        except Exception:
            pass
        return result

    def _crystallize(
        self,
        *,
        intent_signature: str,
        connector: str,
        pipeline_name: str,
        mode: str,
        target_profile: str = "default",
    ) -> dict[str, Any]:
        """Scan the WAL for the most recent synthesizable exec for intent_signature,
        wrap it in a pipeline harness, and write .py + .yaml to the connector root.
        """
        import time as _time
        import textwrap

        # --- find synthesizable WAL entry ---
        normalized = (target_profile or "default").strip() or "default"
        profile_suffix = "" if normalized == "default" else f"__{derive_profile_token(normalized)}"

        # Scan ALL session dirs for this profile suffix — crystallize must work even
        # when the synthesizable exec ran in a prior daemon session.
        best_code: str | None = None
        best_ts: int = 0
        if self._state_root.exists():
            for session_dir in sorted(self._state_root.iterdir()):
                if not session_dir.is_dir():
                    continue
                # Match profile: default dirs have no suffix, profiled dirs end with __<token>
                dir_name = session_dir.name
                if profile_suffix:
                    if not dir_name.endswith(profile_suffix):
                        continue
                else:
                    # default profile: dir must NOT contain __ (no profile suffix)
                    if "__" in dir_name:
                        continue
                wal_path = session_dir / "wal.jsonl"
                if not wal_path.exists():
                    continue
                with wal_path.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if (
                            entry.get("status") == "success"
                            and not entry.get("no_replay", False)
                            and entry.get("metadata", {}).get("intent_signature") == intent_signature
                        ):
                            ts = int(entry.get("finished_at_ms", 0))
                            if ts > best_ts:
                                best_ts = ts
                                best_code = str(entry.get("code", "")).strip()

        if not best_code:
            return {
                "isError": True,
                "content": [{"type": "text", "text": (
                    f"icc_crystallize: no synthesizable WAL entry found for "
                    f"intent_signature='{intent_signature}'. Run icc_exec with "
                    f"intent_signature='{intent_signature}' and no_replay=false first."
                )}],
            }

        # --- generate pipeline harness ---
        ts = int(_time.time())
        indented = textwrap.indent(best_code, "    ")

        # Pull description from pipeline registry (populated during exec tracking)
        _registry_path = self._state_root / "pipelines-registry.json"
        _registry = self._load_json_object(_registry_path, root_key="pipelines")
        description = str(_registry["pipelines"].get(intent_signature, {}).get("description", "")).strip()

        if mode == "read":
            py_src = (
                f"# auto-generated by icc_crystallize — review before promoting\n"
                f"# intent_signature: {intent_signature}\n"
                f"# synthesized_at: {ts}\n"
                f"\n"
                f"def run_read(metadata, args):\n"
                f"    __args = args  # compat with exec __args scope\n"
                f"    # --- CRYSTALLIZED ---\n"
                f"{indented}\n"
                f"    # --- END ---\n"
                f"    return __result  # exec code must set __result = [{{...}}]\n"
                f"\n"
                f"\n"
                f"def verify_read(metadata, args, rows):\n"
                f"    return {{\"ok\": bool(rows)}}\n"
            )
            mode_step_key = "read_steps"
            mode_step_value = "run_read"
            verify_step_value = "verify_read"
        else:  # write
            py_src = (
                f"# auto-generated by icc_crystallize — review before promoting\n"
                f"# intent_signature: {intent_signature}\n"
                f"# synthesized_at: {ts}\n"
                f"\n"
                f"def run_write(metadata, args):\n"
                f"    __args = args  # compat with exec __args scope\n"
                f"    # --- CRYSTALLIZED ---\n"
                f"{indented}\n"
                f"    # --- END ---\n"
                f"    return __action  # exec code must set __action = {{\"ok\": True, ...}}\n"
                f"\n"
                f"\n"
                f"def verify_write(metadata, args, action_result):\n"
                f"    return {{\"ok\": bool(action_result.get(\"ok\"))}}\n"
            )
            mode_step_key = "write_steps"
            mode_step_value = "run_write"
            verify_step_value = "verify_write"

        yaml_data: dict[str, Any] = {
            "intent_signature": intent_signature,
            "rollback_or_stop_policy": "stop",
            mode_step_key: [mode_step_value],
            "verify_steps": [verify_step_value],
            "synthesized": True,
            "synthesized_at": ts,
        }
        if description:
            yaml_data["description"] = description

        try:
            yaml_src = _IndentedSafeDumper.dump_yaml(yaml_data)
        except ImportError as exc:
            raise RuntimeError(
                "PyYAML is required to crystallize pipeline metadata. Install with: pip install pyyaml"
            ) from exc

        # --- write files ---
        from scripts.pipeline_engine import _USER_CONNECTOR_ROOT
        env_root_str = os.environ.get("EMERGE_CONNECTOR_ROOT", "").strip()
        target_root = Path(env_root_str).expanduser() if env_root_str else _USER_CONNECTOR_ROOT

        pipeline_dir = target_root / connector / "pipelines" / mode
        # Path traversal guard — ensure resolved path stays inside target_root
        try:
            pipeline_dir.resolve().relative_to(target_root.resolve())
        except ValueError as _e:
            raise ValueError(
                f"icc_crystallize: connector/mode path escapes connector root "
                f"(connector={connector!r}, mode={mode!r})"
            ) from _e
        pipeline_dir.mkdir(parents=True, exist_ok=True)

        py_path = pipeline_dir / f"{pipeline_name}.py"
        yaml_path = pipeline_dir / f"{pipeline_name}.yaml"
        # Guard individual file paths too
        for _check_path in (py_path, yaml_path):
            try:
                _check_path.resolve().relative_to(target_root.resolve())
            except ValueError as _e:
                raise ValueError(
                    f"icc_crystallize: pipeline_name path escapes connector root "
                    f"(pipeline_name={pipeline_name!r})"
                ) from _e

        # Atomic writes using temp file + os.replace to prevent partial state
        for dest_path, content in ((py_path, py_src), (yaml_path, yaml_src)):
            fd, tmp = tempfile.mkstemp(prefix=".crystallize-", dir=str(pipeline_dir))
            tmp_path = tmp
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, dest_path)
                tmp_path = ""
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    os.unlink(tmp_path)

        # Clear synthesis_ready — pipeline is now on disk, no need to re-crystallize
        if intent_signature in _registry["pipelines"]:
            _registry["pipelines"][intent_signature].pop("synthesis_ready", None)
            self._atomic_write_json(_registry_path, _registry)

        preview_lines = py_src.splitlines()[:20]
        code_preview = "\n".join(preview_lines)

        next_step = (
            f"Pipeline crystallized. Switch to pipeline path now:\n"
            f"  icc_{'read' if mode == 'read' else 'write'} connector={connector!r} pipeline={pipeline_name!r}\n"
            f"Do NOT call icc_exec for this intent again — the pipeline handles it."
        )
        return {
            "ok": True,
            "py_path": str(py_path),
            "yaml_path": str(yaml_path),
            "code_preview": code_preview,
            "next_step": next_step,
            "content": [{"type": "text", "text": json.dumps({
                "ok": True,
                "py_path": str(py_path),
                "yaml_path": str(yaml_path),
                "next_step": next_step,
            })}],
        }

    @staticmethod
    def _tool_error(text: str) -> dict[str, Any]:
        return {"isError": True, "content": [{"type": "text", "text": text}]}

    @staticmethod
    def _tool_ok_json(payload: Any) -> dict[str, Any]:
        return {"isError": False, "content": [{"type": "text", "text": json.dumps(payload)}]}

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

    def _call_pipeline_tool(
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
                self._record_pipeline_event(
                    tool_name=tool_name,
                    arguments=arguments,
                    result=result,
                    is_error=False,
                    execution_path=execution_path,
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
            return {
                "isError": False,
                "pipeline_missing": True,
                "connector": connector,
                "pipeline": pipeline,
                "mode": mode,
                "fallback": "icc_exec",
                "fallback_hint": hint,
                "content": [{"type": "text", "text": f"Pipeline not found. {hint}"}],
            }
        except Exception as exc:
            try:
                self._record_pipeline_event(
                    tool_name=tool_name,
                    arguments=arguments,
                    result={},
                    is_error=True,
                    error_text=str(exc),
                    execution_path="local",
                )
            except Exception:
                pass
            return {
                "isError": True,
                "recovery_suggestion": "exec",
                "content": [{"type": "text", "text": f"{tool_name} failed: {exc}"}],
            }

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "icc_goal_ingest":
            event_type = str(arguments.get("event_type", EVENT_SYSTEM_REFINE)).strip()
            source = str(arguments.get("source", "system")).strip() or "system"
            actor = str(arguments.get("actor", "daemon")).strip() or "daemon"
            text = str(arguments.get("text", "")).strip()
            rationale = str(arguments.get("rationale", "")).strip()
            confidence = self._as_float(arguments.get("confidence", 0.8), 0.8)
            context_match_score = self._as_float(arguments.get("context_match_score", 0.5), 0.5)
            recent_failure_risk = self._as_float(arguments.get("recent_failure_risk", 0.0), 0.0)
            ttl_ms = int(arguments.get("ttl_ms", 0) or 0)
            lock_window_ms = int(arguments.get("lock_window_ms", 0) or 0)
            force = bool(arguments.get("force", False))
            target_event_id = str(arguments.get("target_event_id", "")).strip()
            try:
                result = self._goal_control.ingest(
                    event_type=event_type,
                    source=source,
                    actor=actor,
                    text=text,
                    rationale=rationale,
                    confidence=confidence,
                    ttl_ms=ttl_ms,
                    lock_window_ms=lock_window_ms,
                    force=force,
                    target_event_id=target_event_id,
                    context_match_score=context_match_score,
                    recent_failure_risk=recent_failure_risk,
                )
            except Exception as exc:
                return self._tool_error(f"icc_goal_ingest failed: {exc}")
            return self._tool_ok_json(result)
        if name == "icc_goal_read":
            snapshot = self._goal_control.read_snapshot()
            limit = int(arguments.get("limit", 30) or 30)
            ledger = self._goal_control.read_ledger(limit=max(1, min(500, limit)))
            payload = {"snapshot": snapshot, "events": ledger}
            return self._tool_ok_json(payload)
        if name == "icc_goal_rollback":
            target_event_id = str(arguments.get("target_event_id", "")).strip()
            if not target_event_id:
                return self._tool_error("icc_goal_rollback: target_event_id is required")
            try:
                result = self._goal_control.rollback(
                    target_event_id=target_event_id,
                    actor=str(arguments.get("actor", "daemon") or "daemon"),
                    rationale=str(arguments.get("rationale", "") or ""),
                )
            except Exception as exc:
                return self._tool_error(f"icc_goal_rollback failed: {exc}")
            return self._tool_ok_json(result)
        if name == "icc_exec":
            # flywheel bridge: if candidate is stable and pipeline is ready, redirect
            promoted = self._try_flywheel_bridge(arguments)
            if promoted is not None:
                response = {"isError": False, "content": [{"type": "text", "text": json.dumps(promoted)}]}
                try:
                    _pid_parts = promoted.get("pipeline_id", "").split(".")
                    tool_for_event = "icc_read" if len(_pid_parts) >= 2 and _pid_parts[1] == "read" else "icc_write"
                    _rr = self._get_runner_router()
                    _client = _rr.find_client(arguments) if _rr else None
                    _execution_path = "remote" if _client is not None else "local"
                    self._record_pipeline_event(
                        tool_name=tool_for_event,
                        arguments=arguments,
                        result=promoted,
                        is_error=False,
                        execution_path=_execution_path,
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
                _rr = self._get_runner_router()
                _exec_client = _rr.find_client(arguments) if _rr else None
                execution_path = "remote" if _exec_client is not None else "local"
                if _exec_client is not None:
                    # Enforce local script_ref allowlist before remote dispatch so
                    # local and remote execution paths share the same trust boundary.
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
                    self._record_exec_event(
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
                if "isError" not in result:
                    result["isError"] = False
                return result
            except Exception as exc:
                return {
                    "isError": True,
                    "content": [{"type": "text", "text": f"icc_exec failed: {exc}"}],
                }
        if name == "icc_read":
            return self._call_pipeline_tool(tool_name=name, mode="read", arguments=arguments)
        if name == "icc_write":
            return self._call_pipeline_tool(tool_name=name, mode="write", arguments=arguments)
        if name == "icc_crystallize":
            try:
                intent_signature = str(arguments.get("intent_signature", "")).strip()
                connector = str(arguments.get("connector", "")).strip()
                pipeline_name = str(arguments.get("pipeline_name", "")).strip()
                mode = str(arguments.get("mode", "read")).strip()
                target_profile = str(arguments.get("target_profile", "default")).strip()
                if not all([intent_signature, connector, pipeline_name, mode]):
                    return {
                        "isError": True,
                        "content": [{"type": "text", "text": "icc_crystallize: intent_signature, connector, pipeline_name, and mode are required"}],
                    }
                if mode not in ("read", "write"):
                    return {
                        "isError": True,
                        "content": [{"type": "text", "text": f"icc_crystallize: mode must be 'read' or 'write', got {mode!r}"}],
                    }
                return self._crystallize(
                    intent_signature=intent_signature,
                    connector=connector,
                    pipeline_name=pipeline_name,
                    mode=mode,
                    target_profile=target_profile,
                )
            except Exception as exc:
                return self._tool_error(f"icc_crystallize failed: {exc}")
        if name == "icc_reconcile":
            delta_id = str(arguments.get("delta_id", "")).strip()
            outcome = str(arguments.get("outcome", "")).strip()
            intent_signature = str(arguments.get("intent_signature", "")).strip()
            if not delta_id:
                return self._tool_error("icc_reconcile: delta_id is required")
            if outcome not in ("confirm", "correct", "retract"):
                return self._tool_error(
                    f"icc_reconcile: outcome must be confirm/correct/retract, got {outcome!r}"
                )
            from scripts.state_tracker import load_tracker, save_tracker
            state_path = self._hook_state_path()
            tracker = load_tracker(state_path)
            tracker.reconcile_delta(delta_id, outcome)
            save_tracker(state_path, tracker)
            td = tracker.to_dict()
            goal_snapshot = self._goal_control.read_snapshot()
            # When outcome=correct and intent_signature provided, increment human_fixes
            if outcome == "correct" and intent_signature:
                self._increment_human_fix(intent_signature)
            return self._tool_ok_json({
                "delta_id": delta_id,
                "outcome": outcome,
                "intent_signature": intent_signature or None,
                "verification_state": td.get("verification_state", "unverified"),
                "goal": goal_snapshot.get("text", ""),
                "goal_source": goal_snapshot.get("source", "unset"),
                "goal_version": goal_snapshot.get("version", 0),
            })
        return self._tool_error(f"Unknown tool: {name}")

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
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "tools": [
                        {
                            "name": "icc_exec",
                            "description": "Execute Python in a persistent session with flywheel tracking. intent_signature is required (enforced). Read tasks set __result=[{...}]; write tasks set __action={'ok':True,...}; side effects use no_replay=True.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "code": {"type": "string", "description": "Python code to execute (inline_code mode)"},
                                    "mode": {"type": "string", "enum": ["inline_code", "script_ref"], "default": "inline_code"},
                                    "target_profile": {"type": "string", "description": "Execution profile / remote runner key", "default": "default"},
                                    "intent_signature": {"type": "string", "description": "Stable dot-notation identifier for this exec pattern (e.g. zwcad.read.state). Required for flywheel tracking. Use connector://notes to see existing intents before choosing."},
                                    "description": {"type": "string", "description": "Human-readable description of what this intent does. Stored in registry and surfaced in connector://notes. Only needed the first time a new intent is introduced."},
                                    "no_replay": {"type": "boolean", "description": "If true, exclude this call from WAL replay and crystallization. Use for side-effectful calls only.", "default": False},
                                    "script_ref": {"type": "string", "description": "Path to script file (script_ref mode)"},
                                    "script_args": {"type": "object", "description": "Arguments injected as __args in script scope"},
                                    "result_var": {"type": "string", "description": "Optional variable name to extract from exec globals as structured JSON in response (e.g. '__result')."},
                                    "base_pipeline_id": {"type": "string", "description": "Pipeline id for flywheel bridge routing (e.g. mock.read.layers)"},
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
                            "name": "icc_goal_ingest",
                            "description": "Submit a goal event proposal to Goal Control Plane and get the latest decision snapshot.",
                            "_internal": True,
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "event_type": {"type": "string", "enum": ["human_edit", "hook_payload", "system_generate", "system_refine", "rollback_request"]},
                                    "source": {"type": "string"},
                                    "actor": {"type": "string"},
                                    "text": {"type": "string"},
                                    "rationale": {"type": "string"},
                                    "confidence": {"type": "number"},
                                    "context_match_score": {"type": "number", "description": "0..1 context relevance score"},
                                    "recent_failure_risk": {"type": "number", "description": "0..1 risk score from recent failures"},
                                    "ttl_ms": {"type": "integer"},
                                    "lock_window_ms": {"type": "integer"},
                                    "force": {"type": "boolean"},
                                    "target_event_id": {"type": "string"},
                                },
                                "required": ["event_type", "source", "actor", "text"],
                            },
                        },
                        {
                            "name": "icc_goal_read",
                            "description": "Read active goal snapshot and recent goal event ledger.",
                            "_internal": True,
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "limit": {"type": "integer", "description": "Max ledger rows to return", "default": 30},
                                },
                                "required": [],
                            },
                        },
                        {
                            "name": "icc_goal_rollback",
                            "description": "Rollback active goal to a previous goal event id.",
                            "_internal": True,
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "target_event_id": {"type": "string"},
                                    "actor": {"type": "string"},
                                    "rationale": {"type": "string"},
                                },
                                "required": ["target_event_id"],
                            },
                        },
                        {
                            "name": "icc_reconcile",
                            "description": "Reconcile a state tracker delta — confirm, correct, or retract a recorded observation. Pass intent_signature with outcome=correct to register a human fix against the policy flywheel.",
                            "_internal": True,
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "delta_id": {"type": "string", "description": "ID of the delta to reconcile"},
                                    "outcome": {"type": "string", "enum": ["confirm", "correct", "retract"], "description": "Reconciliation outcome"},
                                    "intent_signature": {"type": "string", "description": "Intent signature of the exec/pipeline being corrected (required when outcome=correct to update human_fix_rate)"},
                                },
                                "required": ["delta_id", "outcome"],
                            },
                        },
                        {
                            "name": "icc_crystallize",
                            "description": "Crystallize exec history into a pipeline file. Reads the WAL for the most recent successful icc_exec matching intent_signature and generates .py + .yaml in the connector root. Call when synthesis_ready is true in policy://current.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "intent_signature": {"type": "string", "description": "Intent signature used in icc_exec calls (e.g. zwcad.read.state)"},
                                    "connector": {"type": "string", "description": "Connector name for the output pipeline (e.g. zwcad)"},
                                    "pipeline_name": {"type": "string", "description": "Pipeline file name without extension (e.g. state)"},
                                    "mode": {"type": "string", "enum": ["read", "write"], "description": "Pipeline mode"},
                                    "target_profile": {"type": "string", "description": "Which exec profile's WAL to read", "default": "default"},
                                },
                                "required": ["intent_signature", "connector", "pipeline_name", "mode"],
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
                        {
                            "uriTemplate": "state://goal",
                            "name": "Active goal snapshot",
                            "description": "Goal Control Plane active goal decision snapshot",
                            "mimeType": "application/json",
                        },
                        {
                            "uriTemplate": "state://goal-ledger",
                            "name": "Goal event ledger",
                            "description": "Append-only goal event and decision audit log",
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
            {
                "uri": "state://goal",
                "name": "Goal control snapshot",
                "mimeType": "application/json",
                "description": "Current active goal and decision metadata",
            },
            {
                "uri": "state://goal-ledger",
                "name": "Goal control ledger",
                "mimeType": "application/json",
                "description": "Recent goal events and decision outcomes",
            },
        ]
        # Collect connector names from both NOTES.md files and tracked pipeline entries
        # (single pass — avoids scanning _connector_roots twice)
        connector_names: set[str] = set()
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
            for notes in connector_root.glob("*/NOTES.md"):
                cname = notes.parent.name
                connector_names.add(cname)
                uri = f"connector://{cname}/notes"
                static.append({"uri": uri, "name": f"{cname} connector notes", "mimeType": "text/markdown", "description": f"Operational notes for the {cname} vertical: COM patterns, API quirks, known issues. Includes tracked intent_signature list."})
        # Add connectors that have flywheel tracking but no NOTES.md yet
        registry_path = self._state_root / "pipelines-registry.json"
        registry = self._load_json_object(registry_path, root_key="pipelines")
        for key in registry["pipelines"]:
            # Require well-formed <connector>.<mode>.<name> — single-segment or 2-part keys are orphans
            if _PIPELINE_KEY_RE.match(key):
                connector_names.add(key.split(".", 1)[0])
        already_noted = {r["uri"] for r in static}
        for cname in sorted(connector_names):
            uri = f"connector://{cname}/intents"
            static.append({"uri": uri, "name": f"{cname} tracked intents", "mimeType": "application/json", "description": f"JSON index of all flywheel-tracked intent_signature values for {cname}, with status and description"})
            notes_uri = f"connector://{cname}/notes"
            if notes_uri not in already_noted:
                # Connector has tracked entries but no NOTES.md — expose notes so CC can read intents table
                static.append({"uri": notes_uri, "name": f"{cname} connector notes", "mimeType": "text/markdown", "description": f"Tracked intents for {cname} connector (no NOTES.md yet)."})
        return static

    def _read_resource(self, uri: str) -> dict[str, Any]:
        if uri == "policy://current":
            path = self._state_root / "pipelines-registry.json"
            data = self._load_json_object(path, root_key="pipelines")
            return {"uri": uri, "mimeType": "application/json", "text": json.dumps(data)}
        if uri == "runner://status":
            router = RunnerRouter.from_env()
            summary = router.health_summary() if router else {"configured": False, "any_reachable": False}
            return {"uri": uri, "mimeType": "application/json", "text": json.dumps(summary)}
        if uri == "state://deltas":
            from scripts.state_tracker import load_tracker
            state_path = self._hook_state_path()
            tracker = load_tracker(state_path)
            data = tracker.to_dict()
            snapshot = self._goal_control.read_snapshot()
            data["goal"] = snapshot.get("text", "")
            data["goal_source"] = snapshot.get("source", "unset")
            data["goal_version"] = snapshot.get("version", 0)
            return {"uri": uri, "mimeType": "application/json", "text": json.dumps(data)}
        if uri == "state://goal":
            snapshot = self._goal_control.read_snapshot()
            return {"uri": uri, "mimeType": "application/json", "text": json.dumps(snapshot)}
        if uri == "state://goal-ledger":
            rows = self._goal_control.read_ledger(limit=500)
            return {"uri": uri, "mimeType": "application/json", "text": json.dumps({"events": rows})}
        if uri.startswith("pipeline://"):
            rest = uri[len("pipeline://"):]
            parts = rest.split("/", 2)
            if len(parts) == 3:
                connector, mode, name = parts
                # Reject any path traversal in URI components
                if any(".." in p or p.startswith("/") for p in (connector, mode, name)):
                    raise KeyError(f"Resource not found: {uri}")
                for connector_root in self.pipeline._connector_roots:
                    meta = connector_root / connector / "pipelines" / mode / f"{name}.yaml"
                    # Confirm resolved path stays within the connector_root
                    try:
                        meta.resolve().relative_to(connector_root.resolve())
                    except ValueError:
                        continue
                    if meta.exists():
                        data = PipelineEngine._load_metadata(meta)
                        return {"uri": uri, "mimeType": "application/json", "text": json.dumps(data)}
        if uri.startswith("connector://"):
            rest = uri[len("connector://"):]
            parts = rest.split("/", 1)
            if len(parts) == 2:
                connector, resource = parts
                # Validate connector name: lowercase alphanumeric + _ - only, no path chars
                if not re.match(r"^[a-z0-9][a-z0-9_-]*$", connector):
                    raise KeyError(f"Resource not found: {uri}")
                if resource == "notes":
                    notes_text = ""
                    for connector_root in self.pipeline._connector_roots:
                        notes = connector_root / connector / "NOTES.md"
                        try:
                            notes.resolve().relative_to(connector_root.resolve())
                        except ValueError:
                            continue
                        if notes.exists():
                            notes_text = notes.read_text(encoding="utf-8")
                            break
                    intents_section = self._build_intents_section(connector)
                    if intents_section:
                        notes_text = (notes_text.rstrip() + "\n\n" + intents_section).lstrip()
                    if notes_text:
                        return {"uri": uri, "mimeType": "text/markdown", "text": notes_text}
                    raise KeyError(f"Resource not found: {uri}")
                if resource == "intents":
                    data = self._get_connector_intents(connector)
                    return {"uri": uri, "mimeType": "application/json", "text": json.dumps(data)}
        raise KeyError(f"Resource not found: {uri}")

    def _get_connector_intents(self, connector: str) -> dict[str, Any]:
        """Return all tracked intent entries for a connector from pipelines-registry.json.

        source='pipeline' means a crystallized pipeline exists (icc_read/write path).
        source='exec' means only icc_exec tracking so far.
        """
        registry_path = self._state_root / "pipelines-registry.json"
        registry = self._load_json_object(registry_path, root_key="pipelines")
        prefix = f"{connector}."
        result: dict[str, Any] = {}
        for key, pipeline in registry["pipelines"].items():
            if key.startswith(prefix):
                result[key] = {
                    "status": pipeline.get("status", "explore"),
                    "success_rate": pipeline.get("success_rate", 0.0),
                    "verify_rate": pipeline.get("verify_rate", 0.0),
                    "attempts": pipeline.get("attempts_at_transition", 0),
                    "description": pipeline.get("description", ""),
                    "source": pipeline.get("source", "exec"),
                }
        return result

    def _build_intents_section(self, connector: str) -> str:
        """Build a markdown table of tracked intents for injection into connector://notes."""
        intents = self._get_connector_intents(connector)
        if not intents:
            return ""
        status_icon = {"stable": "✓", "canary": "⟳", "explore": "…"}
        rows = []
        for key in sorted(intents):
            info = intents[key]
            icon = status_icon.get(info["status"], "?")
            success_pct = f"{info['success_rate'] * 100:.0f}%"
            desc = info["description"] or ""
            # Show whether intent has a crystallized pipeline or is still exec-only
            path = "`icc_read/write`" if info["source"] == "pipeline" else "`icc_exec`"
            rows.append(f"| `{key}` | {info['status']} {icon} | {success_pct} | {path} | {desc} |")
        header = (
            "---\n"
            "## Tracked Intents (Emerge flywheel)\n"
            "- Intents with `icc_read/write` path are crystallized pipelines — use `icc_read`/`icc_write`, NOT `icc_exec`.\n"
            "- Intents with `icc_exec` path are still in explore/canary — use `icc_exec` with the exact `intent_signature`.\n"
            "- Do NOT invent new intent names — pick from this list whenever the intent matches.\n\n"
            "| Intent | Status | Success | Path | Description |\n"
            "|--------|--------|---------|------|-------------|"
        )
        return header + "\n" + "\n".join(rows)

    _PROMPTS = [
        {
            "name": "icc_explore",
            "description": "Explore a new vertical using icc_exec with policy tracking",
            "arguments": [
                {"name": "vertical", "description": "Name of the vertical (e.g. zwcad)", "required": True},
                {"name": "goal", "description": "What to explore", "required": False},
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
        raise KeyError(f"Prompt not found: {name}")

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

    def _record_exec_event(
        self,
        *,
        arguments: dict[str, Any],
        result: dict[str, Any],
        target_profile: str,
        mode: str,
        execution_path: str,
        sampled_in_policy: bool,
        candidate_key: str,
    ) -> None:
        is_error = bool(result.get("isError"))
        intent_signature = str(arguments.get("intent_signature", ""))
        script_ref = str(arguments.get("script_ref", ""))
        base_pipeline_id = str(arguments.get("base_pipeline_id", "")).strip()
        description = str(arguments.get("description", "")).strip()
        trusted_verify_passed = not is_error
        event = {
            "ts_ms": int(time.time() * 1000),
            "source": "exec",
            "mode": mode,
            "target_profile": target_profile,
            "intent_signature": intent_signature,
            "script_ref": script_ref,
            "base_pipeline_id": base_pipeline_id,
            "verify_passed": trusted_verify_passed,
            "human_fix": False,  # incremented via icc_reconcile(outcome=correct), not at execution time
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
                "last_execution_path": execution_path,
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
        # Store description if provided (first call wins; subsequent calls with description update it)
        if description:
            entry["description"] = description
        entry["last_execution_path"] = execution_path
        entry["total_calls"] = int(entry.get("total_calls", 0)) + 1
        if is_error:
            sampled_in_policy = True
        if sampled_in_policy:
            entry["attempts"] += 1
            if not is_error:
                entry["successes"] += 1
            if trusted_verify_passed:
                entry["verify_passes"] += 1
            # human_fixes incremented via _increment_human_fix() on icc_reconcile(outcome=correct)
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
        self._update_pipeline_registry(candidate_key=key, entry=entry)

    def _record_pipeline_event(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
        is_error: bool,
        error_text: str = "",
        execution_path: str = "local",
    ) -> None:
        session_dir = self._state_root / self._base_session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        connector = str(arguments.get("connector", ""))
        mode = "read" if tool_name == "icc_read" else "write"
        pipeline = str(arguments.get("pipeline", ""))
        pipeline_id = str(result.get("pipeline_id", f"{connector}.{mode}.{pipeline}"))
        intent_signature = str(result.get("intent_signature", ""))
        target_profile = str(arguments.get("target_profile", "default"))
        # Read description from pipeline YAML if available
        pipeline_description = ""
        for _cr in self.pipeline._connector_roots:
            _meta = _cr / connector / "pipelines" / mode / f"{pipeline}.yaml"
            if _meta.exists():
                try:
                    _data = PipelineEngine._load_metadata(_meta)
                    pipeline_description = str(_data.get("description", "")).strip()
                except Exception:
                    pass
                break
        verify_passed = str(result.get("verification_state", "")).lower() == "verified"
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
            "human_fix": False,  # incremented via icc_reconcile(outcome=correct), not at execution time
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
                "source": "pipeline",
                "pipeline_id": pipeline_id,
                "target_profile": target_profile,
                "last_execution_path": execution_path,
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
        # Pipeline path is authoritative: always mark source=pipeline so
        # synthesis_ready is never set for an already-crystallized intent.
        entry["source"] = "pipeline"
        entry["last_execution_path"] = execution_path
        # Store description from YAML (never overwrite existing)
        if pipeline_description and not entry.get("description"):
            entry["description"] = pipeline_description
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
            # human_fixes incremented via _increment_human_fix() on icc_reconcile(outcome=correct)
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
        self._update_pipeline_registry(candidate_key=key, entry=entry)

    def _update_pipeline_registry(
        self,
        *,
        candidate_key: str,
        entry: dict[str, Any],
    ) -> None:
        if not _PIPELINE_KEY_RE.match(candidate_key):
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "Refusing to register malformed pipeline key %r — must match <connector>.(read|write).<name>",
                candidate_key,
            )
            return
        registry_path = self._state_root / "pipelines-registry.json"
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
        # Propagate description from candidate entry into pipeline registry (if set, never cleared)
        if entry.get("description") and not pipeline.get("description"):
            pipeline["description"] = entry["description"]
        # Propagate source: 'pipeline' once set takes precedence over 'exec'
        if entry.get("source") == "pipeline" or pipeline.get("source") != "pipeline":
            pipeline["source"] = entry.get("source", "exec")

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
                # Signal that this exec candidate can be crystallized into a pipeline
                intent_sig = entry.get("intent_signature", "")
                if intent_sig and entry.get("source") == "exec":
                    if self._has_synthesizable_wal_entry(intent_sig, entry.get("target_profile", "default")):
                        pipeline["synthesis_ready"] = True
                        try:
                            self._sink.emit(
                                "policy.synthesis_ready",
                                {
                                    "candidate_key": candidate_key,
                                    "intent_signature": intent_sig,
                                    "session_id": self._base_session_id,
                                },
                            )
                        except Exception:
                            pass
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
        pipeline["last_execution_path"] = str(entry.get("last_execution_path", "unknown"))
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

    def _run_pipeline_remotely(
        self,
        mode: str,
        arguments: dict[str, Any],
        client: Any,
    ) -> dict[str, Any]:
        """Execute a pipeline on a remote runner as inline icc_exec code.

        The daemon loads connector assets (YAML + .py) locally, builds a
        self-contained exec payload, sends it to the runner via icc_exec, and
        assembles the response in the same format as PipelineEngine.run_read/write.
        The runner stays a pure Python executor — it never needs connector files.
        """
        connector = str(arguments.get("connector", "")).strip()
        pipeline_name = str(arguments.get("pipeline", "")).strip()
        target_profile = str(arguments.get("target_profile", "default")).strip()

        # Raises PipelineMissingError if not found locally — propagates to structured hint.
        metadata, py_source = self.pipeline._load_pipeline_source(connector, mode, pipeline_name)

        # Strip `from __future__` lines — they are only valid as the first statement of a
        # module, and exec() raises SyntaxError when they appear mid-string.
        py_source = "\n".join(
            line for line in py_source.splitlines()
            if not line.strip().startswith("from __future__")
        )

        meta_repr = repr(json.dumps(metadata, ensure_ascii=True))
        args_repr = repr(json.dumps(arguments, ensure_ascii=True))

        if mode == "read":
            dispatch = (
                "_rows = run_read(metadata=_m, args=_a)\n"
                "_vfn = globals().get('verify_read')\n"
                "_v = _vfn(metadata=_m, args=_a, rows=_rows) if callable(_vfn) else {'ok': True}\n"
                "_out = {'rows': _rows, 'verify': _v}\n"
            )
        else:
            dispatch = (
                "_act = run_write(metadata=_m, args=_a)\n"
                "_vfn = globals().get('verify_write')\n"
                "if not callable(_vfn): raise ValueError('verify_write is required')\n"
                "_v = _vfn(metadata=_m, args=_a, action_result=_act)\n"
                "_ok = bool(_v.get('ok', False))\n"
                "_pol = _m.get('rollback_or_stop_policy', 'stop')\n"
                "_rb, _rr, _st = False, None, False\n"
                "if not _ok:\n"
                "    if _pol == 'rollback':\n"
                "        _rfn = globals().get('rollback_write')\n"
                "        if callable(_rfn):\n"
                "            try:\n"
                "                _rr = _rfn(metadata=_m, args=_a, action_result=_act)\n"
                "                if not isinstance(_rr, dict): _rr = {'ok': False, 'error': 'must return object'}\n"
                "            except Exception as _re: _rr = {'ok': False, 'error': str(_re)}\n"
                "            _rb = True\n"
                "        else:\n"
                "            _rr = {'ok': False, 'error': 'rollback_write not implemented'}; _st = True\n"
                "    else:\n"
                "        _st = True\n"
                "_out = {'action_result': _act, 'verify': _v, 'rollback_executed': _rb, 'rollback_result': _rr, 'stop_triggered': _st}\n"
            )

        result_var = "__emerge_pipeline_out"
        exec_code = (
            "import json as _j\n"
            f"_m = _j.loads({meta_repr})\n"
            f"_a = _j.loads({args_repr})\n"
            f"{py_source}\n"
            f"{dispatch}"
            f"{result_var} = _out\n"
        )

        exec_result = client.call_tool("icc_exec", {
            "code": exec_code,
            "no_replay": True,
            "target_profile": target_profile,
            "result_var": result_var,
        })

        if exec_result.get("isError"):
            text = str(exec_result.get("content", [{}])[0].get("text", "remote pipeline exec failed"))
            raise RuntimeError(text)

        output = exec_result.get("result_var_value")
        if not isinstance(output, dict):
            result_err = str(exec_result.get("result_var_error", "")).strip()
            fallback_text = str(exec_result.get("content", [{}])[0].get("text", "")).strip()
            detail = result_err or fallback_text or "missing result_var_value"
            raise RuntimeError(f"remote pipeline exec missing structured output: {detail}")

        if mode == "read":
            rows = output.get("rows", [])
            verify = output.get("verify", {"ok": True})
            return PipelineEngine._build_read_result(
                connector=connector,
                pipeline=pipeline_name,
                metadata=metadata,
                rows=rows,
                verify_result=verify,
            )
        else:
            act = output.get("action_result", {})
            verify = output.get("verify", {})
            rb = bool(output.get("rollback_executed", False))
            st = bool(output.get("stop_triggered", False))
            rr = output.get("rollback_result")
            return PipelineEngine._build_write_result(
                connector=connector,
                pipeline=pipeline_name,
                metadata=metadata,
                action_result=act,
                verify_result=verify,
                stop_triggered=st,
                rollback_executed=rb,
                rollback_result=rr,
            )

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

    def _increment_human_fix(self, intent_signature: str) -> None:
        """Increment human_fixes for the candidate keyed by intent_signature.

        With unified intent-based keys there is exactly one entry per intent — direct lookup.
        """
        session_dir = self._state_root / self._base_session_id
        candidates_path = session_dir / "candidates.json"
        if not candidates_path.exists():
            return
        registry = self._load_json_object(candidates_path, root_key="candidates")
        entry = registry["candidates"].get(intent_signature)
        if not isinstance(entry, dict):
            return
        entry["human_fixes"] = int(entry.get("human_fixes", 0)) + 1
        registry["candidates"][intent_signature] = entry
        self._atomic_write_json(candidates_path, registry)
        try:
            self._update_pipeline_registry(candidate_key=intent_signature, entry=entry)
        except Exception:
            pass

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
    def _resolve_exec_candidate_key(*, arguments: dict[str, Any], target_profile: str) -> str:
        """Key is intent_signature — runner and script are execution metadata, not identity.

        Returns the key if it matches the canonical format, otherwise returns an empty string
        so that _update_pipeline_registry rejects it at write time.
        """
        key = str(arguments.get("intent_signature", "")).strip()
        if key and not _PIPELINE_KEY_RE.match(key):
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "icc_exec: intent_signature %r does not match <connector>.(read|write).<name> — "
                "execution will proceed but telemetry will NOT be registered",
                key,
            )
        return key

    @staticmethod
    def _resolve_pipeline_candidate_key(*, arguments: dict[str, Any], pipeline_id: str) -> str:
        """Key is pipeline_id (= intent by convention: <connector>.<mode>.<op>)."""
        return pipeline_id

    def _should_sample(self, candidate_key: str) -> bool:
        if not candidate_key:
            return True
        path = self._state_root / "pipelines-registry.json"
        if not path.exists():
            return True
        data = self._load_json_object(path, root_key="pipelines")
        pipeline = data.get("pipelines", {}).get(candidate_key)
        if not isinstance(pipeline, dict):
            return True
        status = str(pipeline.get("status", "explore"))
        if status != "canary":
            return True
        rollout_pct = self._as_int(pipeline.get("rollout_pct", 0), 0)
        rollout_pct = max(0, min(100, rollout_pct))
        if rollout_pct <= 0:
            return False

        candidates_path = (self._state_root / self._base_session_id) / "candidates.json"
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
        # Append as a separate content item so existing JSON text fields are not corrupted.
        # content[0]["text"] may already be json.dumps(...); appending plaintext there breaks
        # json.loads() for any downstream consumer.
        content = result.get("content")
        if isinstance(content, list):
            content.append({"type": "text", "text": f"warning:\n{warning}"})
            return
        result["content"] = [{"type": "text", "text": f"warning:\n{warning}"}]

    def _has_synthesizable_wal_entry(self, intent_signature: str, target_profile: str = "default") -> bool:
        """Return True if any session WAL for the given profile has at least one success entry
        with no_replay=False for the given intent_signature.

        Scans ALL session dirs (not just current) so sessions from previous restarts are included.
        """
        if not intent_signature:
            return False
        normalized = (target_profile or "default").strip() or "default"
        profile_suffix = "" if normalized == "default" else f"__{derive_profile_token(normalized)}"

        if not self._state_root.exists():
            return False
        try:
            session_dirs = list(self._state_root.iterdir())
        except OSError:
            return False

        for session_dir in session_dirs:
            if not session_dir.is_dir():
                continue
            dir_name = session_dir.name
            if profile_suffix:
                if not dir_name.endswith(profile_suffix):
                    continue
            else:
                if "__" in dir_name:
                    continue
            wal_path = session_dir / "wal.jsonl"
            if not wal_path.exists():
                continue
            try:
                with wal_path.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if (
                            entry.get("status") == "success"
                            and not entry.get("no_replay", False)
                            and entry.get("metadata", {}).get("intent_signature") == intent_signature
                        ):
                            return True
            except OSError:
                continue
        return False

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

    # ------------------------------------------------------------------
    # OperatorMonitor lifecycle
    # ------------------------------------------------------------------

    def start_operator_monitor(self) -> None:
        """Start OperatorMonitor if EMERGE_OPERATOR_MONITOR=1 and not already running."""
        if os.environ.get("EMERGE_OPERATOR_MONITOR", "0") != "1":
            return
        if self._operator_monitor is not None and self._operator_monitor.is_alive():
            return
        from scripts.operator_monitor import OperatorMonitor

        poll_s = float(os.environ.get("EMERGE_MONITOR_POLL_S", "5"))
        machines_env = os.environ.get("EMERGE_MONITOR_MACHINES", "")

        machines: dict = {}
        _rr = self._get_runner_router()
        if _rr:
            for profile_name in (machines_env.split(",") if machines_env else ["default"]):
                profile_name = profile_name.strip()
                if not profile_name:
                    continue
                client = _rr.find_client({"target_profile": profile_name})
                if client is not None:
                    machines[profile_name] = _RunnerClientAdapter(
                        base_url=client.base_url,
                        timeout_s=min(client.timeout_s, 10.0),
                    )

        self._operator_monitor = OperatorMonitor(
            machines=machines,
            push_fn=self._push_pattern,
            poll_interval_s=poll_s,
            event_root=Path.home() / ".emerge" / "operator-events",
            adapter_root=Path.home() / ".emerge" / "adapters",
        )
        self._operator_monitor.start()

    def stop_operator_monitor(self) -> None:
        if self._operator_monitor is not None:
            self._operator_monitor.stop()

    def start_pending_monitor(self) -> None:
        if os.environ.get("EMERGE_COCKPIT_DISABLE", "0") == "1":
            return
        if self._pending_monitor is not None and self._pending_monitor.is_alive():
            return
        self._pending_monitor = PendingActionMonitor(
            state_root=self._state_root,
            write_push_fn=self._write_mcp_push,
        )
        self._pending_monitor.start()

    def stop_pending_monitor(self) -> None:
        if self._pending_monitor is not None:
            self._pending_monitor.stop()

    def _push_pattern(self, stage: str, context: dict, summary: Any) -> None:
        """Push pattern detection result to CC via MCP channel notification.

        CC reads policy_stage from meta and decides whether to engage the operator
        (via icc_exec → show_notify) or crystallize directly. Daemon never pops up.
        """
        message = self._build_explore_message(context, summary)
        self._write_mcp_push({
            "jsonrpc": "2.0",
            "method": "notifications/claude/channel",
            "params": {
                "serverName": "emerge",
                "content": message,
                "meta": {
                    "source": "operator_monitor",
                    "intent_signature": summary.intent_signature,
                    "policy_stage": stage,
                    "occurrences": summary.occurrences,
                    "window_minutes": summary.window_minutes,
                    "machine_ids": summary.machine_ids,
                },
            },
        })

    def _build_explore_message(self, context: dict, summary: Any) -> str:
        app = context.get("app", "unknown")
        samples = context.get("samples", summary.context_hint.get("samples", []))
        sig = summary.intent_signature
        return (
            f"[OperatorMonitor] 检测到 {app} 中反复出现操作模式 `{sig}`，"
            f"共 {summary.occurrences} 次，约 {summary.window_minutes:.0f} 分钟内。"
            + (f" 样本: {', '.join(str(s) for s in samples[:3])}。" if samples else "")
            + " 请评估是否值得接管，如值得请发起 elicitation。"
        )

    def _write_mcp_push(self, payload: dict) -> None:
        """Write a JSON-RPC notification/request to stdout for CC to receive."""
        line = json.dumps(payload) + "\n"
        with _stdout_lock:
            sys.stdout.write(line)
            sys.stdout.flush()


def _format_pending_actions_message(actions: list) -> str:
    lines = ["[Cockpit] 用户提交了以下操作，请依次执行："]
    for i, a in enumerate(actions, 1):
        t = a.get("type", "unknown")
        if t == "pipeline-set":
            lines.append(f"{i}. pipeline-set {a.get('key')} fields={a.get('fields', {})}")
        elif t == "pipeline-delete":
            lines.append(f"{i}. pipeline-delete {a.get('key')}")
        elif t == "notes-edit":
            lines.append(f"{i}. 更新 {a.get('connector')} NOTES.md（全文替换）")
        elif t == "notes-comment":
            lines.append(f"{i}. 追加 comment 到 {a.get('connector')} NOTES.md: {str(a.get('comment', ''))[:80]}")
        elif t == "scenario-run":
            lines.append(f"{i}. 运行 scenario {a.get('scenario')} (connector: {a.get('connector')}) args={a.get('args', {})}")
        elif t == "crystallize-component":
            lines.append(f"{i}. 固化组件 {a.get('filename')} → {a.get('connector')}/cockpit/")
        else:
            lines.append(f"{i}. {t}: {a}")
    return "\n".join(lines)


class PendingActionMonitor(threading.Thread):
    """Polls `<state_root>/pending-actions.json` every 2s.
    When a new submission is detected, fires a notifications/claude/channel
    push so CC processes the pending actions via subagents.
    """

    def __init__(self, state_root: Path, write_push_fn) -> None:
        super().__init__(daemon=True, name="PendingActionMonitor")
        self._state_root = state_root
        self._write_push_fn = write_push_fn
        self._stop_event = threading.Event()
        self._last_seen_ts: int = 0

    def _check_once(self, pending_path: Path) -> None:
        if not pending_path.exists():
            return
        try:
            text = pending_path.read_text(encoding="utf-8")
        except OSError:
            return
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            import sys
            print(f"[PendingActionMonitor] malformed JSON in {pending_path}; quarantining", file=sys.stderr)
            try:
                pending_path.rename(self._state_root / "pending-actions.invalid.json")
            except OSError:
                pass
            return
        ts = int(data.get("submitted_at", 0))
        if ts <= self._last_seen_ts:
            return
        actions = data.get("actions", [])
        try:
            self._write_push_fn({
                "jsonrpc": "2.0",
                "method": "notifications/claude/channel",
                "params": {
                    "serverName": "emerge",
                    "content": _format_pending_actions_message(actions),
                    "meta": {
                        "source": "cockpit",
                        "action_count": len(actions),
                        "action_types": list({a.get("type") for a in actions}),
                    },
                },
            })
        except Exception:
            import sys, traceback
            traceback.print_exc(file=sys.stderr)
            return  # don't advance _last_seen_ts — allow retry
        try:
            processed = self._state_root / "pending-actions.processed.json"
            pending_path.rename(processed)
            self._last_seen_ts = ts  # advance only after successful rename
        except OSError:
            import sys
            print(f"[PendingActionMonitor] failed to rename {pending_path}", file=sys.stderr)

    def run(self) -> None:
        pending_path = self._state_root / "pending-actions.json"
        while not self._stop_event.wait(2.0):
            self._check_once(pending_path)
        self._check_once(pending_path)  # drain on stop

    def stop(self) -> None:
        self._stop_event.set()


def run_stdio() -> None:
    import atexit
    daemon = EmergeDaemon()
    daemon.start_operator_monitor()
    atexit.register(daemon.stop_operator_monitor)
    daemon.start_pending_monitor()
    atexit.register(daemon.stop_pending_monitor)
    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue
        try:
            req = json.loads(text)
        except json.JSONDecodeError as exc:  # pragma: no cover
            resp = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc}"},
            }
            if resp is not None:
                with _stdout_lock:
                    sys.stdout.write(json.dumps(resp) + "\n")
                    sys.stdout.flush()
            continue
        try:
            resp = daemon.handle_jsonrpc(req)
        except Exception as exc:  # pragma: no cover
            resp = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32603, "message": str(exc)},
            }
        if resp is not None:
            with _stdout_lock:
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()


if __name__ == "__main__":
    run_stdio()
