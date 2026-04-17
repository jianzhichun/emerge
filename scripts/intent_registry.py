from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from scripts.policy_config import atomic_write_json, load_json_object, registry_root


def registry_path(state_root: Path) -> Path:
    return registry_root(state_root) / "intents.json"


def default_intent_entry() -> dict[str, Any]:
    return {
        "stage": "explore",
        "frozen": False,
        "persistent": False,
        "attempts": 0,
        "successes": 0,
        "human_fixes": 0,
        "consecutive_failures": 0,
        "recent_outcomes": [],
        "rollout_pct": 0,
        "verify_rate": None,
        "synthesis_ready": False,
        "is_read_only": False,
        "skeleton_generated": False,
        "description": "",
        "last_ts_ms": 0,
        "updated_at_ms": 0,
        # Bounded audit trail of stage changes. Each entry:
        # {ts_ms, from_stage, to_stage, reason, attempts, success_rate,
        #  verify_rate, consecutive_failures, window_success_rate,
        #  session_id, target_profile, execution_path}.
        "transition_history": [],
        # Snapshot of the most recent demotion (explore→rollback,
        # canary→explore, stable→explore). ``None`` while the intent has
        # never been demoted. Mirrors the last demoting transition_history
        # entry so cockpit can render "Why did this pipeline regress?"
        # without scanning history.
        "last_demotion": None,
        # Composition: when non-empty, this intent is a composite. Executing
        # it runs each child intent's flywheel bridge in order and returns
        # the aggregated result. Composite stage derives from the children
        # (min-rank); a single non-stable child pulls the composite down so
        # we never bridge-execute a composite whose pieces are broken.
        "composed_from": [],
    }


class IntentRegistry:
    """Single read/write interface for global intent lifecycle state."""

    @staticmethod
    def load(state_root: Path) -> dict[str, Any]:
        data = load_json_object(registry_path(state_root), root_key="intents")
        if "intents" not in data or not isinstance(data["intents"], dict):
            data["intents"] = {}
        return data

    @staticmethod
    def save(state_root: Path, data: dict[str, Any]) -> None:
        atomic_write_json(registry_path(state_root), data)

    @classmethod
    def get(cls, state_root: Path, intent_signature: str) -> dict[str, Any]:
        data = cls.load(state_root)
        return data["intents"].get(intent_signature, {})

    @classmethod
    def update(cls, state_root: Path, intent_signature: str, **fields: Any) -> dict[str, Any]:
        data = cls.load(state_root)
        intents = data["intents"]
        entry = dict(default_intent_entry())
        entry.update(intents.get(intent_signature, {}))
        entry.update(fields)
        entry["updated_at_ms"] = int(time.time() * 1000)
        intents[intent_signature] = entry
        cls.save(state_root, data)
        return entry

    @classmethod
    def iter_by_stage(cls, state_root: Path, stage: str) -> dict[str, dict[str, Any]]:
        data = cls.load(state_root)
        return {
            k: v for k, v in data["intents"].items()
            if isinstance(v, dict) and str(v.get("stage", "explore")) == stage
        }

    @classmethod
    def iter_persistent(cls, state_root: Path) -> dict[str, dict[str, Any]]:
        data = cls.load(state_root)
        return {
            k: v for k, v in data["intents"].items()
            if isinstance(v, dict) and bool(v.get("persistent", False))
        }
