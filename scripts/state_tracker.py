from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


LEVEL_CORE_CRITICAL = "core_critical"
LEVEL_CORE_SECONDARY = "core_secondary"
LEVEL_PERIPHERAL = "peripheral"


class StateTracker:
    def __init__(self, state: dict[str, Any] | None = None) -> None:
        self.state = state or {
            "goal": "",
            "open_risks": [],
            "deltas": [],
            "verification_state": "verified",
            "consistency_window_ms": 0,
        }

    def set_goal(self, goal: str) -> None:
        self.state["goal"] = goal

    def set_consistency_window(self, window_ms: int) -> None:
        self.state["consistency_window_ms"] = max(0, int(window_ms))

    def add_delta(
        self,
        message: str,
        level: str = LEVEL_CORE_CRITICAL,
        verification_state: str = "verified",
        provisional: bool = False,
    ) -> str:
        delta_id = f"d-{int(time.time() * 1000)}-{len(self.state['deltas'])}"
        self.state["deltas"].append(
            {
                "id": delta_id,
                "message": message,
                "level": level,
                "verification_state": verification_state,
                "provisional": provisional,
            }
        )
        if verification_state == "degraded":
            self.state["verification_state"] = "degraded"
        return delta_id

    def add_risk(self, risk: str) -> None:
        self.state["open_risks"].append(risk)

    def mark_degraded(self, reason: str) -> None:
        self.state["verification_state"] = "degraded"
        self.add_risk(reason)

    def reconcile_delta(self, delta_id: str, outcome: str) -> None:
        for delta in self.state["deltas"]:
            if delta["id"] == delta_id:
                delta["provisional"] = False
                if outcome in {"confirm", "correct", "retract"}:
                    delta["reconcile_outcome"] = outcome
                    if outcome == "retract":
                        delta["verification_state"] = "degraded"
                        self.state["verification_state"] = "degraded"
                break

    def can_auto_chain_high_risk_write(self) -> bool:
        return self.state.get("verification_state") != "degraded"

    def format_context(self, budget_chars: int | None = None) -> dict[str, str]:
        deltas = list(self.state["deltas"])
        critical = [d for d in deltas if d["level"] == LEVEL_CORE_CRITICAL]
        secondary = [d for d in deltas if d["level"] == LEVEL_CORE_SECONDARY]
        peripheral = [d for d in deltas if d["level"] == LEVEL_PERIPHERAL]

        delta_lines = [f"- {d['message']}" for d in critical]
        if secondary:
            delta_lines.extend([f"- {d['message']}" for d in secondary])
        if peripheral:
            delta_lines.extend([f"- {d['message']}" for d in peripheral])

        delta_text = "\n".join(delta_lines) if delta_lines else "- No changes."

        if budget_chars and len(delta_text) > budget_chars:
            # Trim order: drop peripheral -> aggregate secondary -> keep critical verbatim.
            delta_lines = [f"- {d['message']}" for d in critical]
            if secondary:
                delta_lines.append(f"- Secondary changes: {len(secondary)} (aggregated)")
            delta_text = "\n".join(delta_lines)
            if len(delta_text) > budget_chars:
                delta_text = "\n".join([f"- {d['message']}" for d in critical]) or "- No changes."

        risks = self.state["open_risks"]
        risks_text = "\n".join(f"- {r}" for r in risks) if risks else "- None."

        return {
            "Goal": self.state.get("goal") or "Not set.",
            "Delta": delta_text,
            "Open Risks": risks_text,
        }

    def to_dict(self) -> dict[str, Any]:
        return self.state


def load_tracker(path: Path) -> StateTracker:
    if path.exists():
        return StateTracker(json.loads(path.read_text(encoding="utf-8")))
    return StateTracker()


def save_tracker(path: Path, tracker: StateTracker) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tracker.to_dict(), ensure_ascii=True, indent=2), encoding="utf-8")
