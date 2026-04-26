"""Control-plane read/write API functions.

All cmd_control_plane_* functions live here.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import time
from pathlib import Path

import sys

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.admin.shared import _resolve_state_root  # noqa: E402
from scripts.policy_config import (  # noqa: E402
    default_state_root,
    default_hook_state_root,
    derive_profile_token,
    derive_session_id,
    sessions_root,
    events_root,
)
from scripts.state_tracker import StateTracker, load_tracker, with_locked_tracker  # noqa: E402
from scripts.intent_registry import IntentRegistry  # noqa: E402
from scripts.policy_engine import derive_stage  # noqa: E402


# ---------------------------------------------------------------------------
# Session resolvers
# ---------------------------------------------------------------------------

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _normalize_session_id(raw: str) -> str:
    sid = (raw or "").strip()
    if not sid:
        raise ValueError("session_id is required")
    if ".." in sid or "/" in sid or "\\" in sid:
        raise ValueError("invalid session_id path")
    if not _SESSION_ID_RE.fullmatch(sid):
        raise ValueError("invalid session_id format")
    return sid


def _resolve_session_id(session_id: str | None = None) -> str:
    if session_id:
        return _normalize_session_id(session_id)
    return derive_session_id(os.environ.get("EMERGE_SESSION_ID"), Path.cwd())


def _session_paths(session_id: str | None = None) -> tuple[Path, Path, Path]:
    state_root = _resolve_state_root()
    sid = _resolve_session_id(session_id=session_id)
    if not session_id:
        target_profile = str(os.environ.get("EMERGE_TARGET_PROFILE", "default")).strip() or "default"
        if target_profile != "default":
            profile_key = derive_profile_token(target_profile)
            sid = f"{sid}__{profile_key}"
    session_dir = sessions_root(state_root) / sid
    return session_dir, session_dir / "wal.jsonl", session_dir / "checkpoint.json"


def cmd_control_plane_sessions(
    limit: int = 200,
    state_root: Path | None = None,
    current_session_id: str | None = None,
) -> dict:
    """List known session directories under state root for cockpit selector."""
    root = Path(state_root) if state_root else _resolve_state_root()
    sessions_dir = sessions_root(root)
    current = _resolve_session_id(session_id=current_session_id) if current_session_id else _resolve_session_id()
    sessions: list[dict] = []
    if sessions_dir.exists():
        for entry in sessions_dir.iterdir():
            if not entry.is_dir():
                continue
            sid = entry.name
            if not _SESSION_ID_RE.fullmatch(sid):
                continue
            marker_paths = (
                entry / "checkpoint.json",
                entry / "wal.jsonl",
                entry / "exec-events.jsonl",
                entry / "pipeline-events.jsonl",
                entry / "tool-events.jsonl",
            )
            existing = [p for p in marker_paths if p.exists()]
            if not existing:
                continue
            last_ts_ms = int(max(p.stat().st_mtime for p in existing) * 1000)
            sessions.append(
                {
                    "session_id": sid,
                    "last_ts_ms": last_ts_ms,
                    "has_checkpoint": (entry / "checkpoint.json").exists(),
                    "has_wal": (entry / "wal.jsonl").exists(),
                }
            )
    sessions.sort(key=lambda x: int(x.get("last_ts_ms", 0)), reverse=True)
    return {
        "ok": True,
        "current_session_id": current,
        "sessions": sessions[: max(1, int(limit or 200))],
    }


# ---------------------------------------------------------------------------
# Control-plane read API
# ---------------------------------------------------------------------------

def cmd_control_plane_state() -> dict:
    """Full StateTracker snapshot for cockpit."""
    state_path = Path(default_hook_state_root()) / "state.json"
    tracker = load_tracker(state_path)
    d = tracker.to_dict()
    return {
        "ok": True,
        "deltas": d.get("deltas", []),
        "risks": d.get("open_risks", []),
        "verification_state": d.get("verification_state", "verified"),
        "consistency_window_ms": d.get("consistency_window_ms", 0),
        "active_span_id": d.get("active_span_id"),
        "active_span_intent": d.get("active_span_intent"),
    }


def cmd_control_plane_intents() -> dict:
    """Global intent list from state/registry/intents.json (single source of truth).

    Clean-break contract: each row surfaces the canonical ``stage`` field
    written by :class:`scripts.policy_engine.PolicyEngine`. Legacy
    ``policy_status`` naming has been removed.
    """
    state_root = _resolve_state_root()
    data = IntentRegistry.load(state_root)
    intents = []
    for key, entry in data.get("intents", {}).items():
        if not isinstance(entry, dict):
            continue
        stage = entry.get("stage")
        if not stage:
            # Lazy fallback for rows written before PolicyEngine: re-derive
            # read-only from counters. PolicyEngine remains the only writer.
            stage = derive_stage(entry)
        intents.append(
            {
                "intent_signature": key,
                "stage": stage,
                "success_rate": entry.get("success_rate"),
                "verify_rate": entry.get("verify_rate"),
                "human_fix_rate": entry.get("human_fix_rate"),
                "consecutive_failures": entry.get("consecutive_failures", 0),
                "frozen": entry.get("frozen", False),
                "updated_at_ms": entry.get("updated_at_ms", 0),
                "persistent": entry.get("persistent", False),
                "description": entry.get("description", ""),
                "last_transition_reason": entry.get("last_transition_reason"),
                "last_transition_ts_ms": entry.get("last_transition_ts_ms", 0),
                "last_demotion": entry.get("last_demotion"),
            }
        )
    return {"ok": True, "intents": intents}


def cmd_control_plane_intent_history(
    intent_signature: str,
    *,
    limit: int | None = None,
) -> dict:
    """Per-intent lifecycle audit trail.

    Returns the bounded ``transition_history`` stored by
    :class:`scripts.policy_engine.PolicyEngine`, plus the ``last_demotion``
    snapshot for quick rollback attribution.
    """
    key = (intent_signature or "").strip()
    if not key:
        return {"ok": False, "error": "intent_signature required"}
    state_root = _resolve_state_root()
    data = IntentRegistry.load(state_root)
    entry = data.get("intents", {}).get(key)
    if not isinstance(entry, dict):
        return {"ok": False, "error": "unknown_intent", "intent_signature": key}
    history = list(entry.get("transition_history") or [])
    if limit is not None and limit > 0:
        history = history[-int(limit):]
    return {
        "ok": True,
        "intent_signature": key,
        "stage": entry.get("stage"),
        "last_transition_reason": entry.get("last_transition_reason"),
        "last_transition_ts_ms": entry.get("last_transition_ts_ms", 0),
        "last_demotion": entry.get("last_demotion"),
        "transition_history": history,
    }


def cmd_control_plane_session(session_id: str | None = None) -> dict:
    """Session health: checkpoint + recovery + WAL stats."""
    session_dir, wal_path, checkpoint_path = _session_paths(session_id=session_id)
    wal_entries = 0
    if wal_path.exists():
        with wal_path.open("r", encoding="utf-8") as f:
            wal_entries = sum(1 for line in f if line.strip())
    checkpoint = None
    if checkpoint_path.exists():
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    recovery = None
    recovery_path = session_dir / "recovery.json"
    if recovery_path.exists():
        try:
            recovery = json.loads(recovery_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "ok": True,
        "session_id": _resolve_session_id(session_id=session_id),
        "session_dir": str(session_dir),
        "wal_entries": wal_entries,
        "checkpoint": checkpoint,
        "recovery": recovery,
    }


def _load_jsonl_filtered(
    path: Path,
    *,
    limit: int,
    since_ms: int = 0,
    intent: str = "",
    intent_prefix: str = "",
    intent_field: str = "intent_signature",
    ts_field: str = "ts_ms",
) -> list[dict]:
    rows: list[dict] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if since_ms and int(row.get("ts_ms", 0)) < since_ms:
                continue
            sig = str(row.get(intent_field, "") or "")
            if intent and sig != intent:
                continue
            if intent_prefix and not sig.startswith(intent_prefix):
                continue
            rows.append(row)
    rows.sort(key=lambda item: int(item.get(ts_field, 0) or 0), reverse=True)
    return rows[:limit]


def cmd_control_plane_hook_state() -> dict:
    """Hook state: fields tracked by hooks in state.json + context injection preview."""
    hook_state_root = Path(default_hook_state_root())
    state_path = hook_state_root / "state.json"
    from scripts.span_tracker import SpanTracker
    tracker = load_tracker(state_path)

    state = tracker.state
    hook_fields = {
        "turn_count": int(state.get("turn_count", 0) or 0),
        "active_span_id": state.get("active_span_id") or None,
        "active_span_intent": state.get("active_span_intent") or None,
        "span_nudge_sent": (hook_state_root / "span-nudge-sent").exists(),
    }

    try:
        exec_root = Path(os.environ.get("EMERGE_STATE_ROOT", str(default_state_root())))
        reflection = SpanTracker(
            state_root=exec_root,
            hook_state_root=hook_state_root,
        ).format_reflection_with_cache(cache_ttl_ms=15 * 60 * 1000)
        context_preview = tracker.format_additional_context()
        if reflection:
            context_preview = reflection + "\n\n" + context_preview
        active_span = hook_fields["active_span_id"]
        if not active_span and hook_fields["turn_count"] > 1 and hook_fields["turn_count"] % 5 == 0:
            context_preview = (
                "[Span] No active span. "
                "If this turn involves tool use, open one first: "
                'icc_span_open(intent_signature="connector.mode.name").'
                "\n\n" + context_preview
            )
    except Exception as e:
        context_preview = f"(preview unavailable: {e})"

    hook_list: list[dict] = []
    try:
        _hooks_json = Path.home() / ".claude" / "hooks.json"
        if _hooks_json.exists():
            _hdata = json.loads(_hooks_json.read_text(encoding="utf-8"))
            for event, entries in (_hdata if isinstance(_hdata, dict) else {}).items():
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    for h in (entry.get("hooks") or []):
                        cmd = str(h.get("command", ""))
                        if "emerge" in cmd.lower():
                            hook_list.append({"event": event, "command": cmd})
    except Exception:
        pass

    return {
        "ok": True,
        "hook_fields": hook_fields,
        "context_preview": context_preview,
        "registered_hooks": hook_list,
    }


def cmd_control_plane_exec_events(
    limit: int = 100,
    since_ms: int = 0,
    intent: str = "",
    intent_prefix: str = "",
    session_id: str | None = None,
) -> dict:
    """Paginated exec events from session."""
    session_dir, _, _ = _session_paths(session_id=session_id)
    return {"ok": True, "events": _load_jsonl_filtered(
        session_dir / "exec-events.jsonl",
        limit=limit,
        since_ms=since_ms,
        intent=intent,
        intent_prefix=intent_prefix,
    )}


def cmd_control_plane_tool_events(
    limit: int = 200,
    since_ms: int = 0,
    session_id: str | None = None,
) -> dict:
    """Paginated general CC tool-call events from session (Bash, Read, Grep, etc.)."""
    session_dir, _, _ = _session_paths(session_id=session_id)
    return {"ok": True, "events": _load_jsonl_filtered(
        session_dir / "tool-events.jsonl",
        limit=limit,
        since_ms=since_ms,
    )}


def cmd_control_plane_pipeline_events(
    limit: int = 100,
    since_ms: int = 0,
    intent: str = "",
    intent_prefix: str = "",
    session_id: str | None = None,
) -> dict:
    """Paginated pipeline events from session."""
    session_dir, _, _ = _session_paths(session_id=session_id)
    return {"ok": True, "events": _load_jsonl_filtered(
        session_dir / "pipeline-events.jsonl",
        limit=limit,
        since_ms=since_ms,
        intent=intent,
        intent_prefix=intent_prefix,
    )}


def cmd_control_plane_spans(limit: int = 50, intent: str = "", intent_prefix: str = "") -> dict:
    """Recent span WAL entries."""
    state_root = _resolve_state_root()
    return {"ok": True, "spans": _load_jsonl_filtered(
        state_root / "span-wal" / "spans.jsonl",
        limit=limit,
        intent=intent,
        intent_prefix=intent_prefix,
        ts_field="closed_at_ms",
    )}


def cmd_control_plane_span_candidates() -> dict:
    """All tracked intent entries.

    The endpoint name is kept for URL stability, but the payload uses the
    canonical ``intents`` key. The legacy ``candidates`` alias has been
    removed as part of the clean-break refactor.
    """
    state_root = _resolve_state_root()
    data = IntentRegistry.load(state_root)
    return {"ok": True, "intents": data.get("intents", {})}


def cmd_control_plane_reflection_cache(ttl_ms: int = 15 * 60 * 1000) -> dict:
    """Reflection cache status for cockpit observability."""
    ttl_ms = max(0, int(ttl_ms or 0))
    state_root = _resolve_state_root()
    cache_path = state_root / "reflection-cache" / "global.json"
    now_ms = int(time.time() * 1000)
    if not cache_path.exists():
        return {
            "ok": True,
            "exists": False,
            "source": "lightweight",
            "is_fresh": False,
            "generated_at_ms": None,
            "age_ms": None,
            "ttl_ms": ttl_ms,
            "summary_preview": "",
            "meta": {},
            "cache_path": str(cache_path),
        }
    try:
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return {
            "ok": True,
            "exists": True,
            "source": "lightweight",
            "is_fresh": False,
            "generated_at_ms": None,
            "age_ms": None,
            "ttl_ms": ttl_ms,
            "summary_preview": "",
            "meta": {},
            "cache_path": str(cache_path),
            "error": "invalid_cache_json",
        }

    generated_at_ms = int(raw.get("generated_at_ms", 0) or 0)
    summary_text = str(raw.get("summary_text", "") or "")
    age_ms = max(0, now_ms - generated_at_ms) if generated_at_ms > 0 else None
    is_fresh = bool(
        generated_at_ms > 0
        and summary_text.strip()
        and (ttl_ms <= 0 or now_ms - generated_at_ms <= ttl_ms)
    )
    return {
        "ok": True,
        "exists": True,
        "source": "deep_cache" if is_fresh else "lightweight",
        "is_fresh": is_fresh,
        "generated_at_ms": generated_at_ms if generated_at_ms > 0 else None,
        "age_ms": age_ms,
        "ttl_ms": ttl_ms,
        "summary_preview": summary_text[:200],
        "meta": raw.get("meta", {}),
        "cache_path": str(cache_path),
    }


def cmd_control_plane_monitors(state_root=None) -> dict:
    """Return connected runner monitor state from runner-monitor-state.json."""
    root = Path(state_root) if state_root else _resolve_state_root()
    state_path = events_root(root) / "runner-monitor-state.json"
    if not state_path.exists():
        return {"runners": [], "team_active": False}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        return {
            "runners": data.get("runners", []),
            "team_active": bool(data.get("team_active", False)),
        }
    except (OSError, json.JSONDecodeError):
        return {"runners": [], "team_active": False}


def cmd_control_plane_watchers(state_root=None) -> dict:
    """Return the aggregated watcher heartbeat / SLO summary.

    Each ``watch_emerge.py`` process writes a heartbeat file under
    ``state/events/watchers/``. This command summarises them so cockpit
    clients can render a "watchers healthy / stale" badge without scanning
    the directory themselves.
    """
    from scripts.watchers import watcher_health_summary

    root = Path(state_root) if state_root else _resolve_state_root()
    return watcher_health_summary(root)


_PROFILE_RE = re.compile(r"^[a-zA-Z0-9_.\-]{1,64}$")


def cmd_control_plane_runner_events(profile: str, limit: int = 20) -> dict:
    """Return per-runner events, activity buckets, and today's stats."""
    if not profile or not _PROFILE_RE.match(profile):
        return {"ok": False, "error": "invalid profile"}
    limit = min(int(limit), 100)
    state_root = _resolve_state_root()
    events_path = events_root(state_root) / f"events-{profile}.jsonl"
    _empty: dict = {"ok": True, "events": [], "activity": [0] * 10, "today_events": 0, "today_alerts": 0}
    if not events_path.exists():
        return _empty
    try:
        raw = events_path.read_text(encoding="utf-8")
    except OSError:
        return _empty

    lines = raw.splitlines()
    # Parse all lines (needed for today_events/today_alerts and activity)
    all_parsed = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            all_parsed.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    now_ms = int(time.time() * 1000)
    # Activity: divide last 3600s into 10 buckets of 360s each
    bucket_ms = 360_000
    window_start_ms = now_ms - 3_600_000
    activity = [0] * 10

    today_start_ms = int(
        datetime.datetime.combine(
            datetime.datetime.now(datetime.timezone.utc).date(),
            datetime.time.min,
            tzinfo=datetime.timezone.utc,
        ).timestamp() * 1000
    )
    today_events = 0
    today_alerts = 0
    for ev in all_parsed:
        ts = ev.get("ts_ms", 0)
        if ts >= today_start_ms:
            today_events += 1
            if ev.get("type") == "pattern_alert":
                today_alerts += 1
        # Activity buckets
        if ts >= window_start_ms:
            idx = min(int((ts - window_start_ms) // bucket_ms), 9)
            activity[idx] += 1

    # Return last `limit` events newest-first
    events_sorted = sorted(all_parsed, key=lambda e: e.get("ts_ms", 0), reverse=True)
    return {
        "ok": True,
        "events": events_sorted[:limit],
        "activity": activity,
        "today_events": today_events,
        "today_alerts": today_alerts,
    }


# ---------------------------------------------------------------------------
# Control-plane write API
# ---------------------------------------------------------------------------

def cmd_control_plane_delta_reconcile(delta_id: str, outcome: str, intent_signature: str = "") -> dict:
    state_path = Path(default_hook_state_root()) / "state.json"
    with_locked_tracker(state_path, lambda tracker: tracker.reconcile_delta(delta_id, outcome))
    return {"ok": True, "delta_id": delta_id, "outcome": outcome}


def cmd_control_plane_risk_update(
    risk_id: str, action: str, reason: str = "", snooze_duration_ms: int = 3600000,
) -> dict:
    state_path = Path(default_hook_state_root()) / "state.json"

    def _mutate(tracker):
        tracker.update_risk(risk_id, action=action, reason=reason or None, snooze_duration_ms=snooze_duration_ms)

    with_locked_tracker(state_path, _mutate)
    return {"ok": True, "risk_id": risk_id, "action": action}


def cmd_control_plane_risk_add(text: str, intent_signature: str = "") -> dict:
    state_path = Path(default_hook_state_root()) / "state.json"
    with_locked_tracker(
        state_path,
        lambda tracker: tracker.add_risk(text, intent_signature=intent_signature or None),
    )
    return {"ok": True, "text": text}


def cmd_control_plane_policy_freeze(key: str) -> dict:
    state_root = _resolve_state_root()
    data = IntentRegistry.load(state_root)
    if key not in data.get("intents", {}):
        return {"ok": False, "error": f"intent {key!r} not found"}
    data["intents"][key]["frozen"] = True
    IntentRegistry.save(state_root, data)
    return {"ok": True, "key": key, "frozen": True}


def cmd_control_plane_policy_unfreeze(key: str) -> dict:
    state_root = _resolve_state_root()
    data = IntentRegistry.load(state_root)
    if key not in data.get("intents", {}):
        return {"ok": False, "error": f"intent {key!r} not found"}
    data["intents"][key]["frozen"] = False
    IntentRegistry.save(state_root, data)
    return {"ok": True, "key": key, "frozen": False}


def cmd_control_plane_session_export(session_id: str | None = None) -> dict:
    state_path = Path(default_hook_state_root()) / "state.json"
    tracker = load_tracker(state_path)
    session_dir, wal_path, checkpoint_path = _session_paths(session_id=session_id)
    snapshot = {
        "state_tracker": tracker.to_dict(),
        "session_id": _resolve_session_id(session_id=session_id),
    }
    if checkpoint_path.exists():
        try:
            snapshot["checkpoint"] = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"ok": True, "snapshot": snapshot}


def cmd_control_plane_session_reset(confirm: str, full: bool = False, session_id: str | None = None) -> dict:
    if confirm != "RESET":
        return {"ok": False, "error": "must pass confirm='RESET'"}
    state_path = Path(default_hook_state_root()) / "state.json"
    export = cmd_control_plane_session_export(session_id=session_id)

    def _reset(tracker):
        if tracker.state.get("active_span_id"):
            return {
                "ok": False,
                "error": "active_span_open",
                "message": (
                    f"Cannot reset while span is active "
                    f"(intent={tracker.state.get('active_span_intent', '?')}). "
                    "Close or abort the span first via icc_span_close(outcome='aborted')."
                ),
            }
        fresh = StateTracker()
        tracker.state.clear()
        tracker.state.update(fresh.state)
        return {"ok": True}

    reset_result = with_locked_tracker(state_path, _reset)
    if not reset_result.get("ok"):
        return reset_result
    removed: list[str] = []
    if full:
        session_dir, wal_path, checkpoint_path = _session_paths(session_id=session_id)
        recovery_path = session_dir / "recovery.json"
        exec_events_path = session_dir / "exec-events.jsonl"
        pipeline_events_path = session_dir / "pipeline-events.jsonl"
        for p in (wal_path, checkpoint_path, recovery_path, exec_events_path, pipeline_events_path):
            try:
                if p.exists():
                    p.unlink()
                    removed.append(str(p))
            except OSError:
                pass
    return {
        "ok": True,
        "reset": True,
        "full": bool(full),
        "removed_paths": removed,
        "pre_reset_snapshot": export.get("snapshot"),
    }
