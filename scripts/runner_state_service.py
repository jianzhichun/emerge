from __future__ import annotations

import json
import threading
from pathlib import Path


class RunnerStateService:
    """Owns connected runner state and monitor-state persistence."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.runners: dict[str, dict] = {}

    def on_online(self, runner_profile: str, machine_id: str, now_ms: int) -> None:
        with self.lock:
            prev = self.runners.get(runner_profile, {})
            self.runners[runner_profile] = {
                "connected_at_ms": now_ms,
                "last_event_ts_ms": int(prev.get("last_event_ts_ms", 0)),
                "machine_id": machine_id,
                "last_alert": prev.get("last_alert"),
                "last_online_ts_ms": now_ms,
                "last_sse_connected_ts_ms": int(prev.get("last_sse_connected_ts_ms", 0)),
                "last_sse_disconnected_ts_ms": int(prev.get("last_sse_disconnected_ts_ms", 0)),
                "sse_connected": bool(prev.get("sse_connected", True)),
            }

    def on_sse_connected(self, runner_profile: str, machine_id: str, now_ms: int) -> None:
        with self.lock:
            if runner_profile not in self.runners:
                self.runners[runner_profile] = {
                    "connected_at_ms": now_ms,
                    "last_event_ts_ms": 0,
                    "machine_id": machine_id or runner_profile,
                    "last_alert": None,
                    "last_online_ts_ms": now_ms,
                    "last_sse_connected_ts_ms": now_ms,
                    "last_sse_disconnected_ts_ms": 0,
                    "sse_connected": True,
                }
                return
            info = self.runners[runner_profile]
            if machine_id:
                info["machine_id"] = machine_id
            info["sse_connected"] = True
            info["last_sse_connected_ts_ms"] = now_ms

    def on_event(self, runner_profile: str, ts_ms: int) -> None:
        with self.lock:
            if runner_profile in self.runners:
                self.runners[runner_profile]["last_event_ts_ms"] = ts_ms

    def set_alert(self, runner_profile: str, alert: dict) -> None:
        with self.lock:
            if runner_profile in self.runners:
                self.runners[runner_profile]["last_alert"] = alert

    def mark_sse_disconnected(self, runner_profile: str, now_ms: int, grace_ms: int) -> tuple[bool, int]:
        """Returns (removed_runner, last_seen_age_ms)."""
        with self.lock:
            info = self.runners.get(runner_profile)
            if info is None:
                return False, 0
            info["sse_connected"] = False
            info["last_sse_disconnected_ts_ms"] = now_ms
            last_seen_ms = max(
                int(info.get("last_event_ts_ms", 0)),
                int(info.get("last_online_ts_ms", 0)),
                int(info.get("last_sse_connected_ts_ms", 0)),
            )
            age_ms = now_ms - last_seen_ms
            if age_ms > grace_ms:
                self.runners.pop(runner_profile, None)
                return True, age_ms
            return False, age_ms

    def snapshot(self) -> list[dict]:
        with self.lock:
            return [
                {
                    "runner_profile": profile,
                    "connected": bool(info.get("sse_connected", True)),
                    "connected_at_ms": info.get("connected_at_ms", 0),
                    "last_event_ts_ms": info.get("last_event_ts_ms", 0),
                    "machine_id": info.get("machine_id", ""),
                    "last_alert": info.get("last_alert"),
                    "last_online_ts_ms": info.get("last_online_ts_ms", 0),
                    "last_sse_connected_ts_ms": info.get("last_sse_connected_ts_ms", 0),
                    "last_sse_disconnected_ts_ms": info.get("last_sse_disconnected_ts_ms", 0),
                }
                for profile, info in self.runners.items()
            ]

    def counts(self) -> int:
        with self.lock:
            return len(self.runners)

    def write_monitor_state(self, path: Path) -> None:
        import tempfile as _tf
        import time

        data = {
            "runners": self.snapshot(),
            "team_active": bool(self.counts()),
            "updated_ts_ms": int(time.time() * 1000),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with _tf.NamedTemporaryFile("w", delete=False, dir=str(path.parent), encoding="utf-8") as tmp:
            json.dump(data, tmp, ensure_ascii=False)
            tmp.flush()
            tmp_name = tmp.name
        Path(tmp_name).replace(path)
