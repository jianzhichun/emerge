from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Any

from scripts.intent_registry import IntentRegistry
from scripts.pattern_detector import PatternDetector
from scripts.policy_engine import derive_stage
from scripts.policy_config import default_state_root, events_root


class OperatorMonitor:
    """Process local operator events and emit pattern alerts."""

    def __init__(
        self,
        machines: dict[str, Any],
        poll_interval_s: float = 5.0,
        event_root: Path | None = None,
        state_root: Path | None = None,
    ) -> None:
        self._machines = machines
        self._poll_interval_s = poll_interval_s
        self._event_root = event_root or (Path.home() / ".emerge" / "operator-events")
        self._state_root = state_root or default_state_root()
        self._detector = PatternDetector()
        self._last_poll_ms: dict[str, int] = {}
        self._event_buffers: dict[str, deque] = {}
        self._started = False

    def stop(self) -> None:
        self._started = False

    def start(self) -> None:
        self._started = True

    def is_alive(self) -> bool:
        return self._started

    def process_local_file(self, events_path: Path) -> None:
        """Process a single local events.jsonl file. Called by EventRouter on file change."""
        import json as _json
        import time as _time
        if not events_path.exists() or events_path.name != "events.jsonl":
            return
        machine_id = events_path.parent.name
        key = f"local:{machine_id}"
        since_ms = self._last_poll_ms.get(key, 0)
        events: list[dict] = []
        with events_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                if e.get("ts_ms", 0) > since_ms:
                    events.append(e)
        if events:
            latest_ts = max(e.get("ts_ms", 0) for e in events)
            self._last_poll_ms[key] = latest_ts
            buf = self._event_buffers.setdefault(key, deque())
            buf.extend(events)
        buf = self._event_buffers.get(key)
        if not buf:
            return
        now_ms = int(_time.time() * 1000)
        window_ms = self._detector.FREQ_WINDOW_MS
        while buf and now_ms - buf[0].get("ts_ms", 0) > window_ms:
            buf.popleft()
        if not buf:
            return
        summaries = self._detector.ingest(list(buf))
        for summary in summaries:
            # Primary path: write directly to events-local.jsonl
            ts_ms = int(_time.time() * 1000)
            events_local = events_root(self._state_root) / "events-local.jsonl"
            events_local.parent.mkdir(parents=True, exist_ok=True)
            # Resolve stage from IntentRegistry — single source of truth.
            # Unknown signatures default to "explore" via derive_stage on an
            # empty entry.
            try:
                entry = IntentRegistry.get(self._state_root, summary.intent_signature) or {}
                stage = str(entry.get("stage") or derive_stage(entry))
            except Exception:
                stage = summary.policy_stage
            alert = {
                "type": "local_pattern_observed",
                "ts_ms": ts_ms,
                "stage": stage,
                "intent_signature": summary.intent_signature,
                "meta": {
                    "occurrences": summary.occurrences,
                    "window_minutes": round(summary.window_minutes, 1),
                    "machine_ids": summary.machine_ids,
                    "detector_signals": summary.detector_signals,
                    "app": summary.context_hint.get("app", ""),
                },
            }
            with events_local.open("a", encoding="utf-8") as f:
                f.write(_json.dumps(alert, ensure_ascii=False) + "\n")
