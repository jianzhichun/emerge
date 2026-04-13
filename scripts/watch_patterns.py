#!/usr/bin/env python3
"""Watch for operator-monitor pattern alerts and emit formatted lines to stdout.

Designed to be launched via CC's Monitor tool::

    Monitor(command="python3 .../watch_patterns.py --runner-profile mycader-1",
            description="operator pattern alert watcher — mycader-1",
            persistent=true)

Without --runner-profile, falls back to watching pattern-alerts.json (legacy).
"""
from __future__ import annotations

import argparse
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


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Watch emerge pattern alerts for one runner.")
    p.add_argument(
        "--runner-profile",
        default="",
        help="Profile name to scope alert file (e.g. mycader-1). "
             "Omit to watch the shared pattern-alerts.json fallback.",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    profile = args.runner_profile.strip()
    filename = f"pattern-alerts-{profile}.json" if profile else "pattern-alerts.json"
    run_watcher(_state_root() / filename, format_pattern_alert)
