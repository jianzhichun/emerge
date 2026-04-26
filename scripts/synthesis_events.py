"""Fact-only synthesis event helpers.

This module does not call providers, smoke-test generated code, or write
pipelines. It packages deterministic evidence so Claude Code skills can perform
the intelligent distillation step outside Python.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from scripts.policy_config import derive_profile_token, events_root, sessions_root


def normalize_intent_signature(value: str) -> str:
    """Normalize free-form detector names into `connector.mode.name`-like text."""
    normalized = re.sub(r"[^a-zA-Z0-9_.]+", "_", value.strip().lower())
    normalized = re.sub(r"_+", "_", normalized).strip("._")
    return normalized[:200] or "unknown.pattern"


def enqueue_reverse_synthesis(
    *,
    state_root: Path,
    connector_root: Path,
    summary: Any,
    runner_profile: str,
    events: list[dict[str, Any]],
    event_path: Path | None = None,
) -> dict[str, Any]:
    """Emit reverse synthesis fact events for Claude Code skills."""
    normalized = normalize_intent_signature(str(summary.intent_signature))
    fingerprint = _fingerprint(runner_profile, normalized, events)
    job_id = "syn-" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:16]
    connector = _infer_connector(normalized, getattr(summary, "context_hint", {}))
    job = {
        "job_id": job_id,
        "normalized_intent": normalized,
        "connector": connector,
        "runner_profile": runner_profile,
        "source": "reverse",
        "skill_name": "distill-from-pattern",
        "machine_ids": list(getattr(summary, "machine_ids", [])),
        "detector_signals": list(getattr(summary, "detector_signals", [])),
        "context_hint": dict(getattr(summary, "context_hint", {}) or {}),
        "events": list(events),
        "connector_notes": _load_notes(connector_root, connector),
        "synthesis_hints": _load_hints(connector_root, connector),
        "event_fingerprint": fingerprint,
    }
    stream_path = event_path or events_root(state_root) / f"events-{runner_profile}.jsonl"
    _append_event(
        stream_path,
        {
            "type": "pattern_pending_synthesis",
            "ts_ms": _now_ms(),
            "runner_profile": runner_profile,
            "job_id": job_id,
            "intent_signature": normalized,
            "event_fingerprint": fingerprint,
            "skill_name": "distill-from-pattern",
            "meta": {
                "machine_ids": job["machine_ids"],
                "detector_signals": job["detector_signals"],
                "occurrences": len(events),
            },
        },
    )
    _append_event(
        stream_path,
        {
            "type": "synthesis_job_ready",
            "ts_ms": _now_ms(),
            "runner_profile": runner_profile,
            "job_id": job_id,
            "intent_signature": normalized,
            "event_fingerprint": fingerprint,
            "skill_name": "distill-from-pattern",
            "job": job,
        },
    )
    return {"status": "enqueued", "job_id": job_id, "event_fingerprint": fingerprint}


def enqueue_forward_synthesis(
    *,
    state_root: Path,
    connector_root: Path,
    intent_signature: str,
    connector: str,
    pipeline_name: str,
    mode: str,
    target_profile: str = "default",
    event_path: Path | None = None,
) -> dict[str, Any]:
    """Emit forward crystallization facts for Claude Code skills."""
    samples = collect_success_samples(
        state_root,
        intent_signature,
        target_profile=target_profile,
    )
    fingerprint = _fingerprint(target_profile, intent_signature, samples)
    job_id = "fwd-" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:16]
    job = {
        "job_id": job_id,
        "normalized_intent": intent_signature,
        "connector": connector,
        "mode": mode,
        "pipeline_name": pipeline_name,
        "runner_profile": target_profile,
        "source": "forward",
        "skill_name": "crystallize-from-wal",
        "event_fingerprint": fingerprint,
        "samples": samples,
        "connector_notes": _load_notes(connector_root, connector),
        "synthesis_hints": _load_hints(connector_root, connector),
    }
    stream_path = event_path or events_root(state_root) / "events.jsonl"
    _append_event(
        stream_path,
        {
            "type": "forward_synthesis_pending",
            "ts_ms": _now_ms(),
            "job_id": job_id,
            "intent_signature": intent_signature,
            "event_fingerprint": fingerprint,
            "skill_name": "crystallize-from-wal",
            "job": job,
        },
    )
    return {
        "status": "enqueued",
        "job_id": job_id,
        "event_fingerprint": fingerprint,
        "samples": len(samples),
    }


def collect_success_samples(
    state_root: Path,
    intent_signature: str,
    *,
    target_profile: str = "default",
) -> list[dict[str, Any]]:
    """Collect successful replayable WAL samples for an intent."""
    normalized = (target_profile or "default").strip() or "default"
    profile_suffix = "" if normalized == "default" else f"__{derive_profile_token(normalized)}"
    samples: list[dict[str, Any]] = []
    root = sessions_root(state_root)
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
        for line in wal_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
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


def _append_event(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def _fingerprint(scope: str, normalized_intent: str, events: list[dict[str, Any]]) -> str:
    payload = {
        "scope": scope,
        "intent": normalized_intent,
        "events": events,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _infer_connector(normalized_intent: str, context_hint: dict[str, Any]) -> str:
    explicit = str(context_hint.get("connector") or context_hint.get("app") or "").strip()
    if explicit:
        return normalize_intent_signature(explicit).split(".")[0]
    return normalized_intent.split(".", 1)[0] if "." in normalized_intent else "unknown"


def _load_notes(connector_root: Path, connector: str) -> str:
    path = connector_root / connector / "NOTES.md"
    try:
        return path.read_text(encoding="utf-8")[:20_000]
    except OSError:
        return ""


def _load_hints(connector_root: Path, connector: str) -> dict[str, Any]:
    path = connector_root / connector / "synthesis_hints.yaml"
    if not path.exists():
        return {}
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _now_ms() -> int:
    return int(time.time() * 1000)
