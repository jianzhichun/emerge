from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from scripts.policy_config import (
    PROMOTE_MAX_HUMAN_FIX_RATE,
    PROMOTE_MIN_ATTEMPTS,
    PROMOTE_MIN_SUCCESS_RATE,
    ROLLBACK_CONSECUTIVE_FAILURES,
    STABLE_MIN_ATTEMPTS,
    STABLE_MIN_SUCCESS_RATE,
    WINDOW_SIZE,
)

# ── read-only tool classification ─────────────────────────────────────────────
# Conservative: unknown tools default to has_side_effects=True.
# icc_exec is intentionally absent — it is excluded from span recording entirely.

_READ_ONLY_TOOL_NAMES = frozenset({
    # Claude Code built-in read-only tools
    "Read", "Glob", "Grep", "WebFetch", "WebSearch", "ToolSearch",
    "TaskGet", "TaskList", "TaskOutput",
    "ListMcpResourcesTool", "ReadMcpResourceTool",
    "mcp__computer-use__screenshot", "mcp__computer-use__cursor_position",
    "mcp__computer-use__read_clipboard", "mcp__computer-use__list_granted_applications",
})
_READ_ONLY_TOOL_PREFIXES = ("mcp__context7__",)
_READ_ONLY_TOOL_SUFFIXES = (
    "__get", "__list", "__search", "__query", "__read", "__resolve",
    "__screenshot", "__cursor_position",
)


def is_read_only_tool(tool_name: str) -> bool:
    if tool_name in _READ_ONLY_TOOL_NAMES:
        return True
    for prefix in _READ_ONLY_TOOL_PREFIXES:
        if tool_name.startswith(prefix):
            return True
    for suffix in _READ_ONLY_TOOL_SUFFIXES:
        if tool_name.endswith(suffix):
            return True
    return False


# ── data classes ──────────────────────────────────────────────────────────────

@dataclass
class ActionRecord:
    seq: int
    tool_name: str
    args_hash: str
    has_side_effects: bool
    ts_ms: int

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "tool_name": self.tool_name,
            "args_hash": self.args_hash,
            "has_side_effects": self.has_side_effects,
            "ts_ms": self.ts_ms,
        }


@dataclass
class SpanRecord:
    span_id: str
    intent_signature: str
    description: str
    source: str  # "skill" | "manual"
    opened_at_ms: int
    is_read_only: bool = True
    skill_name: str | None = None
    closed_at_ms: int | None = None
    outcome: str | None = None
    args: dict = field(default_factory=dict)
    result_summary: dict = field(default_factory=dict)
    actions: list[ActionRecord] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "span_id": self.span_id,
            "intent_signature": self.intent_signature,
            "description": self.description,
            "source": self.source,
            "skill_name": self.skill_name,
            "opened_at_ms": self.opened_at_ms,
            "closed_at_ms": self.closed_at_ms,
            "outcome": self.outcome,
            "is_read_only": self.is_read_only,
            "args": self.args,
            "result_summary": self.result_summary,
            "actions": [a.to_dict() for a in self.actions],
        }


# ── SpanTracker ───────────────────────────────────────────────────────────────

class SpanTracker:
    """Manages intent span lifecycle: open → [actions] → close → WAL → candidates → policy."""

    def __init__(self, state_root: Path, hook_state_root: Path) -> None:
        self._state_root = state_root
        self._hook_state_root = hook_state_root
        # span-wal dir is created lazily on first write to avoid polluting state root

    # ── paths ──────────────────────────────────────────────────────────────

    def _wal_path(self) -> Path:
        return self._state_root / "span-wal" / "spans.jsonl"

    def _candidates_path(self) -> Path:
        return self._state_root / "span-candidates.json"

    def _buffer_path(self) -> Path:
        return self._hook_state_root / "active-span-actions.jsonl"

    def _state_path(self) -> Path:
        return self._hook_state_root / "state.json"

    # ── helpers ────────────────────────────────────────────────────────────

    def _atomic_write(self, path: Path, data: dict) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def _load_state(self) -> dict:
        p = self._state_path()
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except Exception:
            return {}

    def _load_candidates(self) -> dict:
        p = self._candidates_path()
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"spans": {}}
        except Exception:
            return {"spans": {}}

    # ── open ───────────────────────────────────────────────────────────────

    def open_span(
        self,
        intent_signature: str,
        description: str = "",
        args: dict | None = None,
        source: str = "manual",
        skill_name: str | None = None,
    ) -> SpanRecord:
        state = self._load_state()
        if state.get("active_span_id"):
            raise RuntimeError(
                f"active span already open: {state['active_span_id']} "
                f"({state.get('active_span_intent', '?')}). "
                "Call icc_span_close before opening a new span."
            )
        span = SpanRecord(
            span_id=str(uuid.uuid4()),
            intent_signature=intent_signature,
            description=description,
            source=source,
            skill_name=skill_name,
            opened_at_ms=int(time.time() * 1000),
            args=args or {},
        )
        state["active_span_id"] = span.span_id
        state["active_span_intent"] = intent_signature
        self._atomic_write(self._state_path(), state)
        self._buffer_path().write_text("", encoding="utf-8")
        return span

    # ── close ──────────────────────────────────────────────────────────────

    def close_span(
        self,
        span: SpanRecord,
        outcome: str,
        result_summary: dict | None = None,
    ) -> SpanRecord:
        span.closed_at_ms = int(time.time() * 1000)
        span.outcome = outcome
        span.result_summary = result_summary or {}

        # Collect actions from hook buffer
        buf = self._buffer_path()
        actions: list[ActionRecord] = []
        if buf.exists():
            for i, line in enumerate(buf.read_text(encoding="utf-8").splitlines()):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    actions.append(ActionRecord(
                        seq=i,
                        tool_name=str(rec.get("tool_name", "")),
                        args_hash=str(rec.get("args_hash", "")),
                        has_side_effects=bool(rec.get("has_side_effects", True)),
                        ts_ms=int(rec.get("ts_ms", 0)),
                    ))
                except Exception:
                    pass
        span.actions = actions
        span.is_read_only = all(not a.has_side_effects for a in actions)

        # Persist to WAL
        self._wal_path().parent.mkdir(parents=True, exist_ok=True)
        with self._wal_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(span.to_dict(), ensure_ascii=False) + "\n")

        # Update candidates
        self._update_candidates(span)

        # Clear hook state
        state = self._load_state()
        state.pop("active_span_id", None)
        state.pop("active_span_intent", None)
        self._atomic_write(self._state_path(), state)
        buf.unlink(missing_ok=True)

        return span

    # ── candidates / policy ────────────────────────────────────────────────

    def _update_candidates(self, span: SpanRecord) -> None:
        if not span.intent_signature:
            return
        candidates = self._load_candidates()
        key = span.intent_signature
        entry = candidates["spans"].get(key, {
            "intent_signature": key,
            "is_read_only": span.is_read_only,
            "description": span.description,
            "attempts": 0,
            "successes": 0,
            "human_fixes": 0,
            "consecutive_failures": 0,
            "recent_outcomes": [],
            "last_ts_ms": 0,
            "skeleton_generated": False,
            "frozen": False,
        })
        is_success = span.outcome == "success"
        entry["attempts"] += 1
        if is_success:
            entry["successes"] += 1
        entry["consecutive_failures"] = (
            0 if is_success else int(entry.get("consecutive_failures", 0)) + 1
        )
        recent = list(entry.get("recent_outcomes", []))
        recent.append(1 if is_success else 0)
        entry["recent_outcomes"] = recent[-WINDOW_SIZE:]
        entry["last_ts_ms"] = span.closed_at_ms or 0
        entry["is_read_only"] = span.is_read_only
        if span.description:
            entry["description"] = span.description
        candidates["spans"][key] = entry
        # Hard cap: evict oldest entries (by last_ts_ms) when over MAX_CANDIDATES.
        _MAX_CANDIDATES = 1000
        if len(candidates["spans"]) > _MAX_CANDIDATES:
            evict_count = len(candidates["spans"]) - _MAX_CANDIDATES
            sorted_keys = sorted(
                candidates["spans"],
                key=lambda k: (candidates["spans"][k].get("last_ts_ms", 0), k),
            )
            for evict_key in sorted_keys[:evict_count]:
                del candidates["spans"][evict_key]
        self._atomic_write(self._candidates_path(), candidates)

    def get_policy_status(self, intent_signature: str) -> str:
        """explore | canary | stable | rollback.
        Span policy intentionally omits verify_rate — spans have no verify step.
        """
        entry = self._load_candidates()["spans"].get(intent_signature, {})
        if not entry:
            return "explore"
        if entry.get("frozen"):
            return "explore"
        attempts = int(entry.get("attempts", 0))
        successes = int(entry.get("successes", 0))
        human_fixes = int(entry.get("human_fixes", 0))
        consecutive_failures = int(entry.get("consecutive_failures", 0))
        if consecutive_failures >= ROLLBACK_CONSECUTIVE_FAILURES:
            return "rollback"
        if attempts == 0:
            return "explore"
        success_rate = successes / attempts
        human_fix_rate = human_fixes / attempts
        if attempts >= STABLE_MIN_ATTEMPTS and success_rate >= STABLE_MIN_SUCCESS_RATE:
            return "stable"
        if (
            attempts >= PROMOTE_MIN_ATTEMPTS
            and success_rate >= PROMOTE_MIN_SUCCESS_RATE
            and human_fix_rate <= PROMOTE_MAX_HUMAN_FIX_RATE
        ):
            return "canary"
        return "explore"

    def is_synthesis_ready(self, intent_signature: str) -> bool:
        return self.get_policy_status(intent_signature) == "stable"

    def mark_skeleton_generated(self, intent_signature: str) -> None:
        """Record that a skeleton has been generated for this intent."""
        candidates = self._load_candidates()
        if intent_signature in candidates["spans"]:
            candidates["spans"][intent_signature]["skeleton_generated"] = True
            self._atomic_write(self._candidates_path(), candidates)

    def skeleton_already_generated(self, intent_signature: str) -> bool:
        return bool(
            self._load_candidates()["spans"]
            .get(intent_signature, {})
            .get("skeleton_generated", False)
        )

    def latest_successful_span(self, intent_signature: str) -> dict | None:
        """Return the most recent successful span from WAL."""
        wal = self._wal_path()
        if not wal.exists():
            return None
        best: dict | None = None
        best_ts = 0
        with wal.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if (
                    rec.get("intent_signature") == intent_signature
                    and rec.get("outcome") == "success"
                    and int(rec.get("closed_at_ms", 0)) >= best_ts
                ):
                    best = rec
                    best_ts = int(rec["closed_at_ms"])
        return best
