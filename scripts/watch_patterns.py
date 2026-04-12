#!/usr/bin/env python3
"""Watch for operator-monitor pattern alerts and emit formatted lines to stdout.

Designed to be launched via CC's Monitor tool::

    Monitor(command="python3 .../watch_patterns.py",
            description="operator pattern alert watcher",
            persistent=true)

Each time pattern-alerts.json appears (or is overwritten), this script:
1. Reads and parses the file
2. Prints the formatted alert to stdout (one block per alert)
3. Renames to pattern-alerts.processed.json

stdout lines become Monitor notifications that CC sees in the conversation.
The script exits cleanly on SIGTERM/SIGINT (Monitor sends SIGTERM on stop).
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

_stop = False


def _on_signal(signum, frame):
    global _stop
    _stop = True


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


def _state_root() -> Path:
    env = os.environ.get("EMERGE_STATE_ROOT") or os.environ.get("CLAUDE_PLUGIN_DATA")
    if env:
        return Path(env)
    return Path.home() / ".emerge" / "repl"


def main() -> None:
    root = _state_root()
    root.mkdir(parents=True, exist_ok=True)
    pending = root / "pattern-alerts.json"
    last_ts = 0

    while not _stop:
        if pending.exists():
            try:
                data = json.loads(pending.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                time.sleep(0.5)
                continue
            ts = int(data.get("submitted_at", 0))
            if ts > last_ts:
                message = data.get("message", "")
                meta = data.get("meta", {})
                stage = data.get("stage", "explore")
                sig = data.get("intent_signature", "?")
                lines = [
                    f"[OperatorMonitor] Pattern alert (stage={stage}):",
                    message,
                ]
                if meta:
                    lines.append(
                        f"  occurrences={meta.get('occurrences', '?')} "
                        f"window={meta.get('window_minutes', '?')}min "
                        f"machines={meta.get('machine_ids', [])}"
                    )
                print("\n".join(lines), flush=True)
                try:
                    pending.rename(root / "pattern-alerts.processed.json")
                except OSError:
                    pass
                last_ts = ts
        time.sleep(0.5)


if __name__ == "__main__":
    main()
