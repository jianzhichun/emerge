#!/usr/bin/env python3
"""Unified emerge event stream watcher.

Tails events.jsonl (global), events-{profile}.jsonl (per-runner), or
events-local.jsonl (local) and prints formatted lines to stdout.

Launch via CC's Monitor tool:
    Monitor(command="python3 .../watch_emerge.py", persistent=true)
    Monitor(command="python3 .../watch_emerge.py --runner-profile mycader-1", persistent=true)

Each watcher writes a heartbeat JSON under ``state/events/watchers/<id>.json``
so the cockpit can surface an "alive / <N>s since last loop" SLO. The main
tail loop is exception-resilient: transient file-system or parse errors are
captured in the heartbeat and retried with exponential backoff instead of
killing the process.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import traceback
from typing import Any
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.pending_actions import (  # noqa: E402
    format_pattern_alert,
    format_runner_discovered,
    format_runner_online,
    format_runner_event,
    format_runner_subagent_message,
)
from scripts.policy_config import default_state_root, events_root  # noqa: E402
from scripts.watchers import write_heartbeat  # noqa: E402

_HEARTBEAT_INTERVAL_S = 5.0
_MAX_BACKOFF_S = 10.0

_stop = False


def _append_cockpit_ack(event_root: Path, event: dict[str, Any]) -> None:
    """Persist monitor-delivery ack for a cockpit_action event."""
    event_id = str(event.get("event_id", "")).strip()
    if not event_id:
        return
    ack = {
        "event_id": event_id,
        "event_ts_ms": int(event.get("ts_ms", 0) or 0),
        "ack_ts_ms": int(time.time() * 1000),
        "pid": os.getpid(),
    }
    ack_path = event_root / "cockpit-action-acks.jsonl"
    with ack_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(ack, ensure_ascii=False) + "\n")


def _on_signal(signum, frame) -> None:
    global _stop
    _stop = True


if hasattr(signal, "SIGTERM"):  # not available on Windows
    signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


def _format_event(event: dict) -> str | None:
    etype = event.get("type", "")
    if etype == "runner_discovered":
        return format_runner_discovered(event)
    if etype == "runner_online":
        return format_runner_online(event)
    if etype == "runner_event":
        return format_runner_event(event)
    if etype in ("runner_subagent_message", "pattern_suggestion"):
        return format_runner_subagent_message(event)
    if etype in ("pattern_alert", "local_pattern_alert"):
        return format_pattern_alert(event)
    if etype == "operator_message":
        text = event.get("text", "")
        profile = event.get("runner_profile", event.get("profile", "?"))
        lines = [f"[ACTION REQUIRED][Operator:{profile}] {text}".rstrip()]
        for att in event.get("attachments", []):
            lines.append(f"[附件: {att.get('path', '')} ({att.get('mime', '')})]")
        return "\n".join(lines)
    if etype == "cockpit_action":
        try:
            from scripts.pending_actions import format_pending_actions
            actions = event.get("actions", [])
            if actions:
                return format_pending_actions(actions)
        except (ImportError, Exception):
            pass
        return None
    return f"[Event] {json.dumps(event, ensure_ascii=False)}"


def _state_root(override: str | None = None) -> Path:
    if override:
        return Path(override)
    env = os.environ.get("EMERGE_STATE_ROOT")
    if env:
        return Path(env)
    return default_state_root()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Watch emerge event stream.")
    p.add_argument("--runner-profile", default="",
                   help="Runner profile to watch (watches events-{profile}.jsonl)")
    p.add_argument("--local", action="store_true",
                   help="Watch local events (events-local.jsonl)")
    p.add_argument("--state-root", default="",
                   help="Override state root directory")
    p.add_argument("--watcher-id", default="",
                   help="Explicit watcher id for heartbeat; derived when empty")
    p.add_argument("--no-heartbeat", action="store_true",
                   help="Disable heartbeat file (tests and ephemeral runs)")
    return p.parse_args()


def _derive_watcher_id(args: argparse.Namespace) -> str:
    if args.watcher_id.strip():
        return args.watcher_id.strip()
    if args.local:
        return "local"
    if args.runner_profile.strip():
        return f"runner-{args.runner_profile.strip()}"
    return "global"


class _HeartbeatWriter:
    """Tracks per-loop telemetry and persists the heartbeat record."""

    def __init__(
        self,
        *,
        watcher_id: str,
        target: Path,
        state_root: Path,
        enabled: bool,
    ) -> None:
        self._watcher_id = watcher_id
        self._target = target
        self._state_root = state_root
        self._enabled = enabled
        now = int(time.time() * 1000)
        self._record: dict[str, Any] = {
            "watcher_id": watcher_id,
            "pid": os.getpid(),
            "target": str(target),
            "state_root": str(state_root),
            "started_at_ms": now,
            "last_loop_ts_ms": now,
            "events_read": 0,
            "events_delivered": 0,
            "last_event_id": None,
            "last_error": None,
            "consecutive_errors": 0,
        }
        self._last_flush_ms = 0
        self._dirty = True

    def mark_loop(self) -> None:
        self._record["last_loop_ts_ms"] = int(time.time() * 1000)
        self._dirty = True

    def add_read(self, n: int = 1) -> None:
        self._record["events_read"] = int(self._record["events_read"]) + n
        self._dirty = True

    def add_delivered(self, event_id: str | None = None) -> None:
        self._record["events_delivered"] = int(self._record["events_delivered"]) + 1
        if event_id:
            self._record["last_event_id"] = event_id
        self._dirty = True

    def record_error(self, exc: BaseException) -> None:
        self._record["consecutive_errors"] = int(self._record["consecutive_errors"]) + 1
        self._record["last_error"] = {
            "message": f"{type(exc).__name__}: {exc}",
            "traceback_tail": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-2000:],
            "ts_ms": int(time.time() * 1000),
        }
        self._dirty = True

    def clear_error(self) -> None:
        if self._record["consecutive_errors"] or self._record["last_error"]:
            self._record["consecutive_errors"] = 0
            self._record["last_error"] = None
            self._dirty = True

    def flush(self, *, force: bool = False) -> None:
        if not self._enabled:
            return
        now_ms = int(time.time() * 1000)
        if not force and not self._dirty and (now_ms - self._last_flush_ms) < int(_HEARTBEAT_INTERVAL_S * 1000):
            return
        try:
            write_heartbeat(self._state_root, self._record)
            self._last_flush_ms = now_ms
            self._dirty = False
        except OSError:
            pass

    def stopped(self) -> None:
        if not self._enabled:
            return
        self._record["stopped_at_ms"] = int(time.time() * 1000)
        self._dirty = True
        self.flush(force=True)


def run_tail(
    path: Path,
    sleep_s: float = 0.5,
    *,
    heartbeat: _HeartbeatWriter | None = None,
) -> None:
    """Tail-follow *path*, print formatted events to stdout.

    The loop is exception-resilient: unexpected errors are recorded in the
    heartbeat and retried with exponential backoff so a single malformed line
    or transient FS error can never take the watcher down.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    offset = 0
    if path.exists():
        offset = path.stat().st_size

    backoff_s = sleep_s
    while not _stop:
        try:
            if heartbeat is not None:
                heartbeat.mark_loop()
                heartbeat.flush()
            if not path.exists():
                time.sleep(sleep_s)
                continue
            current_size = path.stat().st_size
            if current_size < offset:
                offset = 0
            if current_size > offset:
                with path.open("r", encoding="utf-8") as f:
                    f.seek(offset)
                    new_data = f.read()
                offset = current_size
                for line in new_data.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if heartbeat is not None:
                        heartbeat.add_read(1)
                    formatted = _format_event(event)
                    if formatted is not None:
                        print(formatted, flush=True)
                        if heartbeat is not None:
                            heartbeat.add_delivered(str(event.get("event_id") or "") or None)
                        if path.name == "events.jsonl" and event.get("type") == "cockpit_action":
                            try:
                                _append_cockpit_ack(path.parent, event)
                            except OSError:
                                pass
            if heartbeat is not None:
                heartbeat.clear_error()
            backoff_s = sleep_s
            time.sleep(sleep_s)
        except Exception as exc:  # noqa: BLE001 — watcher must self-heal, never die
            if heartbeat is not None:
                heartbeat.record_error(exc)
                heartbeat.flush(force=True)
            print(f"[watch_emerge] transient error: {exc!r}", file=sys.stderr, flush=True)
            time.sleep(backoff_s)
            backoff_s = min(_MAX_BACKOFF_S, backoff_s * 2)


if __name__ == "__main__":
    args = _parse_args()
    root = _state_root(args.state_root)
    if args.local:
        target = events_root(root) / "events-local.jsonl"
    elif args.runner_profile.strip():
        profile = args.runner_profile.strip()
        target = events_root(root) / f"events-{profile}.jsonl"
    else:
        target = events_root(root) / "events.jsonl"
    watcher_id = _derive_watcher_id(args)
    heartbeat = _HeartbeatWriter(
        watcher_id=watcher_id,
        target=target,
        state_root=root,
        enabled=not args.no_heartbeat,
    )
    heartbeat.flush(force=True)
    try:
        run_tail(target, heartbeat=heartbeat)
    finally:
        heartbeat.stopped()
