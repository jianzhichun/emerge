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


def _format_actions(actions: list) -> str:
    lines = ["[Cockpit] The operator submitted the following actions — execute in order:"]
    for i, a in enumerate(actions, 1):
        t = a.get("type", "unknown")
        if t == "tool-call":
            call = a.get("call", {}) if isinstance(a.get("call"), dict) else {}
            tool = call.get("tool", "?")
            call_args = call.get("arguments", {})
            meta = a.get("meta", {}) if isinstance(a.get("meta"), dict) else {}
            scope = str(meta.get("scope", "")).strip()
            scope_suffix = f" scope={scope}" if scope else ""
            lines.append(f"{i}. Execute tool-call {tool} args={call_args}{scope_suffix}")
        elif t == "pipeline-set":
            lines.append(f"{i}. pipeline-set {a.get('key')} fields={a.get('fields', {})}")
        elif t == "pipeline-delete":
            lines.append(f"{i}. pipeline-delete {a.get('key')}")
        elif t == "notes-edit":
            lines.append(f"{i}. Update {a.get('connector')} NOTES.md (full replace)")
        elif t == "notes-comment":
            lines.append(f"{i}. Append comment to {a.get('connector')} NOTES.md: {str(a.get('comment', ''))[:80]}")
        elif t == "crystallize-component":
            lines.append(f"{i}. Crystallize component {a.get('filename')} -> {a.get('connector')}/cockpit/")
        else:
            lines.append(f"{i}. {t}: {a}")
    return "\n".join(lines)


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
