from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from scripts.policy_config import default_hook_state_root
from scripts.state_tracker import StateTracker, load_tracker, with_locked_tracker


class SpanService:
    """Centralized hook-state operations for active span lifecycle."""

    def __init__(self, hook_state_root: Path | None = None) -> None:
        self._hook_state_root = hook_state_root or Path(default_hook_state_root())
        self._state_path = self._hook_state_root / "state.json"
        self._actions_path = self._hook_state_root / "active-span-actions.jsonl"

    def get_active(self) -> tuple[str, str]:
        try:
            tracker = load_tracker(self._state_path)
            return (
                str(tracker.state.get("active_span_id") or ""),
                str(tracker.state.get("active_span_intent") or ""),
            )
        except Exception:
            return "", ""

    def clear_active(self) -> bool:
        try:
            def _mutate(tracker):
                had = bool(tracker.state.get("active_span_id"))
                tracker.state.pop("active_span_id", None)
                tracker.state.pop("active_span_intent", None)
                return had

            return bool(with_locked_tracker(self._state_path, _mutate))
        except Exception:
            return False

    def preserve_for_compact(self) -> tuple[str, str]:
        """Reset tracker while preserving active span identity."""
        def _mutate(tracker):
            active_id = str(tracker.state.get("active_span_id") or "")
            active_intent = str(tracker.state.get("active_span_intent") or "")
            fresh = StateTracker()
            if active_id:
                fresh.state["active_span_id"] = active_id
            if active_intent:
                fresh.state["active_span_intent"] = active_intent
            tracker.state.clear()
            tracker.state.update(fresh.state)
            return active_id, active_intent

        return with_locked_tracker(self._state_path, _mutate)

    def append_task_created_action(self, task_id: str, task_subject: str) -> bool:
        active_span_id, active_span_intent = self.get_active()
        if not active_span_id:
            return False
        self._hook_state_root.mkdir(parents=True, exist_ok=True)
        entry: dict[str, Any] = {
            "tool_name": "task_created",
            "args_hash": f"task:{task_id or task_subject}",
            "has_side_effects": True,
            "ts_ms": int(time.time() * 1000),
            "span_id": active_span_id,
            "intent_signature": active_span_intent,
            "task_id": task_id,
            "task_subject": task_subject,
        }
        with self._actions_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return True
