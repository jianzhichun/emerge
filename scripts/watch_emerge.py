#!/usr/bin/env python3
"""Unified emerge event stream watcher.

Tails events.jsonl (global), events-{profile}.jsonl (per-runner), or
events-local.jsonl (local) and prints formatted lines to stdout.

Launch via CC's Monitor tool:
    Monitor(command="python3 .../watch_emerge.py", persistent=true)
    Monitor(command="python3 .../watch_emerge.py --runner-profile mycader-1", persistent=true)
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
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
)

_stop = False


def _append_cockpit_ack(state_root: Path, event: dict[str, Any]) -> None:
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
    ack_path = state_root / "cockpit-action-acks.jsonl"
    with ack_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(ack, ensure_ascii=False) + "\n")


def _on_signal(signum, frame) -> None:
    global _stop
    _stop = True


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
    if etype in ("pattern_alert", "local_pattern_alert"):
        return format_pattern_alert(event)
    if etype == "operator_message":
        text = event.get("text", "")
        profile = event.get("runner_profile", event.get("profile", "?"))
        return f"[ACTION REQUIRED][Operator:{profile}] {text}"
    if etype == "cockpit_action":
        # Pending actions embedded in event
        try:
            from scripts.pending_actions import format_pending_actions
            actions = event.get("actions", [])
            if actions:
                return format_pending_actions(actions)
        except (ImportError, Exception):
            pass
        return None
    # Unknown type: print raw JSON
    return f"[Event] {json.dumps(event, ensure_ascii=False)}"


def _state_root(override: str | None = None) -> Path:
    if override:
        return Path(override)
    env = os.environ.get("EMERGE_STATE_ROOT") or os.environ.get("CLAUDE_PLUGIN_DATA")
    if env:
        return Path(env)
    return Path.home() / ".emerge" / "repl"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Watch emerge event stream.")
    p.add_argument("--runner-profile", default="",
                   help="Runner profile to watch (watches events-{profile}.jsonl)")
    p.add_argument("--local", action="store_true",
                   help="Watch local events (events-local.jsonl)")
    p.add_argument("--state-root", default="",
                   help="Override state root directory")
    return p.parse_args()


def run_tail(path: Path, sleep_s: float = 0.5) -> None:
    """Tail-follow path, print formatted events to stdout."""
    path.parent.mkdir(parents=True, exist_ok=True)
    offset = 0
    # Start from end of file if it already exists (don't replay old events)
    if path.exists():
        offset = path.stat().st_size

    while not _stop:
        try:
            if not path.exists():
                time.sleep(sleep_s)
                continue
            current_size = path.stat().st_size
            if current_size < offset:
                offset = 0  # file truncated/rotated
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
                    formatted = _format_event(event)
                    if formatted is not None:
                        print(formatted, flush=True)
                        if path.name == "events.jsonl" and event.get("type") == "cockpit_action":
                            try:
                                _append_cockpit_ack(path.parent, event)
                            except OSError:
                                pass
        except OSError:
            pass
        time.sleep(sleep_s)


if __name__ == "__main__":
    args = _parse_args()
    root = _state_root(args.state_root)
    if args.local:
        target = root / "events-local.jsonl"
    elif args.runner_profile.strip():
        profile = args.runner_profile.strip()
        target = root / f"events-{profile}.jsonl"
    else:
        target = root / "events.jsonl"
    run_tail(target)
