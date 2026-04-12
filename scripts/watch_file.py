from __future__ import annotations

import json
import signal
import time
from pathlib import Path
from typing import Callable

_stop = False


def _on_signal(signum, frame) -> None:
    global _stop
    _stop = True


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


def process_once(
    path: Path,
    formatter: Callable[[dict], str | None],
    rename_suffix: str,
    last_ts: int,
) -> int:
    """Process *path* if it exists and has a newer ``submitted_at`` timestamp.

    Returns the new ``last_ts`` (unchanged if nothing was processed).
    """
    if not path.exists():
        return last_ts
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return last_ts
    ts = int(data.get("submitted_at", 0))
    if ts <= last_ts:
        return last_ts
    formatted = formatter(data)
    if formatted is not None:
        print(formatted, flush=True)
    renamed = path.parent / (path.stem + rename_suffix)
    try:
        path.rename(renamed)
    except OSError:
        pass
    return ts


def run_watcher(
    path: Path,
    formatter: Callable[[dict], str | None],
    rename_suffix: str = ".processed.json",
    sleep_s: float = 0.5,
) -> None:
    """Poll *path* in a loop, format and print new content, rename on process.

    Exits cleanly on SIGTERM or SIGINT.
    Designed for use as a persistent CC Monitor tool script.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    last_ts = 0
    while not _stop:
        last_ts = process_once(path, formatter, rename_suffix, last_ts)
        time.sleep(sleep_s)
