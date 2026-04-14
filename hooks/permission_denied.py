"""PermissionDenied hook — retry icc_* tools denied by auto-mode classifier.

When CC's auto-mode classifier denies an emerge icc_* tool call, this hook
returns {"retry": true} to tell CC that the model should be allowed to retry
with explicit permission. Without this, icc_* denials are silent — the model
never sees an error and the flywheel misses the event.

All other tools (Bash, Write, etc.) are not retried — those are the user's
permission settings to respect.

Output contract: top-level {"retry": true}. Not hookSpecificOutput.
"""
from __future__ import annotations

import json
import re
import sys

_ICC_PATTERN = re.compile(r"mcp__plugin_.*emerge.*__icc_.*")


def main() -> None:
    payload_text = sys.stdin.read().strip()
    try:
        payload = json.loads(payload_text) if payload_text else {}
    except Exception:
        payload = {}

    tool_name = str(payload.get("tool_name") or "")
    if _ICC_PATTERN.fullmatch(tool_name):
        print(json.dumps({"retry": True}))
    else:
        print(json.dumps({}))


if __name__ == "__main__":
    main()
