#!/usr/bin/env python3
"""Watch for operator-monitor pattern alerts and emit formatted lines to stdout.

Designed to be launched via CC's Monitor tool::

    Monitor(command="python3 .../watch_patterns.py",
            description="operator pattern alert watcher",
            persistent=true)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.pending_actions import format_pattern_alert  # noqa: E402
from scripts.watch_file import run_watcher  # noqa: E402


def _state_root() -> Path:
    env = os.environ.get("EMERGE_STATE_ROOT") or os.environ.get("CLAUDE_PLUGIN_DATA")
    if env:
        return Path(env)
    return Path.home() / ".emerge" / "repl"


if __name__ == "__main__":
    run_watcher(_state_root() / "pattern-alerts.json", format_pattern_alert)
