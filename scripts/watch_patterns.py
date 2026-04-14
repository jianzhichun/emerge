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
    p.add_argument("--state-root", default="", help="Override state root (for testing)")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    profile = args.runner_profile.strip()
    # Shim: delegate to watch_emerge.py for unified event stream
    import os as _os, sys as _sys
    _emerge = str(ROOT / "scripts" / "watch_emerge.py")
    cmd = [_sys.executable, _emerge]
    if profile:
        cmd += ["--runner-profile", profile]
    if getattr(args, "state_root", ""):
        cmd += ["--state-root", args.state_root]
    _os.execv(_sys.executable, cmd)
