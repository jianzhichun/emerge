"""Flywheel recording and policy lifecycle for EmergeDaemon.

FlywheelRecorder owns all candidate tracking and pipeline registry transitions:
  - record_exec_event / record_pipeline_event  — append to session WAL files
  - update_pipeline_registry                   — explore→canary→stable transitions
  - increment_human_fix                        — driven by icc_reconcile(outcome=correct)
  - should_sample / has_synthesizable_wal_entry — sampling and WAL scan helpers
  - resolve_exec_candidate_key / resolve_pipeline_candidate_key — key derivation

All mutable state lives in files managed by the daemon (candidates.json,
pipelines-registry.json, WAL). The lock is shared with the daemon so
concurrent tool calls are serialized correctly.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from scripts.policy_config import (
    PIPELINE_KEY_RE as _PIPELINE_KEY_RE,
    PROMOTE_MAX_HUMAN_FIX_RATE,
    PROMOTE_MIN_ATTEMPTS,
    PROMOTE_MIN_SUCCESS_RATE,
    PROMOTE_MIN_VERIFY_RATE,
    ROLLBACK_CONSECUTIVE_FAILURES,
    STABLE_MIN_ATTEMPTS,
    STABLE_MIN_SUCCESS_RATE,
    STABLE_MIN_VERIFY_RATE,
    WINDOW_SIZE,
    atomic_write_json,
    derive_profile_token,
    load_json_object,
    truncate_jsonl_if_needed,
)
from scripts.pipeline_engine import PipelineEngine
_log = logging.getLogger(__name__)


class FlywheelRecorder:
    """Records flywheel observations and manages pipeline policy lifecycle."""

    def __init__(
        self,
        *,
        state_root: Callable[[], Path],
        session_id: Callable[[], str],
        registry_lock: threading.Lock,
        sink: Callable[[], Any],                  # returns metrics sink with .emit(event, payload)
        pipeline: Callable[[], PipelineEngine], # for connector_roots
        write_mcp_push: Callable[[dict], None], # pushes notifications to CC stdout
        auto_crystallize: Callable[..., None],  # triggers skeleton generation
    ) -> None:
        self._get_state_root = state_root
        self._get_session_id = session_id
        self._lock = registry_lock
        self._get_sink = sink
        self._get_pipeline = pipeline
        self._write_mcp_push = write_mcp_push
        self._auto_crystallize = auto_crystallize

    # ------------------------------------------------------------------
    # Key derivation (static — no daemon state needed)
    # ------------------------------------------------------------------

    @staticmethod
    def resolve_exec_candidate_key(*, arguments: dict[str, Any], target_profile: str) -> str:
        """Key is intent_signature — runner/script are execution metadata, not identity.

        Returns the key if it matches the canonical format, otherwise returns an
        empty string so that update_pipeline_registry rejects it at write time.
        """
        key = str(arguments.get("intent_signature", "")).strip()
        if key and not _PIPELINE_KEY_RE.match(key):
            _log.warning(
                "icc_exec: intent_signature %r does not match <connector>.(read|write).<name> — "
                "execution will proceed but telemetry will NOT be registered",
                key,
            )
        return key

    @staticmethod
    def resolve_pipeline_candidate_key(*, arguments: dict[str, Any], pipeline_id: str) -> str:
        """Key is pipeline_id (= intent by convention: <connector>.<mode>.<op>)."""
        return pipeline_id

    # ------------------------------------------------------------------
    # Sampling
    # ------------------------------------------------------------------

    def should_sample(self, candidate_key: str) -> bool:
        if not candidate_key:
            return True
        state_root = self._get_state_root()
        session_id = self._get_session_id()
        path = state_root / "pipelines-registry.json"
        if not path.exists():
            return True
        data = load_json_object(path, root_key="pipelines")
        pipeline = data.get("pipelines", {}).get(candidate_key)
        if not isinstance(pipeline, dict):
            return True
        status = str(pipeline.get("status", "explore"))
        if status != "canary":
            return True
        rollout_pct = int(pipeline.get("rollout_pct", 0) or 0)
        rollout_pct = max(0, min(100, rollout_pct))
        if rollout_pct <= 0:
            return False

        candidates_path = (state_root / session_id) / "candidates.json"
        total_calls = 0
        if candidates_path.exists():
            cand = load_json_object(candidates_path, root_key="candidates")
            entry = cand.get("candidates", {}).get(candidate_key, {})
            if isinstance(entry, dict):
                total_calls = int(entry.get("total_calls", 0))
        next_call = total_calls + 1
        return ((next_call - 1) % 100) < rollout_pct

    def has_synthesizable_wal_entry(self, intent_signature: str, target_profile: str = "default") -> bool:
        """Return True if any session WAL has a success entry for the intent."""
        if not intent_signature:
            return False
        normalized = (target_profile or "default").strip() or "default"
        profile_suffix = "" if normalized == "default" else f"__{derive_profile_token(normalized)}"
        state_root = self._get_state_root()
        if not state_root.exists():
            return False
        try:
            session_dirs = list(state_root.iterdir())
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

    # ------------------------------------------------------------------
    # Candidate entry bookkeeping (mutates entry in-place; lock must be held)
    # ------------------------------------------------------------------

    def update_candidate_entry(
        self,
        *,
        entry: dict[str, Any],
        sampled_in_policy: bool,
        is_error: bool,
        is_degraded: bool,
        verify_passed: bool,
        ts_ms: int,
    ) -> None:
        """Apply standard attempt/success/verify/failure bookkeeping to entry."""
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

    # ------------------------------------------------------------------
    # Human-fix increment (driven by icc_reconcile outcome=correct)
    # ------------------------------------------------------------------

    def increment_human_fix(self, intent_signature: str) -> None:
        """Increment human_fixes for the candidate keyed by intent_signature."""
        state_root = self._get_state_root()
        session_id = self._get_session_id()
        session_dir = state_root / session_id
        candidates_path = session_dir / "candidates.json"
        if not candidates_path.exists():
            return
        with self._lock:
            registry = load_json_object(candidates_path, root_key="candidates")
            entry = registry["candidates"].get(intent_signature)
            if not isinstance(entry, dict):
                return
            entry["human_fixes"] = int(entry.get("human_fixes", 0)) + 1
            registry["candidates"][intent_signature] = entry
            atomic_write_json(candidates_path, registry)
            try:
                self.update_pipeline_registry(candidate_key=intent_signature, entry=entry)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Event recording
    # ------------------------------------------------------------------

    def record_exec_event(
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
        state_root = self._get_state_root()
        session_id = self._get_session_id()
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
            "human_fix": False,
            "is_error": is_error,
            "sampled_in_policy": sampled_in_policy,
        }
        session_dir = state_root / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        events_path = session_dir / "exec-events.jsonl"
        try:
            with events_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=True) + "\n")
                f.flush()
                os.fsync(f.fileno())
            truncate_jsonl_if_needed(events_path, max_lines=10_000)
        except OSError:
            pass

        try:
            self._get_sink().emit(
                "exec.call",
                {
                    "intent_signature": arguments.get("intent_signature", ""),
                    "target_profile": arguments.get("target_profile", "default"),
                    "is_error": is_error,
                    "session_id": session_id,
                },
            )
        except Exception:
            pass

        if not intent_signature:
            return
        key = candidate_key
        registry_path = session_dir / "candidates.json"
        with self._lock:
            registry = load_json_object(registry_path, root_key="candidates")
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
            if description:
                entry["description"] = description
            entry["last_execution_path"] = execution_path
            self.update_candidate_entry(
                entry=entry,
                sampled_in_policy=sampled_in_policy,
                is_error=is_error,
                is_degraded=False,
                verify_passed=trusted_verify_passed,
                ts_ms=event["ts_ms"],
            )
            registry["candidates"][key] = entry
            atomic_write_json(registry_path, registry)
            self.update_pipeline_registry(candidate_key=key, entry=entry)

    def record_pipeline_event(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
        is_error: bool,
        error_text: str = "",
        execution_path: str = "local",
        mode: str = "",
    ) -> None:
        state_root = self._get_state_root()
        session_id = self._get_session_id()
        session_dir = state_root / session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        connector = str(arguments.get("connector", ""))
        pipeline = str(arguments.get("pipeline", ""))
        _pid = str(arguments.get("pipeline_id") or result.get("pipeline_id", ""))
        _pid_parts = _pid.split(".")
        if len(_pid_parts) >= 3:
            mode = _pid_parts[1]
        elif not mode:
            mode = "read" if tool_name.endswith("_read") else "write"
        pipeline_id = str(result.get("pipeline_id", f"{connector}.{mode}.{pipeline}"))
        intent_signature = str(result.get("intent_signature", ""))
        target_profile = str(arguments.get("target_profile", "default"))

        # Read description from pipeline YAML if available
        pipeline_description = ""
        for _cr in self._get_pipeline()._connector_roots:
            _meta = _cr / connector / "pipelines" / mode / f"{pipeline}.yaml"
            if _meta.exists():
                try:
                    _data = PipelineEngine._load_metadata(_meta)
                    pipeline_description = str(_data.get("description", "")).strip()
                except Exception:
                    pass
                break

        verify_passed = str(result.get("verification_state", "")).lower() == "verified"
        key = self.resolve_pipeline_candidate_key(arguments=arguments, pipeline_id=pipeline_id)
        sampled_in_policy = self.should_sample(key)
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
            "human_fix": False,
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
            pass

        try:
            self._get_sink().emit(
                f"pipeline.{mode}",
                {
                    "pipeline_id": pipeline_id,
                    "is_error": is_error,
                    "session_id": session_id,
                },
            )
        except Exception:
            pass

        registry_path = session_dir / "candidates.json"
        with self._lock:
            registry = load_json_object(registry_path, root_key="candidates")
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
            entry["source"] = "pipeline"
            entry["last_execution_path"] = execution_path
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
            is_degraded = str(result.get("verification_state", "")).lower() == "degraded"
            self.update_candidate_entry(
                entry=entry,
                sampled_in_policy=sampled_in_policy,
                is_error=is_error,
                is_degraded=is_degraded,
                verify_passed=event["verify_passed"],
                ts_ms=event["ts_ms"],
            )
            registry["candidates"][key] = entry
            atomic_write_json(registry_path, registry)
            self.update_pipeline_registry(candidate_key=key, entry=entry)

    # ------------------------------------------------------------------
    # Policy lifecycle transitions
    # ------------------------------------------------------------------

    def update_pipeline_registry(
        self,
        *,
        candidate_key: str,
        entry: dict[str, Any],
    ) -> None:
        if not _PIPELINE_KEY_RE.match(candidate_key):
            _log.warning(
                "Refusing to register malformed pipeline key %r — must match <connector>.(read|write).<name>",
                candidate_key,
            )
            return

        state_root = self._get_state_root()
        session_id = self._get_session_id()
        registry_path = state_root / "pipelines-registry.json"
        registry = load_json_object(registry_path, root_key="pipelines")
        pipeline = registry["pipelines"].get(
            candidate_key,
            {
                "status": "explore",
                "rollout_pct": 0,
                "last_transition_reason": "init",
                "attempts_at_transition": 0,
            },
        )
        if entry.get("description") and not pipeline.get("description"):
            pipeline["description"] = entry["description"]
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
            atomic_write_json(registry_path, registry)
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
                intent_sig = entry.get("intent_signature", "")
                if intent_sig and entry.get("source") == "exec":
                    if self.has_synthesizable_wal_entry(intent_sig, entry.get("target_profile", "default")):
                        pipeline["synthesis_ready"] = True
                        try:
                            self._get_sink().emit(
                                "policy.synthesis_ready",
                                {
                                    "candidate_key": candidate_key,
                                    "intent_signature": intent_sig,
                                    "session_id": session_id,
                                },
                            )
                        except Exception:
                            pass
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
                self._get_sink().emit(
                    "policy.transition",
                    {"candidate_key": candidate_key, "new_status": status, "session_id": session_id},
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
                            append_sync_event({
                                "event": "stable",
                                "connector": connector,
                                "pipeline": pipeline_name,
                                "ts_ms": int(time.time() * 1000),
                            })
                except Exception:
                    pass

        registry["pipelines"][candidate_key] = pipeline
        atomic_write_json(registry_path, registry)
        try:
            self._write_mcp_push({
                "jsonrpc": "2.0",
                "method": "notifications/resources/list_changed",
                "params": {},
            })
        except Exception:
            pass
