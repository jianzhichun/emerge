from __future__ import annotations

import threading
from collections import deque
from pathlib import Path
from typing import Any

from scripts.observer_plugin import AdapterRegistry
from scripts.pattern_detector import PatternDetector, PatternSummary


class OperatorMonitor(threading.Thread):
    """Background thread that watches local operator event files,
    runs PatternDetector against a per-machine sliding window buffer,
    and writes pattern alerts directly to events-local.jsonl."""

    def __init__(
        self,
        machines: dict[str, Any],
        poll_interval_s: float = 5.0,
        event_root: Path | None = None,
        adapter_root: Path | None = None,
        state_root: Path | None = None,
    ) -> None:
        super().__init__(daemon=True, name="OperatorMonitor")
        self._machines = machines
        self._poll_interval_s = poll_interval_s
        self._event_root = event_root or (Path.home() / ".emerge" / "operator-events")
        self._state_root = state_root or (Path.home() / ".emerge" / "repl")
        self._adapter_registry = AdapterRegistry(adapter_root=adapter_root)
        self._detector = PatternDetector()
        self._last_poll_ms: dict[str, int] = {}
        self._event_buffers: dict[str, deque] = {}
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        """Block until stop() is called. Operator events arrive via process_local_file()."""
        self._stop_event.wait()

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
            app = summary.context_hint.get("app", machine_id)
            plugin = self._adapter_registry.get_plugin(app)
            try:
                context = plugin.get_context(summary.context_hint)
            except Exception:
                context = summary.context_hint.copy()
            # Primary path: write directly to events-local.jsonl
            ts_ms = int(_time.time() * 1000)
            events_local = self._state_root / "events-local.jsonl"
            events_local.parent.mkdir(parents=True, exist_ok=True)
            alert = {
                "type": "local_pattern_alert",
                "ts_ms": ts_ms,
                "stage": summary.policy_stage,
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
