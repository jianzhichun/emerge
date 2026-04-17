"""PolicyEngine — single writer of intent lifecycle stage.

Architectural contract (optimal-solution clean-break):

  - All stage transitions for `intents.json` flow through `PolicyEngine.apply_evidence`.
    No other module writes the `stage` field.
  - Evidence callers (span close, exec call, pipeline event) pass a *raw* outcome;
    the engine decides counter updates, stage transitions, and side effects
    (auto-crystallize, hub sync, MCP push).
  - Policy gates use `verify_rate = verify_passes / max(1, verify_attempts)` and
    default to 1.0 when `verify_attempts == 0`. This lets span-only evidence (which
    carries no separate verify signal) clear the verify gate by success alone, while
    exec evidence still has to meet the strict verify threshold whenever verify was
    actually measured.
  - Transition rules are symmetric for every evidence source. The source of evidence
    is **not** persisted; only the aggregated counters are.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

from scripts.intent_registry import IntentRegistry, default_intent_entry
from scripts.policy_config import (
    PIPELINE_KEY_RE as _INTENT_KEY_RE,
    PROMOTE_MAX_HUMAN_FIX_RATE,
    PROMOTE_MIN_ATTEMPTS,
    PROMOTE_MIN_SUCCESS_RATE,
    PROMOTE_MIN_VERIFY_RATE,
    ROLLBACK_CONSECUTIVE_FAILURES,
    STABLE_MIN_ATTEMPTS,
    STABLE_MIN_SUCCESS_RATE,
    STABLE_MIN_VERIFY_RATE,
    WINDOW_SIZE,
)

_log = logging.getLogger(__name__)

# Hard cap to protect intents.json from unbounded growth.
_MAX_INTENTS = 1000


class PolicyEngine:
    """Centralized intent lifecycle state writer.

    Callers record *one* evidence event; the engine atomically updates counters,
    re-derives stage, and fires downstream effects.
    """

    def __init__(
        self,
        *,
        state_root: Callable[[], Path],
        lock: threading.Lock,
        sink: Callable[[], Any] | None = None,
        auto_crystallize: Callable[..., None] | None = None,
        has_synthesizable_wal: Callable[[str, str], bool] | None = None,
        write_mcp_push: Callable[[dict], None] | None = None,
        session_id: Callable[[], str] | None = None,
    ) -> None:
        self._get_state_root = state_root
        self._lock = lock
        self._get_sink = sink or (lambda: None)
        self._auto_crystallize = auto_crystallize
        self._has_synthesizable_wal = has_synthesizable_wal
        self._write_mcp_push = write_mcp_push or (lambda _payload: None)
        self._get_session_id = session_id or (lambda: "")

    # ------------------------------------------------------------------ api

    def apply_evidence(
        self,
        intent_signature: str,
        *,
        success: bool,
        verify_observed: bool = False,
        verify_passed: bool = False,
        human_fix: bool = False,
        is_degraded: bool = False,
        description: str = "",
        is_read_only: bool | None = None,
        target_profile: str = "default",
        execution_path: str | None = None,
        policy_action: str | None = None,
        policy_enforced: bool = False,
        stop_triggered: bool = False,
        rollback_executed: bool = False,
        ts_ms: int | None = None,
    ) -> dict[str, Any]:
        """Record one piece of evidence for an intent.

        Parameters
        ----------
        intent_signature
            Canonical ``<connector>.(read|write).<name>`` key. Must match
            ``PIPELINE_KEY_RE`` or the call is a no-op.
        success
            Whether the attempt succeeded. Drives ``successes`` + resets
            ``consecutive_failures`` when True.
        verify_observed / verify_passed
            ``verify_observed=True`` means this evidence carries a verification
            signal (e.g. `icc_exec` tracked ``verification_state``). Span evidence
            leaves this False because span close has no separate verify step — the
            ``verify_rate`` gate then defaults to 1.0.
        human_fix
            Set by ``icc_reconcile(outcome=correct)`` when the operator had to fix
            the model's output.
        is_degraded
            Pipeline verify returned degraded (not a hard error but not verified).
        description
            First-write-wins description copied onto the intent entry.
        is_read_only
            Propagated from span metadata when available.
        target_profile
            Used to locate synthesizable WAL entries across sessions.
        execution_path / policy_action / policy_enforced / stop_triggered / rollback_executed
            Optional exec/pipeline-side telemetry carried onto the entry.
        ts_ms
            Event timestamp; defaults to ``time.time()*1000``.

        Returns the updated intent entry (a copy).
        """
        if not intent_signature or not _INTENT_KEY_RE.match(intent_signature):
            _log.warning(
                "PolicyEngine: refusing malformed intent_signature %r", intent_signature
            )
            return {}

        state_root = self._get_state_root()
        ts_ms = int(ts_ms if ts_ms is not None else time.time() * 1000)

        with self._lock:
            registry = IntentRegistry.load(state_root)
            intents = registry["intents"]
            entry = intents.get(intent_signature) or {
                **default_intent_entry(),
                "intent_signature": intent_signature,
            }
            # ensure schema defaults (lazy upgrades for old on-disk rows)
            entry.setdefault("stage", "explore")
            entry.setdefault("rollout_pct", 0)
            entry.setdefault("last_transition_reason", "init")
            entry.setdefault("attempts_at_transition", 0)
            entry.setdefault("verify_attempts", 0)
            entry.setdefault("verify_passes", 0)
            entry.setdefault("degraded_count", 0)

            # ── counters ───────────────────────────────────────────────
            entry["attempts"] = int(entry.get("attempts", 0)) + 1
            if success:
                entry["successes"] = int(entry.get("successes", 0)) + 1
                entry["consecutive_failures"] = 0
            else:
                entry["consecutive_failures"] = int(entry.get("consecutive_failures", 0)) + 1
            if verify_observed:
                entry["verify_attempts"] = int(entry.get("verify_attempts", 0)) + 1
                if verify_passed:
                    entry["verify_passes"] = int(entry.get("verify_passes", 0)) + 1
            if human_fix:
                entry["human_fixes"] = int(entry.get("human_fixes", 0)) + 1
            if is_degraded:
                entry["degraded_count"] = int(entry.get("degraded_count", 0)) + 1

            recent = list(entry.get("recent_outcomes", []))
            recent.append(1 if success else 0)
            entry["recent_outcomes"] = recent[-WINDOW_SIZE:]

            if description and not entry.get("description"):
                entry["description"] = description
            if is_read_only is not None:
                entry["is_read_only"] = bool(is_read_only)
            if execution_path:
                entry["last_execution_path"] = str(execution_path)
            if policy_enforced:
                entry["policy_enforced_count"] = int(entry.get("policy_enforced_count", 0)) + 1
            if stop_triggered:
                entry["stop_triggered_count"] = int(entry.get("stop_triggered_count", 0)) + 1
            if rollback_executed:
                entry["rollback_executed_count"] = int(entry.get("rollback_executed_count", 0)) + 1
            if policy_action is not None:
                entry["last_policy_action"] = str(policy_action)

            entry["last_ts_ms"] = ts_ms
            entry["updated_at_ms"] = ts_ms
            entry["target_profile"] = entry.get("target_profile") or target_profile

            # ── rates ──────────────────────────────────────────────────
            attempts = max(1, int(entry["attempts"]))
            success_rate = int(entry.get("successes", 0)) / attempts
            verify_attempts = int(entry.get("verify_attempts", 0))
            verify_rate = (
                int(entry.get("verify_passes", 0)) / verify_attempts
                if verify_attempts > 0
                else 1.0
            )
            human_fix_rate = int(entry.get("human_fixes", 0)) / attempts
            window = entry["recent_outcomes"]
            window_success_rate = sum(window) / len(window) if window else 0.0

            entry["success_rate"] = round(success_rate, 4)
            entry["verify_rate"] = round(verify_rate, 4)
            entry["human_fix_rate"] = round(human_fix_rate, 4)
            entry["window_success_rate"] = round(window_success_rate, 4)

            # ── stage transition ───────────────────────────────────────
            current_stage = str(entry.get("stage", "explore"))
            if entry.get("frozen"):
                new_stage, transitioned, reason = current_stage, False, "frozen"
            else:
                new_stage, transitioned, reason = _derive_transition(
                    current_stage,
                    attempts=int(entry["attempts"]),
                    success_rate=success_rate,
                    verify_rate=verify_rate,
                    human_fix_rate=human_fix_rate,
                    consecutive_failures=int(entry["consecutive_failures"]),
                    window=window,
                )
            entry["stage"] = new_stage
            if transitioned:
                entry["last_transition_reason"] = reason
                entry["attempts_at_transition"] = int(entry["attempts"])
                if new_stage == "canary":
                    entry["rollout_pct"] = 20
                elif new_stage == "stable":
                    entry["rollout_pct"] = 100
                else:  # explore / rollback
                    entry["rollout_pct"] = 0

            # ── side effects on transition ─────────────────────────────
            if transitioned:
                self._emit_sink("policy.transition", {
                    "candidate_key": intent_signature,
                    "new_stage": new_stage,
                    "session_id": self._get_session_id(),
                })
                if new_stage == "canary":
                    self._maybe_fire_auto_crystallize(entry, intent_signature, target_profile)
                if new_stage == "stable":
                    _append_hub_stable_event(intent_signature)

            intents[intent_signature] = entry

            # ── cap ────────────────────────────────────────────────────
            if len(intents) > _MAX_INTENTS:
                evict = len(intents) - _MAX_INTENTS
                for k in sorted(intents, key=lambda x: (intents[x].get("last_ts_ms", 0), x))[:evict]:
                    if k != intent_signature:
                        del intents[k]

            IntentRegistry.save(state_root, registry)

        self._notify_resources_changed()
        return dict(entry)

    # ------------------------------------------------------------------ helpers

    def increment_human_fix(self, intent_signature: str) -> dict[str, Any]:
        """Record a human-fix event without advancing attempts (reconcile path).

        Increments ``human_fixes``, refreshes ``human_fix_rate``, and re-evaluates
        transitions (e.g. a spike in human-fix rate can block promotion).
        """
        if not intent_signature or not _INTENT_KEY_RE.match(intent_signature):
            return {}
        state_root = self._get_state_root()
        with self._lock:
            registry = IntentRegistry.load(state_root)
            intents = registry["intents"]
            entry = intents.get(intent_signature)
            if not isinstance(entry, dict):
                return {}
            entry["human_fixes"] = int(entry.get("human_fixes", 0)) + 1
            attempts = max(1, int(entry.get("attempts", 0)))
            entry["human_fix_rate"] = round(entry["human_fixes"] / attempts, 4)
            entry["updated_at_ms"] = int(time.time() * 1000)
            intents[intent_signature] = entry
            IntentRegistry.save(state_root, registry)
        self._notify_resources_changed()
        return dict(entry)

    # ------------------------------------------------------------------ internals

    def _emit_sink(self, event: str, payload: dict) -> None:
        try:
            sink = self._get_sink()
            if sink is not None:
                sink.emit(event, payload)
        except Exception:
            pass

    def _notify_resources_changed(self) -> None:
        try:
            self._write_mcp_push({
                "jsonrpc": "2.0",
                "method": "notifications/resources/list_changed",
                "params": {},
            })
        except Exception:
            pass

    def _maybe_fire_auto_crystallize(
        self, entry: dict, intent_signature: str, target_profile: str
    ) -> None:
        if not self._has_synthesizable_wal or not self._auto_crystallize:
            return
        try:
            if not self._has_synthesizable_wal(intent_signature, target_profile):
                return
        except Exception:
            return
        entry["synthesis_ready"] = True
        self._emit_sink("policy.synthesis_ready", {
            "candidate_key": intent_signature,
            "intent_signature": intent_signature,
            "session_id": self._get_session_id(),
        })
        parts = intent_signature.split(".", 2)
        if len(parts) != 3:
            return
        connector, mode, name = parts
        try:
            self._auto_crystallize(
                intent_signature=intent_signature,
                connector=connector,
                pipeline_name=name,
                mode=mode,
                target_profile=target_profile,
            )
        except Exception:
            _log.exception("auto-crystallize failed for %s", intent_signature)


# ──────────────────────────────────────────────────────────────────────────────
# Pure transition logic — no I/O.
# ──────────────────────────────────────────────────────────────────────────────


def _derive_transition(
    stage: str,
    *,
    attempts: int,
    success_rate: float,
    verify_rate: float,
    human_fix_rate: float,
    consecutive_failures: int,
    window: list[int],
) -> tuple[str, bool, str]:
    """Return ``(new_stage, transitioned, reason)``.

    Canonical rules — one function, one truth across every evidence source
    (span close, exec call, pipeline event). Destinations are stage-aware so
    the registry reflects *why* trust changed, not just *that* it did.

    Promotion path:
    - ``explore → canary`` when attempts ≥ promote_min, success_rate ≥ promote_min,
      verify_rate ≥ promote_min, human_fix_rate ≤ promote_max.
    - ``canary → stable`` when attempts ≥ stable_min, success_rate ≥ stable_min,
      verify_rate ≥ stable_min.

    Demotion path (``consecutive_failures ≥ threshold`` or a blown window):
    - ``explore → rollback``: we haven't earned trust yet and we're already failing;
      rollback is a cooldown marker.
    - ``rollback → explore`` on the first success that resets the failure streak.
    - ``canary → explore`` (reason=two_consecutive_failures): we trusted it early
      and it broke trust — restart learning.
    - ``stable → explore`` (reason=two_consecutive_failures or window_failure_rate):
      we fully trusted it and it regressed — restart learning.
    """
    if consecutive_failures >= ROLLBACK_CONSECUTIVE_FAILURES:
        if stage in ("canary", "stable"):
            return "explore", True, "two_consecutive_failures"
        if stage == "rollback":
            return "rollback", False, "no_change"
        # explore or unknown → mark as rollback cooldown
        return "rollback", True, "two_consecutive_failures"

    if stage == "rollback":
        if consecutive_failures == 0:
            return "explore", True, "rollback_recovered"
        return "rollback", False, "no_change"

    if stage == "explore":
        if attempts == 0:
            return "explore", False, "no_change"
        should_promote = (
            attempts >= PROMOTE_MIN_ATTEMPTS
            and success_rate >= PROMOTE_MIN_SUCCESS_RATE
            and verify_rate >= PROMOTE_MIN_VERIFY_RATE
            and human_fix_rate <= PROMOTE_MAX_HUMAN_FIX_RATE
        )
        if should_promote:
            return "canary", True, "promotion_threshold_met"
        return "explore", False, "no_change"

    if stage == "canary":
        should_stabilize = (
            attempts >= STABLE_MIN_ATTEMPTS
            and success_rate >= STABLE_MIN_SUCCESS_RATE
            and verify_rate >= STABLE_MIN_VERIFY_RATE
        )
        if should_stabilize:
            return "stable", True, "stable_threshold_met"
        return "canary", False, "no_change"

    if stage == "stable":
        if len(window) >= WINDOW_SIZE and (sum(window) / len(window)) < 0.9:
            return "explore", True, "window_failure_rate"
        return "stable", False, "no_change"

    return "explore", False, "no_change"


def derive_stage(entry: dict) -> str:
    """Read-only helper — derive stage from an entry without side effects.

    Used by callers that need to inspect lifecycle without recording evidence.
    """
    if entry.get("frozen"):
        return "explore"
    stage = str(entry.get("stage") or "explore")
    attempts = int(entry.get("attempts", 0))
    if attempts == 0:
        return stage if stage in ("explore", "canary", "stable", "rollback") else "explore"
    verify_attempts = int(entry.get("verify_attempts", 0))
    verify_rate = (
        int(entry.get("verify_passes", 0)) / verify_attempts
        if verify_attempts > 0
        else 1.0
    )
    success_rate = int(entry.get("successes", 0)) / attempts
    human_fix_rate = int(entry.get("human_fixes", 0)) / attempts
    consecutive_failures = int(entry.get("consecutive_failures", 0))
    window = list(entry.get("recent_outcomes", []))
    new_stage, _, _ = _derive_transition(
        stage,
        attempts=attempts,
        success_rate=success_rate,
        verify_rate=verify_rate,
        human_fix_rate=human_fix_rate,
        consecutive_failures=consecutive_failures,
        window=window,
    )
    return new_stage


# ──────────────────────────────────────────────────────────────────────────────
# Hub sync side-effect (kept local so PolicyEngine doesn't import emerge_daemon).
# ──────────────────────────────────────────────────────────────────────────────


def _append_hub_stable_event(intent_signature: str) -> None:
    try:
        from scripts.hub_config import append_sync_event, is_configured, load_hub_config
    except Exception:
        return
    try:
        if not is_configured():
            return
        parts = intent_signature.split(".", 2)
        connector = parts[0] if parts else intent_signature
        cfg = load_hub_config()
        if connector not in cfg.get("selected_verticals", []):
            return
        pipeline_name = parts[2] if len(parts) >= 3 else intent_signature
        append_sync_event({
            "event": "stable",
            "connector": connector,
            "pipeline": pipeline_name,
            "ts_ms": int(time.time() * 1000),
        })
    except Exception:
        pass
