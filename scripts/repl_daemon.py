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

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "icc_exec":
            try:
                mode = str(arguments.get("mode", "inline_code"))
                target_profile = str(arguments.get("target_profile", "default"))
                candidate_key = self._resolve_exec_candidate_key(
                    arguments=arguments,
                    target_profile=target_profile,
                )
                sampled_in_policy = self._should_sample(candidate_key)
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
                return result
            except Exception as exc:
                return {
                    "isError": True,
                    "content": [{"type": "text", "text": f"icc_exec failed: {exc}"}],
                }
        if name == "icc_read":
            try:
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
        return {"isError": True, "content": [{"type": "text", "text": f"Unknown tool: {name}"}]}

    def handle_jsonrpc(self, request: dict[str, Any]) -> dict[str, Any]:
        req_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params") or {}
        if not isinstance(params, dict):
            params = {}

        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "tools": [
                        {"name": "icc_exec", "description": "Persistent Python exec"},
                        {"name": "icc_read", "description": "Run read pipeline"},
                        {"name": "icc_write", "description": "Run write pipeline"},
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

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }

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
        rollout_pct = int(pipeline.get("rollout_pct", 0))
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
        data = json.loads(path.read_text(encoding="utf-8"))
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
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    run_stdio()
