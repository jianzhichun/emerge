from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from scripts.crystallizer import _code_assigns_name
from scripts.distiller import Distiller
from scripts.node_role import is_runner_role
from scripts.policy_config import PIPELINE_KEY_RE, events_root, resolve_connector_root


class SynthesisProviderError(RuntimeError):
    pass


class SynthesisUnavailable(SynthesisProviderError):
    pass


@dataclass
class SynthesisJob:
    job_id: str
    normalized_intent: str
    connector: str
    runner_profile: str
    machine_ids: list[str]
    detector_signals: list[str]
    context_hint: dict[str, Any]
    events: list[dict[str, Any]]
    connector_notes: str = ""
    synthesis_hints: dict[str, Any] = field(default_factory=dict)
    event_fingerprint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SynthesisResult:
    connector: str
    mode: str
    pipeline_name: str
    code: str
    confidence: float = 0.0
    rationale: str = ""

    @property
    def intent_signature(self) -> str:
        return f"{self.connector}.{self.mode}.{self.pipeline_name}"


class SynthesisProvider(Protocol):
    def synthesize(self, job: SynthesisJob) -> SynthesisResult:
        ...


class NullSynthesisProvider:
    """Default provider: fail loudly into the event stream, never fake success."""

    def synthesize(self, job: SynthesisJob) -> SynthesisResult:
        raise SynthesisUnavailable(
            "reverse flywheel synthesis provider is not configured; set EMERGE_SYNTHESIS_COMMAND"
        )


class CommandSynthesisProvider:
    """JSON stdin/stdout provider for external LLM or local model adapters."""

    def __init__(self, command: str | list[str], *, timeout_s: float = 120.0) -> None:
        self._command = shlex.split(command) if isinstance(command, str) else list(command)
        if not self._command:
            raise ValueError("CommandSynthesisProvider requires a command")
        self._timeout_s = max(1.0, float(timeout_s))

    @classmethod
    def from_env(cls) -> "CommandSynthesisProvider | NullSynthesisProvider":
        raw = os.environ.get("EMERGE_SYNTHESIS_COMMAND", "").strip()
        if not raw:
            return NullSynthesisProvider()
        timeout_raw = os.environ.get("EMERGE_SYNTHESIS_TIMEOUT_S", "").strip()
        try:
            timeout_s = float(timeout_raw) if timeout_raw else 120.0
        except ValueError:
            timeout_s = 120.0
        return cls(raw, timeout_s=timeout_s)

    def synthesize(self, job: SynthesisJob) -> SynthesisResult:
        proc = subprocess.run(
            self._command,
            input=json.dumps(job.to_dict(), ensure_ascii=False),
            text=True,
            capture_output=True,
            timeout=self._timeout_s,
            check=False,
        )
        if proc.returncode != 0:
            raise SynthesisProviderError(
                f"synthesis command exited {proc.returncode}: {proc.stderr.strip()[:500]}"
            )
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise SynthesisProviderError(f"synthesis command returned invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise SynthesisProviderError("synthesis command must return a JSON object")
        return SynthesisResult(
            connector=str(payload.get("connector", "")).strip(),
            mode=str(payload.get("mode", "")).strip(),
            pipeline_name=str(payload.get("pipeline_name", "")).strip(),
            code=str(payload.get("code", "")).strip(),
            confidence=_as_float(payload.get("confidence", 0.0), 0.0),
            rationale=str(payload.get("rationale", "")).strip(),
        )


class SynthesisAgent:
    def __init__(
        self,
        *,
        state_root: Path,
        connector_root: Path | None = None,
        provider: SynthesisProvider | None = None,
        exec_tool,
        mode: str | None = None,
    ) -> None:
        self._state_root = state_root
        self._connector_root = connector_root or resolve_connector_root()
        env_mode = os.environ.get("EMERGE_SYNTHESIS_MODE", "").strip()
        self._mode = mode or env_mode or ("provider_exec" if provider is not None else "enqueue_only")
        if is_runner_role():
            self._mode = "enqueue_only"
        self._provider = provider or CommandSynthesisProvider.from_env()
        self._exec_tool = exec_tool
        self._seen: set[str] = set()
        self._lock = threading.Lock()

    def process_pattern(
        self,
        *,
        summary,
        runner_profile: str,
        events: list[dict[str, Any]],
        event_path: Path | None = None,
    ) -> dict[str, Any]:
        normalized = Distiller._normalise(str(summary.intent_signature))
        fingerprint = self._fingerprint(runner_profile, normalized, events)
        connector = self._infer_connector(normalized, summary.context_hint)
        job_id = "syn-" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:16]
        stream_path = event_path or events_root(self._state_root) / f"events-{runner_profile}.jsonl"
        job = SynthesisJob(
            job_id=job_id,
            normalized_intent=normalized,
            connector=connector,
            runner_profile=runner_profile,
            machine_ids=list(summary.machine_ids),
            detector_signals=list(summary.detector_signals),
            context_hint=dict(summary.context_hint),
            events=list(events),
            connector_notes=self._load_notes(connector),
            synthesis_hints=self._load_hints(connector),
            event_fingerprint=fingerprint,
        )

        with self._lock:
            if fingerprint in self._seen:
                return {"status": "duplicate", "job_id": job_id, "event_fingerprint": fingerprint}
            self._seen.add(fingerprint)

        self._append_event(
            stream_path,
            {
                "type": "pattern_pending_synthesis",
                "ts_ms": _now_ms(),
                "runner_profile": runner_profile,
                "job_id": job_id,
                "intent_signature": normalized,
                "event_fingerprint": fingerprint,
                "meta": {
                    "machine_ids": job.machine_ids,
                    "detector_signals": job.detector_signals,
                    "occurrences": int(summary.occurrences),
                },
            },
        )

        if self._mode != "provider_exec":
            self._append_event(
                stream_path,
                {
                    "type": "synthesis_job_ready",
                    "ts_ms": _now_ms(),
                    "runner_profile": runner_profile,
                    "job_id": job_id,
                    "intent_signature": normalized,
                    "event_fingerprint": fingerprint,
                    "job": job.to_dict(),
                },
            )
            return {"status": "enqueued", "job_id": job_id, "event_fingerprint": fingerprint}

        try:
            result = self._provider.synthesize(job)
            self._validate_result(result)
            exec_args = self._exec_arguments(result, runner_profile, job)
            exec_result = self._exec_tool(exec_args)
        except Exception as exc:
            event_type = "synthesis_unconfigured" if isinstance(exc, SynthesisUnavailable) else "synthesis_failed"
            self._append_event(
                stream_path,
                {
                    "type": event_type,
                    "ts_ms": _now_ms(),
                    "runner_profile": runner_profile,
                    "job_id": job_id,
                    "intent_signature": normalized,
                    "error_class": exc.__class__.__name__,
                    "error": str(exc)[:1000],
                    "event_fingerprint": fingerprint,
                },
            )
            return {"status": "failed", "job_id": job_id, "error": str(exc)}

        is_error = bool(exec_result.get("isError")) if isinstance(exec_result, dict) else True
        self._append_event(
            stream_path,
            {
                "type": "synthesis_exec_failed" if is_error else "synthesis_exec_succeeded",
                "ts_ms": _now_ms(),
                "runner_profile": runner_profile,
                "job_id": job_id,
                "intent_signature": result.intent_signature,
                "source_intent_signature": normalized,
                "confidence": result.confidence,
                "event_fingerprint": fingerprint,
            },
        )
        return {
            "status": "exec_failed" if is_error else "executed",
            "job_id": job_id,
            "intent_signature": result.intent_signature,
            "event_fingerprint": fingerprint,
        }

    @staticmethod
    def _fingerprint(runner_profile: str, normalized: str, events: list[dict[str, Any]]) -> str:
        reduced = [
            {
                "ts_ms": e.get("ts_ms"),
                "machine_id": e.get("machine_id"),
                "app": e.get("app"),
                "event_type": e.get("event_type"),
                "payload": e.get("payload", {}),
            }
            for e in events
        ]
        payload = json.dumps(
            {"runner_profile": runner_profile, "intent": normalized, "events": reduced},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _infer_connector(normalized: str, context_hint: dict[str, Any]) -> str:
        first = normalized.split(".", 1)[0].strip()
        return first or str(context_hint.get("app", "unknown")).strip() or "unknown"

    def _load_notes(self, connector: str) -> str:
        path = self._connector_root / connector / "NOTES.md"
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return ""
        return text[:4000]

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

    def _validate_result(self, result: SynthesisResult) -> None:
        if result.mode not in {"read", "write"}:
            raise SynthesisProviderError(f"synthesis result mode must be read/write, got {result.mode!r}")
        if not PIPELINE_KEY_RE.fullmatch(result.intent_signature):
            raise SynthesisProviderError(f"invalid synthesized intent_signature: {result.intent_signature!r}")
        required = "__result" if result.mode == "read" else "__action"
        if not _code_assigns_name(result.code, required):
            raise SynthesisProviderError(f"synthesized code must assign {required}")

    @staticmethod
    def _exec_arguments(result: SynthesisResult, runner_profile: str, job: SynthesisJob) -> dict[str, Any]:
        return {
            "intent_signature": result.intent_signature,
            "code": result.code,
            "result_var": "__result" if result.mode == "read" else "__action",
            "target_profile": runner_profile,
            "no_replay": False,
            "source": "reverse_flywheel_synthesis",
            "synthesis_job_id": job.job_id,
            "source_intent_signature": job.normalized_intent,
        }

    @staticmethod
    def _append_event(path: Path, event: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default
