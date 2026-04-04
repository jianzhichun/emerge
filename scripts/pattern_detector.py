from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


@dataclass
class PatternSummary:
    machine_ids: list[str]
    intent_signature: str
    occurrences: int
    window_minutes: float
    detector_signals: list[str]
    context_hint: dict
    policy_stage: str = "explore"


class PatternDetector:
    """Applies pluggable detector strategies to batches of operator events.
    Returns a list of PatternSummary objects when thresholds are crossed."""

    FREQ_THRESHOLD = 3
    FREQ_WINDOW_MS = 20 * 60 * 1000  # 20 minutes
    ERROR_RATE_THRESHOLD = 0.4  # undos / total ops
    CROSS_MACHINE_MIN_MACHINES = 2
    CROSS_MACHINE_MIN_PER_MACHINE = 2

    def ingest(self, events: list[dict[str, Any]]) -> list[PatternSummary]:
        operator_events = [e for e in events if e.get("session_role") != "monitor_sub"]
        if not operator_events:
            return []

        summaries: list[PatternSummary] = []
        summaries.extend(self._frequency_check(operator_events))
        summaries.extend(self._error_rate_check(operator_events))
        summaries.extend(self._cross_machine_check(operator_events))
        return summaries

    def _frequency_check(self, events: list[dict]) -> list[PatternSummary]:
        now_ms = int(time.time() * 1000)
        window_events = [e for e in events if now_ms - e.get("ts_ms", 0) <= self.FREQ_WINDOW_MS]
        if not window_events:
            return []

        groups: dict[tuple, list[dict]] = {}
        for e in window_events:
            key = (
                e.get("app", ""),
                e.get("event_type", ""),
                e.get("payload", {}).get("layer", ""),
            )
            groups.setdefault(key, []).append(e)

        summaries = []
        for (app, event_type, layer), grp in groups.items():
            if len(grp) < self.FREQ_THRESHOLD:
                continue
            ts_values = [e["ts_ms"] for e in grp if "ts_ms" in e]
            window_min = (max(ts_values) - min(ts_values)) / 60_000 if len(ts_values) >= 2 else 0.0
            machines = list({e.get("machine_id", "unknown") for e in grp})
            samples = [
                e.get("payload", {}).get("content", "")
                for e in grp
                if e.get("payload", {}).get("content")
            ][:5]
            sig = f"{app}.{layer.replace('/', '_') if layer else event_type}"
            summaries.append(PatternSummary(
                machine_ids=machines,
                intent_signature=sig,
                occurrences=len(grp),
                window_minutes=window_min,
                detector_signals=["frequency"],
                context_hint={
                    "app": app,
                    "event_type": event_type,
                    "layer": layer,
                    "samples": samples,
                },
            ))
        return summaries

    def _error_rate_check(self, events: list[dict]) -> list[PatternSummary]:
        by_session: dict[str, list[dict]] = {}
        for e in events:
            sid = e.get("session_id", "unknown")
            by_session.setdefault(sid, []).append(e)

        summaries = []
        for sid, grp in by_session.items():
            total_ops = len([e for e in grp if e.get("event_type") != "undo"])
            undos = len([e for e in grp if e.get("event_type") == "undo"])
            if total_ops == 0:
                continue
            ratio = undos / total_ops
            if ratio < self.ERROR_RATE_THRESHOLD:
                continue
            machines = list({e.get("machine_id", "unknown") for e in grp})
            app = grp[0].get("app", "unknown") if grp else "unknown"
            summaries.append(PatternSummary(
                machine_ids=machines,
                intent_signature=f"{app}.high_error_rate",
                occurrences=len(grp),
                window_minutes=0.0,
                detector_signals=["error_rate"],
                context_hint={"app": app, "undo_ratio": ratio, "session_id": sid},
            ))
        return summaries

    def _cross_machine_check(self, events: list[dict]) -> list[PatternSummary]:
        by_app_event: dict[tuple, dict[str, list[dict]]] = {}
        for e in events:
            key = (e.get("app", ""), e.get("event_type", ""))
            machine = e.get("machine_id", "unknown")
            by_app_event.setdefault(key, {}).setdefault(machine, []).append(e)

        summaries = []
        for (app, event_type), by_machine in by_app_event.items():
            qualifying = {
                m: evts
                for m, evts in by_machine.items()
                if len(evts) >= self.CROSS_MACHINE_MIN_PER_MACHINE
            }
            if len(qualifying) < self.CROSS_MACHINE_MIN_MACHINES:
                continue
            all_events = [e for evts in qualifying.values() for e in evts]
            machines = list(qualifying.keys())
            summaries.append(PatternSummary(
                machine_ids=machines,
                intent_signature=f"{app}.{event_type}.cross_machine",
                occurrences=len(all_events),
                window_minutes=0.0,
                detector_signals=["cross_machine"],
                context_hint={"app": app, "event_type": event_type, "machines": machines},
            ))
        return summaries
