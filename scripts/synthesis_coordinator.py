from __future__ import annotations

import hashlib
import json
import os
import time
import textwrap
from pathlib import Path
from typing import Any

from scripts.crystallizer import IndentedSafeDumper, PipelineCrystallizer, _code_assigns_name
from scripts.policy_config import PIPELINE_KEY_RE, derive_profile_token, events_root, sessions_root


class SynthesisCoordinator:
    """Thin coordination layer for Claude Code lead-agent synthesis jobs.

    The coordinator does not call an LLM. It gathers deterministic evidence,
    emits jobs for the lead agent + skills workflow, validates submitted output,
    runs smoke checks, and writes reviewed artifacts.
    """

    def __init__(
        self,
        *,
        state_root: Path,
        connector_root: Path,
        exec_tool,
        auto_approve: bool | None = None,
        mark_blocked=None,
    ) -> None:
        self._state_root = state_root
        self._connector_root = connector_root
        self._exec_tool = exec_tool
        self._mark_blocked = mark_blocked or (lambda _intent, _reason: None)
        if auto_approve is None:
            auto_approve = os.environ.get("EMERGE_FORWARD_SYNTHESIS_AUTO_APPROVE", "").lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
        self._auto_approve = bool(auto_approve)

    def enqueue_forward_synthesis(
        self,
        *,
        intent_signature: str,
        connector: str,
        pipeline_name: str,
        mode: str,
        target_profile: str = "default",
        event_path: Path | None = None,
    ) -> dict[str, Any]:
        samples = self.collect_success_samples(intent_signature, target_profile=target_profile)
        fingerprint = self._fingerprint(intent_signature, target_profile, samples)
        job_id = "fwd-" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:16]
        job = {
            "job_id": job_id,
            "normalized_intent": intent_signature,
            "connector": connector,
            "mode": mode,
            "pipeline_name": pipeline_name,
            "runner_profile": target_profile,
            "source": "forward",
            "skill_name": "emerge-forward-synthesis",
            "event_fingerprint": fingerprint,
            "samples": samples,
            "connector_notes": self._load_notes(connector),
            "synthesis_hints": self._load_hints(connector),
        }
        stream_path = event_path or events_root(self._state_root) / "events.jsonl"
        self._append_event(
            stream_path,
            {
                "type": "forward_synthesis_pending",
                "ts_ms": _now_ms(),
                "job_id": job_id,
                "intent_signature": intent_signature,
                "event_fingerprint": fingerprint,
                "skill_name": "emerge-forward-synthesis",
                "job": job,
            },
        )
        return {
            "status": "enqueued",
            "job_id": job_id,
            "event_fingerprint": fingerprint,
            "samples": len(samples),
        }

    def collect_success_samples(self, intent_signature: str, *, target_profile: str = "default") -> list[dict[str, Any]]:
        normalized = (target_profile or "default").strip() or "default"
        profile_suffix = "" if normalized == "default" else f"__{derive_profile_token(normalized)}"
        samples: list[dict[str, Any]] = []
        root = sessions_root(self._state_root)
        if not root.exists():
            return samples
        for session_dir in sorted(root.iterdir()):
            if not session_dir.is_dir():
                continue
            if profile_suffix:
                if not session_dir.name.endswith(profile_suffix):
                    continue
            elif "__" in session_dir.name:
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
                    meta = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
                    if (
                        entry.get("status") == "success"
                        and not entry.get("no_replay", False)
                        and meta.get("intent_signature") == intent_signature
                    ):
                        samples.append(
                            {
                                "session_id": session_dir.name,
                                "finished_at_ms": int(entry.get("finished_at_ms", 0) or 0),
                                "code": str(entry.get("code", "")),
                                "args": meta.get("script_args") if isinstance(meta.get("script_args"), dict) else {},
                                "result": meta.get("result_var_value"),
                            }
                        )
        samples.sort(key=lambda sample: (int(sample.get("finished_at_ms", 0)), str(sample.get("session_id", ""))))
        return samples

    def submit_synthesis_result(
        self,
        *,
        job: dict[str, Any],
        result: dict[str, Any],
        event_path: Path | None = None,
    ) -> dict[str, Any]:
        stream_path = event_path or events_root(self._state_root) / "events.jsonl"
        try:
            normalized = str(job.get("normalized_intent") or result.get("intent_signature") or "").strip()
            connector = str(result.get("connector", "")).strip()
            mode = str(result.get("mode", "")).strip()
            pipeline_name = str(result.get("pipeline_name", "")).strip()
            intent_signature = f"{connector}.{mode}.{pipeline_name}"
            if str(job.get("source", "forward")) == "forward" and normalized and intent_signature != normalized:
                raise ValueError(f"synthesis result {intent_signature!r} does not match job {normalized!r}")
            if not PIPELINE_KEY_RE.fullmatch(intent_signature):
                raise ValueError(f"invalid synthesized intent_signature: {intent_signature!r}")
            code = str(result.get("code", "")).strip()
            required = "__result" if mode == "read" else "__action"
            if not _code_assigns_name(code, required):
                raise ValueError(f"synthesized code must assign {required}")

            sample = self._latest_sample(job)
            exec_result = self._exec_tool(
                {
                    "intent_signature": intent_signature,
                    "code": code,
                    "result_var": required,
                    "script_args": sample.get("args", {}),
                    "target_profile": str(job.get("runner_profile", "default") or "default"),
                    "no_replay": True,
                    "source": "forward_flywheel_synthesis",
                    "synthesis_job_id": str(job.get("job_id", "")),
                    "source_intent_signature": normalized,
                }
            )
            if not isinstance(exec_result, dict) or exec_result.get("isError"):
                self._record_smoke_failure(stream_path, job, intent_signature, exec_result)
                return {"status": "smoke_failed", "job_id": job.get("job_id")}
            expected = sample.get("result")
            if expected is not None and exec_result.get("result_var_value") != expected:
                self._record_smoke_failure(stream_path, job, intent_signature, exec_result)
                return {"status": "smoke_failed", "job_id": job.get("job_id")}

            py_path, yaml_path = self._write_pipeline(
                connector=connector,
                mode=mode,
                pipeline_name=pipeline_name,
                intent_signature=intent_signature,
                code=code,
                verify_strategy=result.get("verify_strategy") if isinstance(result.get("verify_strategy"), dict) else {},
            )
            self._append_event(
                stream_path,
                {
                    "type": "forward_synthesis_completed",
                    "ts_ms": _now_ms(),
                    "job_id": job.get("job_id"),
                    "intent_signature": intent_signature,
                    "py_path": str(py_path),
                    "yaml_path": str(yaml_path),
                    "confidence": result.get("confidence", 0.0),
                },
            )
            return {"status": "completed", "py_path": str(py_path), "yaml_path": str(yaml_path)}
        except Exception as exc:
            self._append_event(
                stream_path,
                {
                    "type": "forward_synthesis_failed",
                    "ts_ms": _now_ms(),
                    "job_id": job.get("job_id"),
                    "intent_signature": job.get("normalized_intent"),
                    "error_class": exc.__class__.__name__,
                    "error": str(exc)[:1000],
                },
            )
            return {"status": "failed", "job_id": job.get("job_id"), "error": str(exc)}

    def _write_pipeline(
        self,
        *,
        connector: str,
        mode: str,
        pipeline_name: str,
        intent_signature: str,
        code: str,
        verify_strategy: dict[str, Any],
    ) -> tuple[Path, Path]:
        output_dir = self._connector_root / connector / "pipelines" / mode
        if not self._auto_approve:
            output_dir = output_dir / "_pending"
        PipelineCrystallizer._check_path_in_root(output_dir, self._connector_root, label="synthesis output directory")
        output_dir.mkdir(parents=True, exist_ok=True)
        py_path = output_dir / f"{pipeline_name}.py"
        yaml_path = output_dir / f"{pipeline_name}.yaml"
        PipelineCrystallizer._check_path_in_root(py_path, self._connector_root, label="synthesis python path")
        PipelineCrystallizer._check_path_in_root(yaml_path, self._connector_root, label="synthesis yaml path")
        py_src = self._pipeline_source(intent_signature, mode, code, verify_strategy)
        yaml_data = {
            "intent_signature": intent_signature,
            "rollback_or_stop_policy": "stop",
            "synthesized": True,
            "synthesis_source": "claude_code_lead_agent",
            "pending_review": not self._auto_approve,
        }
        if mode == "read":
            yaml_data["read_steps"] = ["run_read"]
            yaml_data["verify_steps"] = ["verify_read"]
        else:
            yaml_data["write_steps"] = ["run_write"]
            yaml_data["verify_steps"] = ["verify_write"]
        PipelineCrystallizer._atomic_write_text(py_path, py_src, prefix=".synthesis-")
        PipelineCrystallizer._atomic_write_text(yaml_path, IndentedSafeDumper.dump_yaml(yaml_data), prefix=".synthesis-")
        return py_path, yaml_path

    @staticmethod
    def _pipeline_source(intent_signature: str, mode: str, code: str, verify_strategy: dict[str, Any]) -> str:
        indented = textwrap.indent(code, "    ")
        required_fields = [str(v) for v in verify_strategy.get("required_fields", []) if str(v)]
        fields_literal = repr(required_fields)
        if mode == "read":
            return (
                f"# auto-generated by Claude Code lead agent\n"
                f"# intent_signature: {intent_signature}\n\n"
                "def run_read(metadata, args):\n"
                "    __args = args\n"
                f"{indented}\n"
                "    return __result\n\n\n"
                "def verify_read(metadata, args, rows):\n"
                "    if not isinstance(rows, list):\n"
                "        return {\"ok\": False, \"reason\": \"rows_not_list\"}\n"
                f"    required_fields = {fields_literal}\n"
                "    for row in rows:\n"
                "        if not isinstance(row, dict):\n"
                "            return {\"ok\": False, \"reason\": \"row_not_dict\"}\n"
                "        missing = [field for field in required_fields if field not in row]\n"
                "        if missing:\n"
                "            return {\"ok\": False, \"reason\": \"missing_fields\", \"fields\": missing}\n"
                "    return {\"ok\": True}\n"
            )
        return (
            f"# auto-generated by Claude Code lead agent\n"
            f"# intent_signature: {intent_signature}\n\n"
            "def run_write(metadata, args):\n"
            "    __args = args\n"
            f"{indented}\n"
            "    return __action\n\n\n"
            "def verify_write(metadata, args, action_result):\n"
            "    return {\"ok\": isinstance(action_result, dict) and bool(action_result.get(\"ok\"))}\n"
        )

    @staticmethod
    def _latest_sample(job: dict[str, Any]) -> dict[str, Any]:
        samples = job.get("samples") if isinstance(job.get("samples"), list) else []
        if not samples:
            return {"args": {}, "result": None}
        return dict(samples[-1]) if isinstance(samples[-1], dict) else {"args": {}, "result": None}

    @staticmethod
    def _fingerprint(intent_signature: str, target_profile: str, samples: list[dict[str, Any]]) -> str:
        payload = json.dumps(
            {"intent_signature": intent_signature, "target_profile": target_profile, "samples": samples},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _load_notes(self, connector: str) -> str:
        path = self._connector_root / connector / "NOTES.md"
        try:
            return path.read_text(encoding="utf-8")[:4000]
        except OSError:
            return ""

    def _load_hints(self, connector: str) -> dict[str, Any]:
        path = self._connector_root / connector / "synthesis_hints.yaml"
        if not path.exists():
            return {}
        try:
            import yaml  # type: ignore

            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _failure_event(event_type: str, job: dict[str, Any], intent_signature: str, exec_result: Any) -> dict[str, Any]:
        return {
            "type": event_type,
            "ts_ms": _now_ms(),
            "job_id": job.get("job_id"),
            "intent_signature": intent_signature,
            "result": exec_result,
        }

    def _record_smoke_failure(self, path: Path, job: dict[str, Any], intent_signature: str, exec_result: Any) -> None:
        self._append_event(path, self._failure_event("forward_synthesis_smoke_failed", job, intent_signature, exec_result))
        count = self._increment_failure_count(intent_signature)
        if count >= 3:
            try:
                self._mark_blocked(intent_signature, "smoke_failed")
            except Exception:
                pass
            self._append_event(
                path,
                {
                    "type": "forward_synthesis_blocked",
                    "ts_ms": _now_ms(),
                    "job_id": job.get("job_id"),
                    "intent_signature": intent_signature,
                    "reason": "smoke_failed",
                    "failure_count": count,
                },
            )

    def _increment_failure_count(self, intent_signature: str) -> int:
        path = events_root(self._state_root) / "forward-synthesis-failures.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
        count = int(data.get(intent_signature, 0) or 0) + 1
        data[intent_signature] = count
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        return count

    @staticmethod
    def _append_event(path: Path, event: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")


def _now_ms() -> int:
    return int(time.time() * 1000)
