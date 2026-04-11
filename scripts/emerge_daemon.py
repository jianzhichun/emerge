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
    truncate_jsonl_if_needed,
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
        self._last_seen_pending_ts: int = 0
        self._elicit_events: dict[str, threading.Event] = {}
        self._elicit_results: dict[str, dict] = {}
        self._goal_control = GoalControlPlane(Path(default_hook_state_root()))
        self._goal_control.ensure_initialized()
        self._migrate_legacy_goal_once()
        from scripts.span_tracker import SpanTracker
        _hook_state_root = Path(default_hook_state_root())
        self._span_tracker = SpanTracker(
            state_root=self._state_root,
            hook_state_root=_hook_state_root,
        )
        self._open_spans: dict[str, Any] = {}  # span_id → SpanRecord; in-process cache

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
        except Exception as _bridge_exc:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "flywheel bridge failed for %s (%s), falling back to LLM: %s",
                base_pipeline_id, mode, _bridge_exc,
            )
            # Increment consecutive_failures directly in the registry so the policy engine
            # can downgrade stable→explore if the bridge keeps failing, without polluting
            # the recent_outcomes window (which would cause spurious window-failure downgrades).
            try:
                _reg_path = self._state_root / "pipelines-registry.json"
                with self._registry_lock:
                    _reg = self._load_json_object(_reg_path, root_key="pipelines")
                    _pe = _reg["pipelines"].get(base_pipeline_id)
                    if isinstance(_pe, dict):
                        _pe["consecutive_failures"] = int(_pe.get("consecutive_failures", 0)) + 1
                        _reg["pipelines"][base_pipeline_id] = _pe
                        self._atomic_write_json(_reg_path, _reg)
            except Exception:
                pass
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
        _cryst_payload = {
            "ok": True,
            "py_path": str(py_path),
            "yaml_path": str(yaml_path),
            "code_preview": code_preview,
            "next_step": next_step,
        }
        return {
            "isError": False,
            "structuredContent": _cryst_payload,
            "content": [{"type": "text", "text": json.dumps(_cryst_payload)}],
        }

    def _auto_crystallize(
        self,
        *,
        intent_signature: str,
        connector: str,
        pipeline_name: str,
        mode: str,
        target_profile: str = "default",
    ) -> None:
        """Auto-crystallize icc_exec WAL at synthesis_ready.

        Silently skips if pipeline file already exists (human-authored wins).
        Silently skips if no synthesizable WAL entry found.
        Never raises — failures are swallowed to avoid disrupting policy bookkeeping.
        """
        from scripts.pipeline_engine import _USER_CONNECTOR_ROOT
        try:
            env_root_str = os.environ.get("EMERGE_CONNECTOR_ROOT", "").strip()
            target_root = Path(env_root_str).expanduser() if env_root_str else _USER_CONNECTOR_ROOT
            pipeline_dir = target_root / connector / "pipelines" / mode
            py_path = pipeline_dir / f"{pipeline_name}.py"
            if py_path.exists():
                return  # never overwrite existing file
            self._crystallize(
                intent_signature=intent_signature,
                connector=connector,
                pipeline_name=pipeline_name,
                mode=mode,
                target_profile=target_profile,
            )
        except Exception:
            pass  # auto-crystallize is best-effort

    def _generate_span_skeleton(
        self,
        *,
        intent_signature: str,
        span: dict,
        connector_root: "Path | None" = None,
    ) -> "Path | None":
        """Generate a Python skeleton for a stable span.

        Writes to connectors/<connector>/pipelines/<mode>/_pending/<name>.py.
        Returns the path written, or None on failure.
        Silently skips if skeleton already exists.
        """
        import textwrap
        from scripts.pipeline_engine import _USER_CONNECTOR_ROOT
        try:
            parts = intent_signature.split(".", 2)
            if len(parts) != 3:
                return None
            connector, mode, pipeline_name = parts
            env_root_str = os.environ.get("EMERGE_CONNECTOR_ROOT", "").strip()
            target_root = connector_root or (
                Path(env_root_str).expanduser() if env_root_str else _USER_CONNECTOR_ROOT
            )
            pending_dir = target_root / connector / "pipelines" / mode / "_pending"
            pending_dir.mkdir(parents=True, exist_ok=True)
            skeleton_path = pending_dir / f"{pipeline_name}.py"
            if skeleton_path.exists():
                return skeleton_path  # already generated

            actions = span.get("actions", [])
            is_read = mode == "read"
            call_lines = []
            for a in actions:
                tool = a.get("tool_name", "unknown_tool")
                call_lines.append(
                    f"    # seq={a.get('seq', '?')}: {tool} was called here\n"
                    f"    raise NotImplementedError('implement: {tool} equivalent')"
                )
            if not call_lines:
                call_lines = ["    raise NotImplementedError('implement pipeline body')"]
            body = "\n".join(call_lines)

            if is_read:
                skeleton = textwrap.dedent(f"""\
                    # auto-generated from span: {intent_signature}
                    # Review and implement before calling icc_span_approve.

                    def run_read(metadata, args):
                    {body}
                        return []  # return list of row dicts

                    def verify_read(metadata, args, rows):
                        return {{"ok": isinstance(rows, list)}}
                """)
            else:
                skeleton = textwrap.dedent(f"""\
                    # auto-generated from span: {intent_signature}
                    # Review and implement before calling icc_span_approve.
                    # verify_write is REQUIRED by PipelineEngine.

                    def run_write(metadata, args):
                    {body}
                        return {{"ok": True}}

                    def verify_write(metadata, args, action_result):
                        raise NotImplementedError('implement verify_write')

                    def rollback(metadata, args, action_result):
                        pass  # optional
                """)

            fd, tmp = tempfile.mkstemp(prefix=".skeleton-", dir=str(pending_dir))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(skeleton)
                os.replace(tmp, skeleton_path)
            except Exception:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
            return skeleton_path
        except Exception:
            return None

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
        if name == "icc_span_open":
            intent_signature = str(arguments.get("intent_signature", "")).strip()
            if not intent_signature:
                return self._tool_error("icc_span_open: 'intent_signature' is required")
            # Bridge check: stable policy AND pipeline file exists
            policy_status = self._span_tracker.get_policy_status(intent_signature)
            if policy_status == "stable":
                parts = intent_signature.split(".", 2)
                if len(parts) == 3:
                    connector, mode, pipeline_name = parts
                    pipeline_args = {**arguments, "connector": connector, "pipeline": pipeline_name}
                    try:
                        _rr = self._get_runner_router()
                        _client = _rr.find_client(arguments) if _rr else None
                        if _client is not None:
                            bridge_result = self._run_pipeline_remotely(mode, pipeline_args, _client)
                            _exec_path = "remote"
                        elif mode == "write":
                            bridge_result = self.pipeline.run_write(pipeline_args)
                            _exec_path = "local"
                        else:
                            bridge_result = self.pipeline.run_read(pipeline_args)
                            _exec_path = "local"
                        bridge_result["bridge_promoted"] = True
                        try:
                            self._record_pipeline_event(
                                tool_name="icc_span_open",
                                arguments=pipeline_args,
                                result=bridge_result,
                                is_error=False,
                                execution_path=_exec_path,
                            )
                        except Exception:
                            pass
                        try:
                            self._sink.emit("span.bridge.promoted", {"intent_signature": intent_signature})
                        except Exception:
                            pass
                        return self._tool_ok_json({
                            "bridge": True,
                            "bridge_type": "result",
                            "intent_signature": intent_signature,
                            "result": bridge_result,
                        })
                    except Exception:
                        pass  # PipelineMissingError or any failure → fall through to explore
            # No bridge: open a new span
            try:
                span = self._span_tracker.open_span(
                    intent_signature=intent_signature,
                    description=str(arguments.get("description", "")).strip(),
                    args=arguments.get("args") or {},
                    source=str(arguments.get("source", "manual")).strip(),
                    skill_name=str(arguments.get("skill_name", "") or "").strip() or None,
                )
            except RuntimeError as exc:
                return self._tool_error(str(exc))
            self._open_spans[span.span_id] = span
            return self._tool_ok_json({
                "span_id": span.span_id,
                "intent_signature": intent_signature,
                "status": "opened",
                "policy_status": policy_status,
            })

        if name == "icc_span_close":
            outcome = str(arguments.get("outcome", "")).strip()
            if outcome not in ("success", "failure", "aborted"):
                return self._tool_error(
                    f"icc_span_close: 'outcome' must be success/failure/aborted, got {outcome!r}"
                )
            span_id = str(arguments.get("span_id", "")).strip()
            result_summary = arguments.get("result_summary") or {}
            # Retrieve in-process span (may be absent after daemon restart)
            from scripts.span_tracker import SpanRecord
            span = self._open_spans.pop(span_id, None)
            if span is None:
                # Graceful fallback: reconstruct minimal span so WAL still gets a record
                import uuid as _uuid
                span = SpanRecord(
                    span_id=span_id or str(_uuid.uuid4()),
                    intent_signature=str(arguments.get("intent_signature", "")).strip(),
                    description="",
                    source="manual",
                    opened_at_ms=0,
                )
            closed = self._span_tracker.close_span(span, outcome=outcome, result_summary=result_summary)
            policy_status = self._span_tracker.get_policy_status(closed.intent_signature)
            synthesis_ready = self._span_tracker.is_synthesis_ready(closed.intent_signature)
            skeleton_path: str | None = None
            # Auto-generate skeleton for stable spans (once only)
            if synthesis_ready and not self._span_tracker.skeleton_already_generated(closed.intent_signature):
                latest = self._span_tracker.latest_successful_span(closed.intent_signature)
                if latest:
                    generated = self._generate_span_skeleton(
                        intent_signature=closed.intent_signature,
                        span=latest,
                    )
                    if generated:
                        skeleton_path = str(generated)
                        self._span_tracker.mark_skeleton_generated(closed.intent_signature)
                        try:
                            self._sink.emit("span.skeleton_generated", {
                                "intent_signature": closed.intent_signature,
                                "path": skeleton_path,
                            })
                        except Exception:
                            pass
            response: dict[str, Any] = {
                "span_id": closed.span_id,
                "intent_signature": closed.intent_signature,
                "outcome": outcome,
                "policy_status": policy_status,
                "synthesis_ready": synthesis_ready,
                "is_read_only": closed.is_read_only,
            }
            if skeleton_path:
                response["skeleton_path"] = skeleton_path
                response["next_step"] = (
                    f"Review and complete {skeleton_path}, "
                    "then call icc_span_approve to activate the bridge."
                )
            return self._tool_ok_json(response)

        if name == "icc_span_approve":
            intent_signature = str(arguments.get("intent_signature", "")).strip()
            if not intent_signature:
                return self._tool_error("icc_span_approve: 'intent_signature' is required")
            policy_status = self._span_tracker.get_policy_status(intent_signature)
            if policy_status != "stable":
                return self._tool_error(
                    f"icc_span_approve: intent '{intent_signature}' is not stable "
                    f"(status={policy_status}). Only stable spans can be approved."
                )
            parts = intent_signature.split(".", 2)
            if len(parts) != 3:
                return self._tool_error(
                    f"icc_span_approve: cannot parse connector/mode/name from '{intent_signature}'"
                )
            connector, mode, pipeline_name = parts
            from scripts.pipeline_engine import _USER_CONNECTOR_ROOT
            env_root_str = os.environ.get("EMERGE_CONNECTOR_ROOT", "").strip()
            target_root = Path(env_root_str).expanduser() if env_root_str else _USER_CONNECTOR_ROOT
            pending_py = target_root / connector / "pipelines" / mode / "_pending" / f"{pipeline_name}.py"
            if not pending_py.exists():
                _msg = (
                    f"icc_span_approve: skeleton not found at {pending_py}. "
                    "Run icc_span_close to generate the skeleton first, then implement it before "
                    "approving. Check _pending/ directory."
                )
                return {"isError": True, "content": [{"type": "text", "text": json.dumps({"message": _msg})}]}
            # Move .py to real pipeline directory
            real_dir = target_root / connector / "pipelines" / mode
            real_dir.mkdir(parents=True, exist_ok=True)
            real_py = real_dir / f"{pipeline_name}.py"
            real_yaml = real_dir / f"{pipeline_name}.yaml"
            # Ask user to confirm before activating the pipeline
            elicit_resp = self._elicit(
                f"确认激活 pipeline `{intent_signature}`？\n"
                f"将从 _pending/ 移动到 {real_dir} 并启用桥接。",
                {
                    "type": "object",
                    "properties": {"confirmed": {"type": "boolean", "title": "激活"}},
                    "required": ["confirmed"],
                },
            )
            if elicit_resp is None:
                return self._tool_error(
                    "icc_span_approve: elicitation timed out — operation cancelled"
                )
            if not elicit_resp.get("confirmed"):
                return self._tool_ok_json({
                    "approved": False,
                    "cancelled": True,
                    "message": "icc_span_approve cancelled by user.",
                })
            # Atomic move: write to temp in target dir, then replace
            fd, tmp_py = tempfile.mkstemp(prefix=".approve-", dir=str(real_dir))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(pending_py.read_text(encoding="utf-8"))
                os.replace(tmp_py, real_py)
            except Exception as exc:
                if os.path.exists(tmp_py):
                    os.unlink(tmp_py)
                return self._tool_error(f"icc_span_approve: failed to move skeleton: {exc}")
            pending_py.unlink(missing_ok=True)
            # Generate minimal YAML metadata
            mode_step_key = "read_steps" if mode == "read" else "write_steps"
            mode_step_val = "run_read" if mode == "read" else "run_write"
            verify_step_val = "verify_read" if mode == "read" else "verify_write"
            yaml_data: dict[str, Any] = {
                "intent_signature": intent_signature,
                "rollback_or_stop_policy": "stop",
                mode_step_key: [mode_step_val],
                "verify_steps": [verify_step_val],
                "span_approved": True,
            }
            try:
                yaml_src = _IndentedSafeDumper.dump_yaml(yaml_data)
                fd2, tmp_yaml = tempfile.mkstemp(prefix=".approve-yaml-", dir=str(real_dir))
                try:
                    with os.fdopen(fd2, "w", encoding="utf-8") as f:
                        f.write(yaml_src)
                    os.replace(tmp_yaml, real_yaml)
                except Exception:
                    if os.path.exists(tmp_yaml):
                        os.unlink(tmp_yaml)
                    raise
            except Exception as exc:
                return self._tool_error(f"icc_span_approve: failed to generate YAML: {exc}")
            try:
                self._sink.emit("span.approved", {"intent_signature": intent_signature})
            except Exception:
                pass
            return self._tool_ok_json({
                "approved": True,
                "intent_signature": intent_signature,
                "pipeline_path": str(real_py),
                "yaml_path": str(real_yaml),
                "bridge_active": True,
                "message": (
                    f"Pipeline activated at {real_py}. "
                    "Future icc_span_open calls will bridge directly to this pipeline."
                ),
            })

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
                # outcome not supplied — ask via ElicitRequest
                elicit_resp = self._elicit(
                    f"请选择 delta `{delta_id}` 的处置结果：",
                    {
                        "type": "object",
                        "properties": {
                            "outcome": {
                                "type": "string",
                                "enum": ["confirm", "correct", "retract"],
                                "title": "处置结果",
                            }
                        },
                        "required": ["outcome"],
                    },
                )
                if elicit_resp is None:
                    return self._tool_error(
                        "icc_reconcile: elicitation timed out — operation cancelled"
                    )
                outcome = str(elicit_resp.get("outcome", "")).strip()
                if outcome not in ("confirm", "correct", "retract"):
                    return self._tool_error(
                        f"icc_reconcile: invalid outcome from elicitation: {outcome!r}"
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
        if name == "icc_hub":
            return self._handle_icc_hub(arguments)
        return self._tool_error(f"Unknown tool: {name}")

    def _handle_icc_hub(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from scripts.hub_config import (
            append_sync_event,
            is_configured,
            load_hub_config,
            load_pending_conflicts,
            save_hub_config,
            save_pending_conflicts,
            sync_queue_path,
        )
        import time as _time

        action = str(arguments.get("action", "")).strip()

        if action == "list":
            cfg = load_hub_config()
            return self._tool_ok_json({
                "remote": cfg.get("remote", ""),
                "branch": cfg.get("branch", "emerge-hub"),
                "selected_verticals": cfg.get("selected_verticals", []),
                "poll_interval_seconds": cfg.get("poll_interval_seconds", 300),
                "configured": is_configured(),
            })

        if action == "add":
            connector = str(arguments.get("connector", "")).strip()
            if not connector:
                return self._tool_error("icc_hub add: 'connector' is required")
            cfg = load_hub_config()
            selected = list(cfg.get("selected_verticals", []))
            if connector not in selected:
                selected.append(connector)
                cfg["selected_verticals"] = selected
                save_hub_config(cfg)
            return self._tool_ok_json({"ok": True, "selected_verticals": selected})

        if action == "remove":
            connector = str(arguments.get("connector", "")).strip()
            if not connector:
                return self._tool_error("icc_hub remove: 'connector' is required")
            cfg = load_hub_config()
            selected = [v for v in cfg.get("selected_verticals", []) if v != connector]
            cfg["selected_verticals"] = selected
            save_hub_config(cfg)
            return self._tool_ok_json({"ok": True, "selected_verticals": selected})

        if action == "status":
            cfg = load_hub_config()
            pending = load_pending_conflicts()
            all_conflicts = pending.get("conflicts", [])
            unresolved = [c for c in all_conflicts if c.get("status") == "pending"]
            awaiting_apply = [c for c in all_conflicts if c.get("status") == "resolved"]
            queue_depth = 0
            qp = sync_queue_path()
            if qp.exists():
                queue_depth = sum(1 for line in qp.read_text(encoding="utf-8").splitlines() if line.strip())
            return self._tool_ok_json({
                "configured": is_configured(),
                "remote": cfg.get("remote", ""),
                "selected_verticals": cfg.get("selected_verticals", []),
                "pending_conflicts": len(unresolved),
                "conflicts": unresolved,
                "awaiting_application": len(awaiting_apply),
                "queue_depth": queue_depth,
            })

        if action == "sync":
            connector = str(arguments.get("connector", "")).strip() or None
            cfg = load_hub_config()
            verticals = [connector] if connector else cfg.get("selected_verticals", [])
            ts = int(_time.time() * 1000)
            for c in verticals:
                append_sync_event({"event": "stable", "connector": c, "pipeline": "__manual__", "ts_ms": ts})
                append_sync_event({"event": "pull_requested", "connector": c, "ts_ms": ts})
            return self._tool_ok_json({"ok": True, "triggered": verticals})

        if action == "configure":
            remote = str(arguments.get("remote", "")).strip()
            if not remote:
                return self._tool_error("icc_hub configure: 'remote' is required (e.g. user@host:repos/hub.git)")
            branch = str(arguments.get("branch", "emerge-hub")).strip() or "emerge-hub"
            author = str(arguments.get("author", "")).strip()
            if not author:
                return self._tool_error(
                    "icc_hub configure: 'author' is required (e.g. 'Alice <alice@team.com>')"
                )
            poll_interval = int(arguments.get("poll_interval_seconds", 300))
            new_verticals = arguments.get("selected_verticals")
            if isinstance(new_verticals, str):
                new_verticals = [v.strip() for v in new_verticals.split(",") if v.strip()]
            if not isinstance(new_verticals, list):
                new_verticals = []

            cfg = load_hub_config()
            cfg.update({
                "remote": remote,
                "branch": branch,
                "author": author,
                "poll_interval_seconds": poll_interval,
            })
            if new_verticals:
                cfg["selected_verticals"] = new_verticals
            elif "selected_verticals" not in cfg:
                cfg["selected_verticals"] = []
            save_hub_config(cfg)

            try:
                from scripts.emerge_sync import git_setup_worktree
                from scripts.hub_config import hub_worktree_path
                worktree = hub_worktree_path()
                result = git_setup_worktree(worktree, remote, branch, author)
                action_taken = result.get("action", "unknown")

                if action_taken == "cloned" and cfg.get("selected_verticals"):
                    import logging as _logging

                    from scripts.emerge_sync import import_vertical as _import_vertical

                    _log = _logging.getLogger(__name__)
                    for _connector in cfg["selected_verticals"]:
                        try:
                            _import_vertical(_connector, hub_worktree=worktree)
                        except Exception as _exc:
                            _log.warning(
                                "icc_hub configure: initial import failed for %s: %s",
                                _connector,
                                _exc,
                            )
            except Exception as exc:
                return self._tool_error(
                    f"icc_hub configure: git worktree init failed — {exc}. "
                    "Check that the remote URL is reachable and SSH keys are in place."
                )

            return self._tool_ok_json({
                "ok": True,
                "action": action_taken,  # "created" | "cloned" | "already_exists"
                "remote": remote,
                "branch": branch,
                "selected_verticals": cfg["selected_verticals"],
                "worktree": str(hub_worktree_path()),
                "next": (
                    "Hub configured. Start the sync agent in a terminal: "
                    "python scripts/emerge_sync.py run"
                ),
            })

        if action == "setup":
            return self._tool_ok_json({
                "ok": True,
                "message": (
                    "Use icc_hub(action='configure', remote='user@host:repos/hub.git', "
                    "author='Name <email>', selected_verticals=['connector1']) to configure "
                    "the hub directly from Claude Code. "
                    "Or run the interactive CLI wizard: python scripts/emerge_sync.py setup"
                ),
            })

        if action == "resolve":
            conflict_id = str(arguments.get("conflict_id", "")).strip()
            resolution = str(arguments.get("resolution", "")).strip()
            if not conflict_id:
                return self._tool_error("icc_hub resolve: 'conflict_id' is required")
            if resolution not in ("ours", "theirs", "skip"):
                elicit_resp = self._elicit(
                    f"请选择冲突 `{conflict_id}` 的解决策略：",
                    {
                        "type": "object",
                        "properties": {
                            "resolution": {
                                "type": "string",
                                "enum": ["ours", "theirs", "skip"],
                                "title": "解决策略",
                            }
                        },
                        "required": ["resolution"],
                    },
                )
                if elicit_resp is None:
                    return self._tool_error(
                        "icc_hub resolve: elicitation timed out — operation cancelled"
                    )
                resolution = str(elicit_resp.get("resolution", "")).strip()
                if resolution not in ("ours", "theirs", "skip"):
                    return self._tool_error(
                        f"icc_hub resolve: invalid resolution from elicitation: {resolution!r}"
                    )
            data = load_pending_conflicts()
            matched = False
            for conflict in data.get("conflicts", []):
                if conflict.get("conflict_id") == conflict_id:
                    conflict["resolution"] = resolution
                    conflict["status"] = "resolved"
                    matched = True
                    break
            if not matched:
                return self._tool_error(f"icc_hub resolve: conflict_id '{conflict_id}' not found")
            save_pending_conflicts(data)
            return self._tool_ok_json({"ok": True, "conflict_id": conflict_id, "resolution": resolution})

        return self._tool_error(
            f"icc_hub: unknown action '{action}'. Valid: configure|list|add|remove|sync|status|resolve|setup"
        )

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
                    "protocolVersion": "2025-03-26",
                    "capabilities": {
                        "tools": {},
                        "resources": {"subscribe": False},
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
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "tools": [
                        {
                            "name": "icc_span_open",
                            "description": (
                                "Open an intent span to track a multi-step MCP tool call sequence "
                                "in the flywheel. Use before any sequence of Lark/context7/skill tool calls "
                                "that represents a reusable intent. When the intent pipeline is stable, "
                                "returns the pipeline result directly (bridge) with zero LLM overhead. "
                                "Blocked if another span is already open."
                            ),
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "intent_signature": {
                                        "type": "string",
                                        "description": "<connector>.(read|write).<name> — e.g. 'lark.read.get-doc'",
                                    },
                                    "description": {"type": "string"},
                                    "args": {"type": "object", "description": "Input args for this span"},
                                    "source": {"type": "string", "enum": ["skill", "manual"], "default": "manual"},
                                    "skill_name": {"type": "string"},
                                },
                                "required": ["intent_signature"],
                            },
                        },
                        {
                            "name": "icc_span_close",
                            "description": (
                                "Close the current intent span and commit it to the flywheel WAL. "
                                "When the intent reaches stable, auto-generates a Python skeleton "
                                "in _pending/ for review. Call icc_span_approve after completing the skeleton."
                            ),
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "span_id": {"type": "string", "description": "span_id from icc_span_open"},
                                    "outcome": {"type": "string", "enum": ["success", "failure", "aborted"]},
                                    "result_summary": {"type": "object"},
                                    "intent_signature": {
                                        "type": "string",
                                        "description": "Required when span_id is unknown (daemon restart recovery)",
                                    },
                                },
                                "required": ["outcome"],
                            },
                        },
                        {
                            "name": "icc_span_approve",
                            "description": (
                                "Approve a completed pipeline skeleton and activate the span bridge. "
                                "Moves _pending/<name>.py to the real pipeline directory and generates "
                                "the required .yaml metadata. Only works when the intent is stable. "
                                "After approval, icc_span_open will bridge directly to this pipeline."
                            ),
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "intent_signature": {
                                        "type": "string",
                                        "description": "Stable span intent to approve",
                                    },
                                },
                                "required": ["intent_signature"],
                            },
                        },
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
                        # icc_read and icc_write are intentionally omitted from the schema.
                        # They remain callable internally but are deprecated for CC use.
                        # Use icc_span_open(intent_signature='<connector>.(read|write).<name>')
                        # instead — the span bridge executes the pipeline automatically when stable.
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
                        {
                            "name": "icc_hub",
                            "description": (
                                "Manage Memory Hub — bidirectional connector asset sync via a self-hosted git repo. "
                                "Actions: configure (first-time setup — saves config and initialises git worktree), "
                                "list (show config), add/remove (manage verticals), "
                                "sync (manual push+pull), status (show pending conflicts), "
                                "resolve (resolve a conflict with ours|theirs|skip)."
                            ),
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "action": {
                                        "type": "string",
                                        "enum": ["configure", "list", "add", "remove", "sync", "status", "resolve", "setup"],
                                        "description": "Hub action to perform",
                                    },
                                    "remote": {
                                        "type": "string",
                                        "description": "Git remote URL (required for configure, e.g. user@host:repos/hub.git)",
                                    },
                                    "branch": {
                                        "type": "string",
                                        "description": "Orphan branch name (configure only, default: emerge-hub)",
                                    },
                                    "author": {
                                        "type": "string",
                                        "description": "Git commit author (required for configure, e.g. 'Alice <alice@team.com>')",
                                    },
                                    "selected_verticals": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "Connector names to sync (configure only)",
                                    },
                                    "poll_interval_seconds": {
                                        "type": "integer",
                                        "description": "Background pull interval in seconds (configure only, default: 300)",
                                    },
                                    "connector": {
                                        "type": "string",
                                        "description": "Connector name (required for add/remove, optional for sync)",
                                    },
                                    "conflict_id": {
                                        "type": "string",
                                        "description": "Conflict ID from status output (required for resolve)",
                                    },
                                    "resolution": {
                                        "type": "string",
                                        "enum": ["ours", "theirs", "skip"],
                                        "description": "Resolution choice (required for resolve)",
                                    },
                                },
                                "required": ["action"],
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
        # connector://spans: per-connector span intent index
        span_candidates = self._span_tracker._load_candidates().get("spans", {})
        span_connectors: set[str] = set()
        for sig in span_candidates:
            if _PIPELINE_KEY_RE.match(sig):
                span_connectors.add(sig.split(".", 1)[0])
        for cname in sorted(span_connectors):
            spans_uri = f"connector://{cname}/spans"
            if spans_uri not in already_noted:
                static.append({
                    "uri": spans_uri,
                    "name": f"{cname} span intents",
                    "mimeType": "application/json",
                    "description": (
                        f"JSON index of all flywheel-tracked span intents for {cname}, "
                        "with policy status and skeleton generation state."
                    ),
                })
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
                if resource == "spans":
                    candidates = self._span_tracker._load_candidates().get("spans", {})
                    relevant = {
                        k: v for k, v in candidates.items()
                        if k.startswith(f"{connector}.")
                    }
                    return {"uri": uri, "mimeType": "application/json", "text": json.dumps(relevant, ensure_ascii=False)}
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
        try:
            with events_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=True) + "\n")
                f.flush()
                os.fsync(f.fileno())
            truncate_jsonl_if_needed(events_path, max_lines=10_000)
        except OSError:
            pass  # disk full or permissions — non-fatal; policy state written elsewhere

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
        with self._registry_lock:
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
            # human_fixes incremented via _increment_human_fix() on icc_reconcile(outcome=correct)
            self._update_candidate_entry(
                entry=entry,
                sampled_in_policy=sampled_in_policy,
                is_error=is_error,
                is_degraded=False,
                verify_passed=trusted_verify_passed,
                ts_ms=event["ts_ms"],
            )
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
        try:
            with events_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=True) + "\n")
                f.flush()
                os.fsync(f.fileno())
            truncate_jsonl_if_needed(events_path, max_lines=10_000)
        except OSError:
            pass  # disk full or permissions — non-fatal

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
        with self._registry_lock:
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
            # human_fixes incremented via _increment_human_fix() on icc_reconcile(outcome=correct)
            is_degraded = str(result.get("verification_state", "")).lower() == "degraded"
            self._update_candidate_entry(
                entry=entry,
                sampled_in_policy=sampled_in_policy,
                is_error=is_error,
                is_degraded=is_degraded,
                verify_passed=event["verify_passed"],
                ts_ms=event["ts_ms"],
            )
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

        if pipeline.get("frozen"):
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
            registry["pipelines"][candidate_key] = pipeline
            self._atomic_write_json(registry_path, registry)
            return

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
                        # Auto-crystallize: derive connector/mode/name from intent_signature
                        try:
                            _parts = intent_sig.split(".", 2)
                            if len(_parts) == 3:
                                _conn, _mode, _name = _parts
                                self._auto_crystallize(
                                    intent_signature=intent_sig,
                                    connector=_conn,
                                    pipeline_name=_name,
                                    mode=_mode,
                                    target_profile=entry.get("target_profile", "default"),
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
            if status == "stable":
                try:
                    from scripts.hub_config import append_sync_event, is_configured, load_hub_config
                    if is_configured():
                        parts = candidate_key.split(".", 2)
                        connector = parts[0] if parts else candidate_key
                        cfg = load_hub_config()
                        if connector in cfg.get("selected_verticals", []):
                            pipeline_name = parts[2] if len(parts) >= 3 else candidate_key
                            import time as _time
                            append_sync_event({
                                "event": "stable",
                                "connector": connector,
                                "pipeline": pipeline_name,
                                "ts_ms": int(_time.time() * 1000),
                            })
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
        with self._registry_lock:
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

    def _update_candidate_entry(
        self,
        *,
        entry: dict[str, Any],
        sampled_in_policy: bool,
        is_error: bool,
        is_degraded: bool,
        verify_passed: bool,
        ts_ms: int,
    ) -> None:
        """Apply standard attempt/success/verify/failure bookkeeping to a candidate entry.

        Mutates ``entry`` in-place. Must be called while ``_registry_lock`` is held.
        """
        entry["total_calls"] = int(entry.get("total_calls", 0)) + 1
        if is_error:
            sampled_in_policy = True  # errors always counted
        failed_attempt = (is_error or is_degraded) and sampled_in_policy
        if sampled_in_policy:
            entry["attempts"] += 1
            if not is_error:
                entry["successes"] += 1
            if verify_passed:
                entry["verify_passes"] += 1
            if is_degraded:
                entry["degraded_count"] = int(entry.get("degraded_count", 0)) + 1
            entry["consecutive_failures"] = (
                int(entry.get("consecutive_failures", 0)) + 1 if failed_attempt else 0
            )
            recent = list(entry.get("recent_outcomes", []))
            recent.append(0 if failed_attempt else 1)
            entry["recent_outcomes"] = recent[-WINDOW_SIZE:]
        entry["last_ts_ms"] = ts_ms

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

    def start_event_router(self) -> None:
        """Start EventRouter to watch pending-actions.json and local operator events."""
        import os as _os
        from scripts.event_router import EventRouter
        from pathlib import Path as _Path

        handlers: dict = {
            self._state_root / "pending-actions.json": lambda _: self._on_pending_actions(),
        }
        # Register local operator-events handler only when OperatorMonitor is active.
        # The handler delegates to OperatorMonitor.process_local_file() which owns the
        # PatternDetector + event buffer state.
        if _os.environ.get("EMERGE_OPERATOR_MONITOR") == "1":
            event_root = _Path.home() / ".emerge" / "operator-events"
            handlers[event_root] = lambda p: self._on_local_event_file(p)

        self._event_router = EventRouter(handlers)
        self._event_router.start()

    def stop_event_router(self) -> None:
        if self._event_router is not None:
            self._event_router.stop()

    def _on_pending_actions(self) -> None:
        """Called by EventRouter when pending-actions.json is created/modified."""
        pending_path = self._state_root / "pending-actions.json"
        if not pending_path.exists():
            return
        try:
            text = pending_path.read_text(encoding="utf-8")
        except OSError:
            return
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            try:
                pending_path.rename(self._state_root / "pending-actions.invalid.json")
            except OSError:
                pass
            return
        ts = int(data.get("submitted_at", 0))
        if ts <= self._last_seen_pending_ts:
            return
        actions = data.get("actions", [])
        try:
            self._write_mcp_push({
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
            return  # don't advance _last_seen_pending_ts — allow retry
        try:
            processed = self._state_root / "pending-actions.processed.json"
            pending_path.rename(processed)
            self._last_seen_pending_ts = ts
        except OSError:
            pass

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

    def _elicit(
        self,
        message: str,
        schema: dict,
        timeout: float = 60.0,
    ) -> dict | None:
        """Send elicitations/create to CC; block current thread until response.

        Must be called from a worker thread (not the main stdio loop).
        Returns the content dict from the response, or None on timeout.
        """
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
        elif t == "tool-call":
            call = a.get("call", {}) if isinstance(a.get("call"), dict) else {}
            tool = call.get("tool", "?")
            call_args = call.get("arguments", {})
            meta = a.get("meta", {}) if isinstance(a.get("meta"), dict) else {}
            scope = str(meta.get("scope", "")).strip()
            scope_suffix = f" scope={scope}" if scope else ""
            lines.append(f"{i}. 执行 tool-call {tool} args={call_args}{scope_suffix}")
        elif t == "crystallize-component":
            lines.append(f"{i}. 固化组件 {a.get('filename')} → {a.get('connector')}/cockpit/")
        else:
            lines.append(f"{i}. {t}: {a}")
    return "\n".join(lines)


def _write_response(payload: dict) -> None:
    """Write a JSON-RPC response to stdout, thread-safe."""
    with _stdout_lock:
        sys.stdout.write(json.dumps(payload) + "\n")
        sys.stdout.flush()


def run_stdio() -> None:
    import atexit
    from concurrent.futures import ThreadPoolExecutor

    daemon = EmergeDaemon()
    executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="emerge-worker")

    daemon.start_operator_monitor()
    daemon.start_event_router()
    atexit.register(daemon.stop_operator_monitor)
    atexit.register(daemon.stop_event_router)
    atexit.register(lambda: executor.shutdown(wait=False))

    for line in sys.stdin:
        try:
            text = line.strip()
            if not text:
                continue
            try:
                req = json.loads(text)
            except json.JSONDecodeError as exc:  # pragma: no cover
                _write_response({"jsonrpc": "2.0", "id": None,
                                 "error": {"code": -32700, "message": f"Parse error: {exc}"}})
                continue

            req_id = req.get("id")
            method = req.get("method", "")

            # Elicitation responses: wake waiting worker thread, never dispatch as a request
            if req_id and req_id in daemon._elicit_events:
                result_obj = req.get("result") or {}
                action = result_obj.get("action", "accept")
                # MCP 2025-03-26: response is {"action": "accept"|"decline"|"cancel", "content": {...}}
                # Store None for decline/cancel so _elicit() returns None → callers treat as cancelled
                if action != "accept":
                    daemon._elicit_results[req_id] = None
                else:
                    daemon._elicit_results[req_id] = result_obj.get("content") or {}
                ev = daemon._elicit_events.pop(req_id, None)
                if ev is not None:
                    ev.set()
                continue

            # Tool calls run in thread pool so _elicit() can block a worker
            # while the main loop continues routing
            if method == "tools/call":
                def _run(_req=req, _id=req_id):
                    try:
                        resp = daemon.handle_jsonrpc(_req)
                    except Exception as exc:  # pragma: no cover
                        resp = {"jsonrpc": "2.0", "id": _id,
                                "error": {"code": -32603, "message": str(exc)}}
                    if resp is not None:
                        _write_response(resp)
                executor.submit(_run)
                continue

            # All other methods (initialize, ping, tools/list, resources/*) are synchronous
            try:
                resp = daemon.handle_jsonrpc(req)
            except Exception as exc:  # pragma: no cover
                resp = {"jsonrpc": "2.0", "id": req_id,
                        "error": {"code": -32603, "message": str(exc)}}
            if resp is not None:
                _write_response(resp)
        except Exception:  # pragma: no cover
            pass  # never let a single bad message kill the main loop


if __name__ == "__main__":
    run_stdio()
