#!/usr/bin/env python3
"""Watch for cockpit pending-actions.json and emit formatted lines to stdout.

Designed to be launched via CC's Monitor tool::

    Monitor(command="python3 .../watch_pending.py",
            description="cockpit action watcher",
            persistent=true)

Each time pending-actions.json appears (or is overwritten), this script:
1. Reads and parses the file
2. Prints the formatted action list to stdout (one block per submission)
3. Renames to pending-actions.processed.json

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

# Add parent directory to path so relative imports work when script is executed directly
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.pending_actions import format_pending_actions as _format_actions

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
    pending = root / "pending-actions.json"
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
                actions = data.get("actions", [])
                if actions:
                    print(_format_actions(actions), flush=True)
                try:
                    pending.rename(root / "pending-actions.processed.json")
                except OSError:
                    pass
                last_ts = ts
        time.sleep(0.5)


if __name__ == "__main__":
    main()
