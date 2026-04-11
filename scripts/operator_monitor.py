from __future__ import annotations

import time
import threading
from collections import deque
from pathlib import Path
from typing import Any, Callable

from scripts.observer_plugin import AdapterRegistry
from scripts.pattern_detector import PatternDetector, PatternSummary


class _RunnerClientProtocol:
    """Duck-typed protocol for runner clients used by OperatorMonitor."""
    def get_events(self, machine_id: str, since_ms: int = 0) -> list[dict]: ...


class OperatorMonitor(threading.Thread):
    """Background thread that polls remote runners for operator events,
    runs PatternDetector against a per-machine sliding window buffer,
    and calls push_fn when a pattern is found."""

    def __init__(
        self,
        machines: dict[str, Any],
        push_fn: Callable[[str, dict, PatternSummary], None],
        poll_interval_s: float = 5.0,
        event_root: Path | None = None,
        adapter_root: Path | None = None,
    ) -> None:
        super().__init__(daemon=True, name="OperatorMonitor")
        self._machines = machines
        self._push_fn = push_fn
        self._poll_interval_s = poll_interval_s
        self._event_root = event_root or (Path.home() / ".emerge" / "operator-events")
        self._adapter_registry = AdapterRegistry(adapter_root=adapter_root)
        self._detector = PatternDetector()
        self._last_poll_ms: dict[str, int] = {}
        # Sliding window buffer: accumulates events within FREQ_WINDOW_MS per machine.
        self._event_buffers: dict[str, deque] = {}
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.wait(timeout=self._poll_interval_s):
            for machine_id, client in self._machines.items():
                try:
                    self._poll_machine(machine_id, client)
                except Exception:
                    pass
            # Local operator events delivered by EventRouter (event-driven, not polled)

    def process_local_file(self, events_path: Path) -> None:
        """Process a single local events.jsonl file. Called by EventRouter on file change."""
        import json as _json
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
        now_ms = int(time.time() * 1000)
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
            self._push_fn(summary.policy_stage, context, summary)

    def _poll_machine(self, machine_id: str, client: Any) -> None:
        since_ms = self._last_poll_ms.get(machine_id, 0)
        events = client.get_events(machine_id=machine_id, since_ms=since_ms)

        if events:
            latest_ts = max(e.get("ts_ms", 0) for e in events)
            self._last_poll_ms[machine_id] = latest_ts

            buf = self._event_buffers.setdefault(machine_id, deque())
            buf.extend(events)

        buf = self._event_buffers.get(machine_id)
        if not buf:
            return

        # Trim events older than the detector's frequency window
        now_ms = int(time.time() * 1000)
        window_ms = PatternDetector.FREQ_WINDOW_MS
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
            self._push_fn(summary.policy_stage, context, summary)
