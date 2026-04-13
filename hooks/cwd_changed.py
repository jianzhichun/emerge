from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> None:
    payload_text = sys.stdin.read().strip()
    try:
        payload = json.loads(payload_text) if payload_text else {}
    except Exception:
        payload = {}

    old_cwd = str(payload.get("old_cwd", "") or "")
    new_cwd = str(payload.get("new_cwd", "") or "")

    if not new_cwd or old_cwd == new_cwd:
        print(json.dumps({}))
        return

    # Notify Claude that the project context has changed.
    # emerge's session ID is derived from CWD at daemon start — mid-session CWD changes
    # mean the active session may not match the new project root.
    msg = (
        f"[emerge/CwdChanged] Working directory changed: {old_cwd} → {new_cwd}\n"
        "emerge session context was anchored to the original CWD. "
        "If you intend to work in this new directory, be aware that:\n"
        "- Flywheel spans and pipeline intents still reference the original session.\n"
        "- Use the new project's connector names explicitly in intent_signature.\n"
        f"New CWD: {new_cwd}"
    )
    # CwdChanged uses top-level systemMessage (not hookSpecificOutput)
    out = {"systemMessage": msg}
    print(json.dumps(out))


if __name__ == "__main__":
    main()
