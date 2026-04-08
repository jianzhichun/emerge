from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from scripts.policy_config import default_hook_state_root

GOAL_CONTROL_SCHEMA_VERSION = "goal.control.v1"
GOAL_EVENT_SCHEMA_VERSION = "goal.event.v1"

EVENT_HUMAN_EDIT = "human_edit"
EVENT_HOOK_PAYLOAD = "hook_payload"
EVENT_SYSTEM_GENERATE = "system_generate"
EVENT_SYSTEM_REFINE = "system_refine"
EVENT_ROLLBACK_REQUEST = "rollback_request"

_VALID_EVENT_TYPES = {
    EVENT_HUMAN_EDIT,
    EVENT_HOOK_PAYLOAD,
    EVENT_SYSTEM_GENERATE,
    EVENT_SYSTEM_REFINE,
    EVENT_ROLLBACK_REQUEST,
}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _goal_control_root() -> Path:
    return Path(os.environ.get("CLAUDE_PLUGIN_DATA", str(default_hook_state_root())))


def _goal_snapshot_path(root: Path) -> Path:
    return root / "goal-snapshot.json"


def _goal_ledger_path(root: Path) -> Path:
    return root / "goal-ledger.jsonl"


def _goal_lock_path(root: Path) -> Path:
    return root / ".goal-control.lock"


@contextlib.contextmanager
def _file_lock(lock_path: Path, timeout_ms: int = 3000):
    """Cross-platform advisory file lock.

    Uses ``fcntl.flock`` on POSIX (macOS, Linux). On Windows (or any platform
    where ``fcntl`` is unavailable), falls back to a sentinel-file mutex.
    The fallback is sufficient for single-machine single-daemon scenarios.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    start_ms = _now_ms()

    try:
        import fcntl as _fcntl
        _has_fcntl = True
    except ImportError:
        _has_fcntl = False

    if _has_fcntl:
        with lock_path.open("a+", encoding="utf-8") as handle:
            while True:
                try:
                    _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                    try:
                        yield
                        return
                    finally:
                        _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
                except BlockingIOError:
                    if _now_ms() - start_ms >= timeout_ms:
                        raise TimeoutError(f"goal control lock timeout: {lock_path}")
                    time.sleep(0.02)
    else:
        # Windows fallback: use a separate sentinel file as a best-effort mutex.
        # Not atomic, but sufficient for single-daemon single-machine use.
        sentinel = lock_path.with_suffix(".wlock")
        while sentinel.exists():
            if _now_ms() - start_ms >= timeout_ms:
                raise TimeoutError(
                    f"goal control lock timeout (Windows fallback): {lock_path}"
                )
            time.sleep(0.02)
        sentinel.touch()
        try:
            yield
        finally:
            try:
                sentinel.unlink()
            except FileNotFoundError:
                pass


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=True) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _normalize_text(text: Any, *, max_chars: int = 120) -> str:
    normalized = str(text or "").strip()
    if len(normalized) > max_chars:
        normalized = normalized[:max_chars]
    return normalized


def _default_snapshot() -> dict[str, Any]:
    return {
        "goal_schema_version": GOAL_CONTROL_SCHEMA_VERSION,
        "version": 0,
        "text": "",
        "source": "unset",
        "decided_by": "bootstrap",
        "rationale": "uninitialized",
        "updated_at_ms": 0,
        "ttl_ms": 0,
        "expires_at_ms": 0,
        "locked_until_ms": 0,
        "last_event_id": "",
    }


def _coerce_snapshot(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return _default_snapshot()
    snap = _default_snapshot()
    snap["goal_schema_version"] = str(raw.get("goal_schema_version", GOAL_CONTROL_SCHEMA_VERSION))
    try:
        snap["version"] = max(0, int(raw.get("version", 0)))
    except Exception:
        snap["version"] = 0
    snap["text"] = _normalize_text(raw.get("text", ""))
    snap["source"] = str(raw.get("source", "unset") or "unset")
    snap["decided_by"] = str(raw.get("decided_by", "bootstrap") or "bootstrap")
    snap["rationale"] = str(raw.get("rationale", "") or "")
    for key in ("updated_at_ms", "ttl_ms", "expires_at_ms", "locked_until_ms"):
        try:
            snap[key] = max(0, int(raw.get(key, 0)))
        except Exception:
            snap[key] = 0
    snap["last_event_id"] = str(raw.get("last_event_id", "") or "")
    return snap


def _score_event(event_type: str, source: str, confidence: float) -> float:
    base_source = {
        "cockpit": 0.95,
        "human": 0.95,
        "daemon": 0.65,
        "hook_payload": 0.5,
        "system": 0.8,
    }.get(source, 0.4)
    event_boost = {
        EVENT_HUMAN_EDIT: 0.3,
        EVENT_SYSTEM_GENERATE: 0.2,
        EVENT_SYSTEM_REFINE: 0.1,
        EVENT_HOOK_PAYLOAD: 0.0,
        EVENT_ROLLBACK_REQUEST: 0.4,
    }.get(event_type, 0.0)
    bounded_conf = min(1.0, max(0.0, float(confidence)))
    return base_source + event_boost + bounded_conf * 0.5


def _bounded_score(value: float) -> float:
    return min(1.0, max(0.0, float(value)))


class GoalControlPlane:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or _goal_control_root()
        self.snapshot_path = _goal_snapshot_path(self.root)
        self.ledger_path = _goal_ledger_path(self.root)
        self.lock_path = _goal_lock_path(self.root)

    def ensure_initialized(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.snapshot_path.exists():
            _atomic_write_json(self.snapshot_path, _default_snapshot())
        if not self.ledger_path.exists():
            self.ledger_path.touch()

    def read_snapshot(self) -> dict[str, Any]:
        self.ensure_initialized()
        try:
            raw = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        except Exception:
            return _default_snapshot()
        return _coerce_snapshot(raw)

    def read_ledger(self, limit: int = 100) -> list[dict[str, Any]]:
        self.ensure_initialized()
        # Read only the tail of the file: seek backward from EOF to avoid loading
        # the full ledger into memory when it grows large over time.
        _MAX_TAIL_BYTES = 256 * 1024  # 256 KB covers ~2000 typical entries
        rows: list[dict[str, Any]] = []
        try:
            size = self.ledger_path.stat().st_size
            with self.ledger_path.open("rb") as f:
                if size > _MAX_TAIL_BYTES:
                    f.seek(-_MAX_TAIL_BYTES, 2)
                    f.readline()  # skip partial first line
                raw = f.read().decode("utf-8", errors="replace")
        except OSError:
            return rows
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict):
                rows.append(row)
        if limit > 0:
            return rows[-limit:]
        return rows

    def migrate_legacy_goal(self, *, legacy_goal: str, legacy_source: str = "legacy") -> dict[str, Any]:
        self.ensure_initialized()
        current = self.read_snapshot()
        if current.get("version", 0) > 0 or current.get("text", ""):
            return current
        text = _normalize_text(legacy_goal)
        if not text:
            return current
        return self.ingest(
            event_type=EVENT_SYSTEM_GENERATE,
            source=(legacy_source or "legacy"),
            actor="migration",
            text=text,
            rationale=f"migrated from state.json source={legacy_source}",
            confidence=1.0,
            force=True,
        )["snapshot"]

    def ingest(
        self,
        *,
        event_type: str,
        source: str,
        actor: str,
        text: str,
        rationale: str = "",
        confidence: float = 0.5,
        ttl_ms: int = 0,
        lock_window_ms: int = 0,
        force: bool = False,
        target_event_id: str = "",
        context_match_score: float | None = None,
        recent_failure_risk: float | None = None,
    ) -> dict[str, Any]:
        self.ensure_initialized()
        if event_type not in _VALID_EVENT_TYPES:
            raise ValueError(f"invalid goal event_type: {event_type!r}")
        normalized_text = _normalize_text(text)
        if not normalized_text and event_type != EVENT_ROLLBACK_REQUEST:
            raise ValueError("goal text is required")

        accepted = False
        reason = "rejected_by_policy"
        decision_score = 0.0
        decision_breakdown: dict[str, Any] = {}
        now_ms = _now_ms()
        snapshot_after = self.read_snapshot()
        event_id = f"g-{now_ms}-{uuid.uuid4().hex[:8]}"

        with _file_lock(self.lock_path):
            current = self.read_snapshot()
            selected_text = normalized_text
            if event_type == EVENT_ROLLBACK_REQUEST:
                if target_event_id:
                    selected_text = self._lookup_text_by_event_id(target_event_id) or current.get("text", "")
                else:
                    selected_text = current.get("text", "")
            if not selected_text:
                reason = "empty_goal_after_resolution"
            else:
                candidate_context = _bounded_score(context_match_score if context_match_score is not None else 0.5)
                candidate_risk = _bounded_score(recent_failure_risk if recent_failure_risk is not None else 0.0)
                decision_score = (
                    _score_event(event_type, source, confidence)
                    + candidate_context * 0.35
                    + candidate_risk * 0.25
                )

                current_score = _score_event(EVENT_SYSTEM_REFINE, str(current.get("source", "unset")), 0.5)
                current_updated_at_ms = int(current.get("updated_at_ms", 0) or 0)
                current_age_ms = max(0, now_ms - current_updated_at_ms)
                # Time-decay lowers incumbent resistance over time, so stale goals
                # can be replaced by stronger context/risk-aligned proposals.
                decay = min(0.4, current_age_ms / float(7 * 24 * 60 * 60 * 1000) * 0.4)
                current_effective_score = max(0.0, current_score - decay)
                expires_at_ms = int(current.get("expires_at_ms", 0) or 0)
                if expires_at_ms > 0 and now_ms > expires_at_ms:
                    current_effective_score = 0.0

                decision_breakdown = {
                    "candidate_score": decision_score,
                    "candidate_context_match": candidate_context,
                    "candidate_recent_failure_risk": candidate_risk,
                    "current_score": current_score,
                    "current_decay": decay,
                    "current_effective_score": current_effective_score,
                    "current_age_ms": current_age_ms,
                }

                locked_until_ms = int(current.get("locked_until_ms", 0) or 0)
                if now_ms < locked_until_ms and source == "system" and not force:
                    reason = "blocked_by_human_lock_window"
                elif force or decision_score >= current_effective_score or not current.get("text", ""):
                    accepted = True
                    reason = "accepted"
                    next_version = int(current.get("version", 0)) + 1
                    ttl_ms = max(0, int(ttl_ms))
                    expires_at_ms = now_ms + ttl_ms if ttl_ms > 0 else 0
                    lock_window_ms = max(0, int(lock_window_ms))
                    snapshot_after = {
                        "goal_schema_version": GOAL_CONTROL_SCHEMA_VERSION,
                        "version": next_version,
                        "text": selected_text,
                        "source": source,
                        "decided_by": actor,
                        "rationale": rationale or f"{event_type}:{source}",
                        "updated_at_ms": now_ms,
                        "ttl_ms": ttl_ms,
                        "expires_at_ms": expires_at_ms,
                        "locked_until_ms": (
                            now_ms + lock_window_ms
                            if lock_window_ms > 0
                            else int(current.get("locked_until_ms", 0) or 0)
                        ),
                        "last_event_id": event_id,
                    }
                    _atomic_write_json(self.snapshot_path, snapshot_after)
                else:
                    snapshot_after = current
                    reason = "lower_priority_than_current_goal"

            ledger_event = {
                "event_schema_version": GOAL_EVENT_SCHEMA_VERSION,
                "event_id": event_id,
                "ts_ms": now_ms,
                "event_type": event_type,
                "source": source,
                "actor": actor,
                "text": selected_text,
                "rationale": rationale,
                "confidence": min(1.0, max(0.0, float(confidence))),
                "ttl_ms": max(0, int(ttl_ms)),
                "lock_window_ms": max(0, int(lock_window_ms)),
                "target_event_id": target_event_id,
                "decision": {
                    "accepted": accepted,
                    "reason": reason,
                    "score": decision_score,
                    "snapshot_version": int(snapshot_after.get("version", 0)),
                    "breakdown": decision_breakdown,
                },
            }
            _append_jsonl(self.ledger_path, ledger_event)

        return {
            "accepted": accepted,
            "reason": reason,
            "event_id": event_id,
            "snapshot": snapshot_after,
            "decision": {
                "score": decision_score,
                "breakdown": decision_breakdown,
            },
        }

    def rollback(self, *, target_event_id: str, actor: str, rationale: str = "") -> dict[str, Any]:
        target_text = self._lookup_text_by_event_id(target_event_id)
        if not target_text:
            raise ValueError(f"rollback target event not found: {target_event_id}")
        return self.ingest(
            event_type=EVENT_ROLLBACK_REQUEST,
            source="cockpit",
            actor=actor,
            text=target_text,
            rationale=rationale or f"rollback to {target_event_id}",
            confidence=1.0,
            force=True,
            target_event_id=target_event_id,
        )

    def _lookup_text_by_event_id(self, event_id: str) -> str:
        if not event_id:
            return ""
        for row in reversed(self.read_ledger(limit=0)):
            if str(row.get("event_id", "")) == event_id:
                return _normalize_text(row.get("text", ""))
        return ""
