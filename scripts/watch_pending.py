#!/usr/bin/env python3
"""Watch for cockpit pending-actions.json and emit formatted lines to stdout.

Designed to be launched via CC's Monitor tool::

    Monitor(command="python3 .../watch_pending.py",
            description="cockpit action watcher",
            persistent=true)
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if __name__ == "__main__":
    # Shim: delegate to watch_emerge.py (global event stream mode)
    import argparse as _ap
    import os as _os, sys as _sys
    from pathlib import Path as _Path
    _root = _Path(__file__).resolve().parent.parent
    _emerge = str(_root / "scripts" / "watch_emerge.py")
    _p = _ap.ArgumentParser(add_help=False)
    _p.add_argument("--state-root", default="")
    _known, _ = _p.parse_known_args()
    _cmd = [_sys.executable, _emerge]
    if _known.state_root:
        _cmd += ["--state-root", _known.state_root]
    _os.execv(_sys.executable, _cmd)
