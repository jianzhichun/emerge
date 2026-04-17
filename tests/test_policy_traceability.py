"""Tests for policy transition traceability and rollback attribution.

Covers:
  - `transition_history` grows on every stage change only (not every evidence event).
  - History bounded by `TRANSITION_HISTORY_MAX`.
  - `last_demotion` populated on `explore→rollback`, `canary→explore`, `stable→explore`
    but *not* on promotions or on `rollback→explore` recovery.
  - `cmd_control_plane_intents` surfaces `last_transition_*` + `last_demotion`.
  - `cmd_control_plane_intent_history` returns bounded history per key.
  - Cockpit `/api/control-plane/intent-history` route wires through.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from scripts.intent_registry import IntentRegistry, registry_path
from scripts.policy_config import TRANSITION_HISTORY_MAX
from scripts.policy_engine import PolicyEngine, _is_demotion


def _fresh_engine(state_root: Path, *, session_id: str = "sess-test") -> PolicyEngine:
    state_root.mkdir(parents=True, exist_ok=True)
    return PolicyEngine(
        state_root=lambda: state_root,
        lock=threading.Lock(),
        session_id=lambda: session_id,
    )


# ── unit: demotion classifier ────────────────────────────────────────────────

def test_is_demotion_rules() -> None:
    assert _is_demotion("stable", "explore") is True
    assert _is_demotion("canary", "explore") is True
    assert _is_demotion("explore", "rollback") is True
    assert _is_demotion("stable", "canary") is True
    # Recovery is not a demotion.
    assert _is_demotion("rollback", "explore") is False
    # Promotions are not demotions.
    assert _is_demotion("explore", "canary") is False
    assert _is_demotion("canary", "stable") is False
    # No change is not a demotion.
    assert _is_demotion("stable", "stable") is False


# ── integration: transition history append/cap ──────────────────────────────

def test_transition_history_appended_on_stage_change_only(tmp_path: Path) -> None:
    from scripts.policy_config import PROMOTE_MIN_ATTEMPTS

    engine = _fresh_engine(tmp_path)
    key = "gmail.read.fetch"

    # PROMOTE_MIN_ATTEMPTS - 1 successes in explore: below promotion threshold.
    for _ in range(PROMOTE_MIN_ATTEMPTS - 1):
        engine.apply_evidence(key, success=True, verify_observed=True, verify_passed=True)

    data = IntentRegistry.load(tmp_path)
    entry = data["intents"][key]
    assert entry["stage"] == "explore"
    assert entry["transition_history"] == [], \
        "no transition has happened yet — history should be empty"

    # One more success trips explore → canary.
    engine.apply_evidence(key, success=True, verify_observed=True, verify_passed=True)
    entry = IntentRegistry.load(tmp_path)["intents"][key]
    assert entry["stage"] == "canary"
    assert len(entry["transition_history"]) == 1
    record = entry["transition_history"][0]
    assert record["from_stage"] == "explore"
    assert record["to_stage"] == "canary"
    assert record["reason"] == "promotion_threshold_met"
    assert record["attempts"] == PROMOTE_MIN_ATTEMPTS
    assert record["session_id"] == "sess-test"
    assert entry["last_transition_reason"] == "promotion_threshold_met"
    assert entry["last_transition_ts_ms"] > 0
    # Promotion is not a demotion.
    assert entry["last_demotion"] is None


def test_transition_history_capped(tmp_path: Path) -> None:
    """Simulate enough demotion/recovery cycles to exceed history cap."""
    engine = _fresh_engine(tmp_path)
    key = "gmail.read.fetch"

    # Seed an explore row that is on the edge (one success away from canary
    # promotion), then bounce between rollback and explore repeatedly.
    reg_path = registry_path(tmp_path)
    reg_path.parent.mkdir(parents=True, exist_ok=True)
    import json
    reg_path.write_text(json.dumps({
        "intents": {
            key: {
                "stage": "explore",
                "attempts": 0,
                "successes": 0,
                "consecutive_failures": 0,
                "recent_outcomes": [],
                "verify_attempts": 0,
                "verify_passes": 0,
                "human_fixes": 0,
            }
        }
    }), encoding="utf-8")

    # Alternate 2 failures (→ rollback) then 1 success (→ explore) for enough
    # cycles to overshoot TRANSITION_HISTORY_MAX (bounded to last N).
    cycles = TRANSITION_HISTORY_MAX + 5
    for _ in range(cycles):
        engine.apply_evidence(key, success=False)
        engine.apply_evidence(key, success=False)  # → rollback on 2nd failure
        engine.apply_evidence(key, success=True)   # rollback → explore

    entry = IntentRegistry.load(tmp_path)["intents"][key]
    history = entry["transition_history"]
    assert len(history) == TRANSITION_HISTORY_MAX, \
        f"history must be bounded at {TRANSITION_HISTORY_MAX}, got {len(history)}"
    # Latest entry should be a valid transition record.
    assert history[-1]["to_stage"] in {"rollback", "explore"}
    assert "ts_ms" in history[-1]
    assert "reason" in history[-1]


# ── bridge-broken auto-demotion ──────────────────────────────────────────────

def test_record_bridge_outcome_demotes_stable_on_repeated_failure(tmp_path: Path) -> None:
    """A stable pipeline whose bridge keeps failing (while LLM fallback masks
    it as a success on the *intent*) must be auto-demoted from stable →
    canary with reason 'bridge_broken'. Without this, a broken crystallized
    pipeline burns LLM on every call forever — direct North Star violation."""
    from scripts.policy_config import BRIDGE_BROKEN_THRESHOLD

    engine = _fresh_engine(tmp_path)
    key = "gmail.read.fetch"

    # Seed an intent at stable directly (we're testing the bridge path, not
    # how it got to stable).
    reg_path = registry_path(tmp_path)
    reg_path.parent.mkdir(parents=True, exist_ok=True)
    import json
    reg_path.write_text(json.dumps({
        "intents": {
            key: {
                "intent_signature": key,
                "stage": "stable",
                "attempts": 50,
                "successes": 50,
                "success_rate": 1.0,
                "verify_rate": 1.0,
                "human_fix_rate": 0.0,
                "window_success_rate": 1.0,
                "consecutive_failures": 0,
                "recent_outcomes": [1] * 20,
                "transition_history": [],
                "last_demotion": None,
            }
        }
    }), encoding="utf-8")

    # Below the threshold: stage should stay stable.
    for _ in range(BRIDGE_BROKEN_THRESHOLD - 1):
        engine.record_bridge_outcome(key, success=False, reason="ImportError")
    entry = IntentRegistry.load(tmp_path)["intents"][key]
    assert entry["stage"] == "stable"
    assert entry["bridge_failure_streak"] == BRIDGE_BROKEN_THRESHOLD - 1

    # One more failure crosses the threshold → demote.
    engine.record_bridge_outcome(key, success=False, reason="ImportError")
    entry = IntentRegistry.load(tmp_path)["intents"][key]
    assert entry["stage"] == "canary", "bridge broken at threshold must demote stable→canary"
    assert entry["last_transition_reason"] == "bridge_broken"
    demo = entry["last_demotion"]
    assert demo is not None
    assert demo["from_stage"] == "stable"
    assert demo["to_stage"] == "canary"
    assert demo["reason"] == "bridge_broken"


def test_record_bridge_outcome_success_resets_streak(tmp_path: Path) -> None:
    engine = _fresh_engine(tmp_path)
    key = "gmail.read.fetch"
    reg_path = registry_path(tmp_path)
    reg_path.parent.mkdir(parents=True, exist_ok=True)
    import json
    reg_path.write_text(json.dumps({
        "intents": {
            key: {
                "intent_signature": key,
                "stage": "stable",
                "attempts": 50,
                "successes": 50,
                "recent_outcomes": [1] * 20,
                "bridge_failure_streak": 0,
            }
        }
    }), encoding="utf-8")

    engine.record_bridge_outcome(key, success=False, reason="boom")
    entry = IntentRegistry.load(tmp_path)["intents"][key]
    assert entry["bridge_failure_streak"] == 1
    # A successful bridge run clears the streak.
    engine.record_bridge_outcome(key, success=True)
    entry = IntentRegistry.load(tmp_path)["intents"][key]
    assert entry["bridge_failure_streak"] == 0
    assert entry["stage"] == "stable"


def test_record_bridge_outcome_no_op_on_missing_intent(tmp_path: Path) -> None:
    """Bridge evidence for an unknown intent must be a no-op, not create a
    rogue entry. Intent creation is the job of apply_evidence."""
    engine = _fresh_engine(tmp_path)
    engine.record_bridge_outcome("no.read.such", success=False)
    data = IntentRegistry.load(tmp_path)
    assert "no.read.such" not in data["intents"]


# ── last_demotion attribution ───────────────────────────────────────────────

def test_last_demotion_populated_on_explore_to_rollback(tmp_path: Path) -> None:
    engine = _fresh_engine(tmp_path)
    key = "gmail.read.fetch"

    engine.apply_evidence(key, success=False, target_profile="mycader-1")
    engine.apply_evidence(
        key, success=False, target_profile="mycader-1", execution_path="exec"
    )

    entry = IntentRegistry.load(tmp_path)["intents"][key]
    assert entry["stage"] == "rollback"
    demotion = entry["last_demotion"]
    assert demotion is not None
    assert demotion["from_stage"] == "explore"
    assert demotion["to_stage"] == "rollback"
    assert demotion["reason"] == "two_consecutive_failures"
    assert demotion["target_profile"] == "mycader-1"
    assert demotion["execution_path"] == "exec"


def test_last_demotion_not_touched_by_recovery(tmp_path: Path) -> None:
    """rollback → explore must not overwrite last_demotion."""
    engine = _fresh_engine(tmp_path)
    key = "gmail.read.fetch"

    engine.apply_evidence(key, success=False)
    engine.apply_evidence(key, success=False)  # → rollback
    before = IntentRegistry.load(tmp_path)["intents"][key]["last_demotion"]
    assert before is not None

    engine.apply_evidence(key, success=True)  # rollback → explore (recovery)

    entry = IntentRegistry.load(tmp_path)["intents"][key]
    assert entry["stage"] == "explore"
    # last_demotion must be preserved — recovery is not a demotion.
    assert entry["last_demotion"] == before


def test_last_demotion_overwritten_on_new_demotion(tmp_path: Path) -> None:
    engine = _fresh_engine(tmp_path)
    key = "gmail.read.fetch"

    # First demotion.
    engine.apply_evidence(key, success=False)
    engine.apply_evidence(key, success=False)
    first = IntentRegistry.load(tmp_path)["intents"][key]["last_demotion"]
    assert first is not None
    first_ts = first["ts_ms"]

    # Recover, then demote again.
    engine.apply_evidence(key, success=True)  # rollback → explore
    import time as _t
    _t.sleep(0.002)  # ensure later ts_ms
    engine.apply_evidence(key, success=False)
    engine.apply_evidence(key, success=False)

    second = IntentRegistry.load(tmp_path)["intents"][key]["last_demotion"]
    assert second is not None
    assert second["ts_ms"] > first_ts
    assert second["from_stage"] == "explore"
    assert second["to_stage"] == "rollback"


# ── sink payload enrichment ─────────────────────────────────────────────────

def test_policy_transition_sink_payload(tmp_path: Path) -> None:
    """Metrics sink sees attribution fields on transitions."""
    events: list[dict] = []

    class _Sink:
        def emit(self, event, payload):
            events.append({"event": event, "payload": dict(payload)})

    engine = PolicyEngine(
        state_root=lambda: tmp_path,
        lock=threading.Lock(),
        sink=lambda: _Sink(),
        session_id=lambda: "sess-42",
    )
    key = "gmail.read.fetch"
    engine.apply_evidence(key, success=False)
    engine.apply_evidence(key, success=False)  # → rollback

    transitions = [e for e in events if e["event"] == "policy.transition"]
    assert len(transitions) == 1
    p = transitions[0]["payload"]
    assert p["intent_signature"] == key
    assert p["candidate_key"] == key  # legacy alias preserved
    assert p["from_stage"] == "explore"
    assert p["to_stage"] == "rollback"
    assert p["reason"] == "two_consecutive_failures"
    assert p["demotion"] is True
    assert p["session_id"] == "sess-42"
    assert "ts_ms" in p


# ── control_plane command surface ────────────────────────────────────────────

def test_cmd_control_plane_intents_includes_transition_fields(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path))
    engine = _fresh_engine(tmp_path)
    key = "gmail.read.fetch"
    engine.apply_evidence(key, success=False)
    engine.apply_evidence(key, success=False)  # → rollback

    from scripts.admin.control_plane import cmd_control_plane_intents
    out = cmd_control_plane_intents()
    assert out["ok"] is True
    row = next(r for r in out["intents"] if r["intent_signature"] == key)
    assert row["stage"] == "rollback"
    assert row["last_transition_reason"] == "two_consecutive_failures"
    assert row["last_transition_ts_ms"] > 0
    assert row["last_demotion"]["to_stage"] == "rollback"


def test_cmd_control_plane_intent_history(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path))
    engine = _fresh_engine(tmp_path)
    key = "gmail.read.fetch"
    engine.apply_evidence(key, success=False)
    engine.apply_evidence(key, success=False)  # → rollback
    engine.apply_evidence(key, success=True)   # rollback → explore

    from scripts.admin.control_plane import cmd_control_plane_intent_history
    out = cmd_control_plane_intent_history(key)
    assert out["ok"] is True
    assert out["intent_signature"] == key
    assert out["stage"] == "explore"
    assert len(out["transition_history"]) == 2
    assert out["transition_history"][0]["to_stage"] == "rollback"
    assert out["transition_history"][1]["to_stage"] == "explore"
    assert out["transition_history"][1]["reason"] == "rollback_recovered"
    assert out["last_demotion"]["to_stage"] == "rollback"


def test_cmd_control_plane_intent_history_limit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path))
    engine = _fresh_engine(tmp_path)
    key = "gmail.read.fetch"
    engine.apply_evidence(key, success=False)
    engine.apply_evidence(key, success=False)
    engine.apply_evidence(key, success=True)

    from scripts.admin.control_plane import cmd_control_plane_intent_history
    out = cmd_control_plane_intent_history(key, limit=1)
    assert out["ok"] is True
    assert len(out["transition_history"]) == 1
    # Keeps the *latest* entry when limited.
    assert out["transition_history"][0]["to_stage"] == "explore"


def test_cmd_control_plane_intent_history_unknown(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path))
    from scripts.admin.control_plane import cmd_control_plane_intent_history
    out = cmd_control_plane_intent_history("gmail.read.missing")
    assert out["ok"] is False
    assert out["error"] == "unknown_intent"


def test_cmd_control_plane_intent_history_requires_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path))
    from scripts.admin.control_plane import cmd_control_plane_intent_history
    out = cmd_control_plane_intent_history("")
    assert out["ok"] is False


# ── cockpit route ───────────────────────────────────────────────────────────

def test_cockpit_intent_history_route(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(tmp_path / "connectors"))
    (tmp_path / "connectors").mkdir(exist_ok=True)

    engine = _fresh_engine(tmp_path)
    key = "gmail.read.fetch"
    engine.apply_evidence(key, success=False)
    engine.apply_evidence(key, success=False)

    import json as _json
    import urllib.request
    from scripts.repl_admin import cmd_serve

    base = cmd_serve(port=0, open_browser=False)["url"]
    url = f"{base}/api/control-plane/intent-history?intent={key}"
    with urllib.request.urlopen(url, timeout=5) as resp:
        body = _json.loads(resp.read())
    assert body["ok"] is True
    assert body["intent_signature"] == key
    assert body["stage"] == "rollback"
    assert len(body["transition_history"]) == 1
    assert body["last_demotion"]["reason"] == "two_consecutive_failures"
