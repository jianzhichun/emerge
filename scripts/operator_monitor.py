from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable

from scripts.observer_plugin import AdapterRegistry
from scripts.pattern_detector import PatternDetector, PatternSummary


class _RunnerClientProtocol:
    """Duck-typed protocol for runner clients used by OperatorMonitor."""
    def get_events(self, machine_id: str, since_ms: int = 0) -> list[dict]: ...


class OperatorMonitor(threading.Thread):
    """Background thread that polls remote runners for operator events,
    runs PatternDetector, and calls push_fn when a pattern is found."""

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

    def _poll_machine(self, machine_id: str, client: Any) -> None:
        since_ms = self._last_poll_ms.get(machine_id, 0)
        events = client.get_events(machine_id=machine_id, since_ms=since_ms)
        if not events:
            return

        latest_ts = max(e.get("ts_ms", 0) for e in events)
        self._last_poll_ms[machine_id] = latest_ts

        summaries = self._detector.ingest(events)
        for summary in summaries:
            app = summary.context_hint.get("app", machine_id)
            plugin = self._adapter_registry.get_plugin(app)
            try:
                context = plugin.get_context(summary.context_hint)
            except Exception:
                context = summary.context_hint.copy()
            self._push_fn(summary.policy_stage, context, summary)
