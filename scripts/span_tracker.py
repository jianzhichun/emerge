from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from scripts.intent_registry import IntentRegistry
from scripts.policy_config import atomic_write_json
from scripts.policy_engine import PolicyEngine, derive_stage

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
    """Manages intent span lifecycle: open → [actions] → close → WAL → policy.

    Stage transitions are delegated to :class:`PolicyEngine` — SpanTracker never
    writes the ``stage`` field directly. When no ``policy_engine`` is injected
    (read-only callers like reflection tools, hooks, CLI status), SpanTracker
    lazily builds a minimal no-side-effect engine sufficient for counter writes.
    """

    def __init__(
        self,
        state_root: Path,
        hook_state_root: Path,
        *,
        policy_engine: PolicyEngine | None = None,
    ) -> None:
        self._state_root = state_root
        self._hook_state_root = hook_state_root
        self._policy_engine = policy_engine
        self._own_lock = threading.Lock()
        # span-wal dir is created lazily on first write to avoid polluting state root

    # ── paths ──────────────────────────────────────────────────────────────

    def _wal_path(self) -> Path:
        return self._state_root / "span-wal" / "spans.jsonl"

    def _buffer_path(self) -> Path:
        return self._hook_state_root / "active-span-actions.jsonl"

    def _state_path(self) -> Path:
        return self._hook_state_root / "state.json"

    def _reflection_cache_path(self) -> Path:
        return self._state_root / "reflection-cache" / "global.json"

    @staticmethod
    def _cap_reflection_text(text: str, max_chars: int = 700) -> str:
        trimmed = str(text or "").strip()
        if len(trimmed) <= max_chars:
            return trimmed
        return trimmed[: max_chars - 3].rstrip() + "..."

    # ── helpers ────────────────────────────────────────────────────────────

    def _atomic_write(self, path: Path, data: dict) -> None:
        atomic_write_json(path, data, ensure_ascii=False)

    def _load_state(self) -> dict:
        p = self._state_path()
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except Exception:
            return {}

    def _load_candidates(self) -> dict:
        return IntentRegistry.load(self._state_root)

    def _save_candidates(self, data: dict) -> None:
        IntentRegistry.save(self._state_root, data)

    def _get_policy_engine(self) -> PolicyEngine:
        if self._policy_engine is None:
            self._policy_engine = PolicyEngine(
                state_root=lambda: self._state_root,
                lock=self._own_lock,
            )
        return self._policy_engine

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
        """Delegate stage/counter update to PolicyEngine.

        Span evidence carries no verify signal (span close has no separate
        verify step), so ``verify_observed=False`` — the verify gate defaults
        to 1.0 for span-only intents.
        """
        if not span.intent_signature:
            return
        self._get_policy_engine().apply_evidence(
            span.intent_signature,
            success=(span.outcome == "success"),
            evidence_unit_id=span.span_id,
            verify_observed=False,
            description=span.description,
            is_read_only=span.is_read_only,
            ts_ms=span.closed_at_ms or int(time.time() * 1000),
        )

    def get_policy_status(self, intent_signature: str) -> str:
        """Return current lifecycle stage (read-only).

        Reads the persisted ``stage`` field written by PolicyEngine, falling
        back to a pure re-derivation if the field is missing on an old row.
        """
        entry = self._load_candidates()["intents"].get(intent_signature, {})
        if not entry:
            return "explore"
        return str(entry.get("stage") or derive_stage(entry))

    def is_synthesis_ready(self, intent_signature: str) -> bool:
        """True when a *span skeleton* should be generated for this intent.

        Span-path synthesis fires at ``stage == stable`` — we wait until the
        intent has fully proven itself before converting WAL→skeleton. This is
        intentionally *different* from the PolicyEngine ``synthesis_ready``
        flag, which fires at ``stage == canary`` for exec-path auto-crystallize
        (exec WAL carries explicit code, so we can crystallize earlier).
        """
        return self.get_policy_status(intent_signature) == "stable"

    def mark_skeleton_generated(self, intent_signature: str) -> None:
        """Record that a skeleton has been generated for this intent."""
        candidates = self._load_candidates()
        if intent_signature in candidates["intents"]:
            candidates["intents"][intent_signature]["skeleton_generated"] = True
            self._save_candidates(candidates)

    def skeleton_already_generated(self, intent_signature: str) -> bool:
        return bool(
            self._load_candidates()["intents"]
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

    def format_reflection(self, max_intents: int = 8) -> str:
        """Build a compact muscle-memory summary for hook context injection."""
        candidates = self._load_candidates().get("intents", {})
        if not candidates:
            return (
                "Muscle memory: no learned patterns yet.\n"
                "Open a span before your next tool use — "
                'icc_span_open(intent_signature="connector.mode.name") '
                "→ execute → icc_span_close(outcome=success|failure|aborted). "
                "A few repetitions auto-promote the pattern to zero-LLM execution."
            )

        stable: list[str] = []
        canary: list[str] = []
        demotions: list[tuple[int, str, str, str, str]] = []  # (ts_ms, sig, to_stage, reason, fingerprint)
        synthesis_skipped: list[tuple[str, str]] = []  # (sig, reason)
        for sig, entry in candidates.items():
            # emerge.* intents are internal development spans — never bridgeable,
            # never repeatable by operator-Claude. Excluding them from reflection
            # prevents the flywheel from filling its own context with noise.
            if sig.startswith("emerge."):
                continue
            status = self.get_policy_status(sig)
            if status == "stable":
                stable.append(sig)
            elif status == "canary":
                canary.append(sig)
            if isinstance(entry, dict):
                demo = entry.get("last_demotion")
                if isinstance(demo, dict):
                    demotions.append((
                        int(demo.get("ts_ms", 0) or 0),
                        sig,
                        str(demo.get("to_stage", "") or ""),
                        str(demo.get("reason", "") or ""),
                        str(demo.get("bridge_failure_exception", "") or ""),
                    ))
                skipped = str(entry.get("synthesis_skipped_reason", "") or "")
                if skipped:
                    synthesis_skipped.append((sig, skipped))

        recent: dict[str, dict[str, int]] = {}
        wal = self._wal_path()
        if wal.exists():
            try:
                rows = wal.read_text(encoding="utf-8").splitlines()[-20:]
            except OSError:
                rows = []
            for line in rows:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                sig = str(rec.get("intent_signature", "")).strip()
                if not sig or sig.startswith("emerge."):
                    continue
                entry = recent.setdefault(sig, {"ok": 0, "fail": 0})
                if rec.get("outcome") == "success":
                    entry["ok"] += 1
                else:
                    entry["fail"] += 1

        parts: list[str] = []
        if stable:
            parts.append(
                "Stable (auto-bridge): " + ", ".join(sorted(stable)[:max_intents])
            )
        if canary:
            parts.append("Canary: " + ", ".join(sorted(canary)[:3]))
        if recent:
            recent_rows: list[str] = []
            for sig in sorted(recent)[:5]:
                _rec = recent[sig]
                recent_rows.append(f"{sig} {_rec['ok']}ok/{_rec['fail']}fail")
            parts.append("Recent: " + ", ".join(recent_rows))
        if demotions:
            # Newest demotions first — next session should see the freshest failure reasons.
            demotions.sort(key=lambda x: x[0], reverse=True)
            demo_rows: list[str] = []
            for _ts, sig, to_stage, reason, fingerprint in demotions[:3]:
                tag = to_stage or "demoted"
                detail = reason
                if fingerprint:
                    detail = f"{reason}:{fingerprint}" if reason else fingerprint
                if detail:
                    demo_rows.append(f"{sig}→{tag} ({detail})")
                else:
                    demo_rows.append(f"{sig}→{tag}")
            parts.append("Demoted: " + "; ".join(demo_rows))
        if synthesis_skipped:
            # Surface so next session knows WHY crystallization refused — otherwise
            # the intent stays stuck as canary with no pipeline forever.
            skipped_rows = [f"{sig} ({reason})" for sig, reason in sorted(synthesis_skipped)[:3]]
            parts.append("Synthesis blocked: " + "; ".join(skipped_rows))
        if not parts:
            return ""
        return self._cap_reflection_text("Muscle memory\n" + "\n".join(parts))

    def load_reflection_cache(self, ttl_ms: int = 15 * 60 * 1000) -> str:
        """Return cached deep reflection if present and fresh; otherwise empty."""
        cache_path = self._reflection_cache_path()
        if not cache_path.exists():
            return ""
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            return ""
        generated_at = int(data.get("generated_at_ms", 0) or 0)
        summary = self._cap_reflection_text(str(data.get("summary_text", "") or ""))
        if not summary:
            return ""
        now_ms = int(time.time() * 1000)
        if ttl_ms > 0 and generated_at > 0 and now_ms - generated_at > ttl_ms:
            return ""
        return summary

    def write_reflection_cache(self, summary_text: str, meta: dict | None = None) -> None:
        """Write deep reflection cache for hook-side fast reads."""
        summary = self._cap_reflection_text(summary_text)
        if not summary:
            return
        cache_path = self._reflection_cache_path()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at_ms": int(time.time() * 1000),
            "summary_text": summary,
            "meta": meta or {},
        }
        self._atomic_write(cache_path, payload)

    def format_reflection_with_cache(
        self,
        max_intents: int = 8,
        cache_ttl_ms: int = 15 * 60 * 1000,
    ) -> str:
        """Prefer fresh deep cache; fallback to local lightweight reflection."""
        cached = self.load_reflection_cache(ttl_ms=cache_ttl_ms)
        if cached:
            return cached
        return self.format_reflection(max_intents=max_intents)
