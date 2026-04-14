"""PermissionRequest hook — pre-approve icc_* tools before the dialog appears.

When CC shows a permission dialog for an emerge flywheel tool, this hook
approves it automatically. This is complementary to PermissionDenied (which
handles tools the auto-classifier already rejected) — PermissionRequest fires
before the user sees the dialog, so the flywheel operates with zero friction.

Scope: only icc_* tools. All other tools go through normal permission flow.

Output contract: hookSpecificOutput with PermissionRequest decision.
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
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {"behavior": "allow"},
            }
        }))
    else:
        print(json.dumps({}))


if __name__ == "__main__":
    main()
