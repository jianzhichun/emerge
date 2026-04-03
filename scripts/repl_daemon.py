from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from hashlib import sha1
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
    default_repl_root,
)
from scripts.repl_state import ReplState  # noqa: E402


class ReplDaemon:
    def __init__(self, root: Path | None = None) -> None:
        resolved_root = root or ROOT
        state_root = Path(os.environ.get("REPL_STATE_ROOT", str(default_repl_root())))
        self._base_session_id = self._resolve_session_id(resolved_root)
        self._state_root = state_root
        self._repl_by_profile: dict[str, ReplState] = {}
        self.pipeline = PipelineEngine(root=resolved_root)
        self._root = resolved_root
        self._script_roots = self._resolve_script_roots()

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "icc_exec":
            mode = str(arguments.get("mode", "inline_code"))
            target_profile = str(arguments.get("target_profile", "default"))
            candidate_key = self._candidate_key(
                target_profile=target_profile,
                intent_signature=str(arguments.get("intent_signature", "")),
                script_ref=str(arguments.get("script_ref", "")),
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
            self._record_exec_event(
                arguments=arguments,
                result=result,
                target_profile=target_profile,
                mode=mode,
                sampled_in_policy=sampled_in_policy,
                candidate_key=candidate_key,
            )
            return result
        if name == "icc_read":
            result = self.pipeline.run_read(arguments)
            return {"content": [{"type": "text", "text": json.dumps(result)}]}
        if name == "icc_write":
            result = self.pipeline.run_write(arguments)
            return {"content": [{"type": "text", "text": json.dumps(result)}]}
        return {"isError": True, "content": [{"type": "text", "text": f"Unknown tool: {name}"}]}

    def handle_jsonrpc(self, request: dict[str, Any]) -> dict[str, Any]:
        req_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params", {})

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
            result = self.call_tool(name, arguments)
            return {"jsonrpc": "2.0", "id": req_id, "result": result}

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }

    def _get_repl(self, target_profile: str) -> ReplState:
        key = target_profile or "default"
        if key not in self._repl_by_profile:
            safe_profile = self._sanitize_profile(key)
            if safe_profile == "default":
                session_id = self._base_session_id
            else:
                session_id = f"{self._base_session_id}__{safe_profile}"
            self._repl_by_profile[key] = ReplState(state_root=self._state_root, session_id=session_id)
        return self._repl_by_profile[key]

    @staticmethod
    def _sanitize_profile(profile: str) -> str:
        allowed = []
        for ch in profile:
            if ch.isalnum() or ch in {".", "-", "_"}:
                allowed.append(ch)
            else:
                allowed.append("_")
        return "".join(allowed) or "default"

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
        event = {
            "ts_ms": int(time.time() * 1000),
            "mode": mode,
            "target_profile": target_profile,
            "intent_signature": intent_signature,
            "script_ref": script_ref,
            "verify_passed": bool(arguments.get("verify_passed", False)),
            "human_fix": bool(arguments.get("human_fix", False)),
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
        if registry_path.exists():
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
        else:
            registry = {"candidates": {}}
        entry = registry["candidates"].get(
            key,
            {
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
                "last_ts_ms": 0,
            },
        )
        if is_error:
            sampled_in_policy = True
        if sampled_in_policy:
            entry["attempts"] += 1
            if not is_error:
                entry["successes"] += 1
            if event["verify_passed"]:
                entry["verify_passes"] += 1
            if event["human_fix"]:
                entry["human_fixes"] += 1
        is_degraded = str(arguments.get("verification_state", "")).lower() == "degraded"
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
        registry = (
            json.loads(registry_path.read_text(encoding="utf-8"))
            if registry_path.exists()
            else {"pipelines": {}}
        )
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
            if consecutive_failures >= 2:
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
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def _resolve_session_id(self, resolved_root: Path) -> str:
        explicit = os.environ.get("REPL_SESSION_ID")
        if explicit:
            return explicit
        project_hash = sha1(str(resolved_root).encode("utf-8")).hexdigest()[:10]
        project_name = resolved_root.name or "project"
        return f"{project_name}-{project_hash}"

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

    def _should_sample(self, candidate_key: str) -> bool:
        if "::" not in candidate_key:
            return True
        session_dir = self._state_root / self._base_session_id
        path = session_dir / "pipelines-registry.json"
        if not path.exists():
            return True
        data = json.loads(path.read_text(encoding="utf-8"))
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
        attempts = 0
        if candidates_path.exists():
            cand = json.loads(candidates_path.read_text(encoding="utf-8"))
            entry = cand.get("candidates", {}).get(candidate_key, {})
            if isinstance(entry, dict):
                attempts = int(entry.get("attempts", 0))
        next_attempt = attempts + 1
        return (next_attempt % 100) < rollout_pct


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
