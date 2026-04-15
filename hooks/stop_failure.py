"""StopFailure hook — clear active span when CC exits due to an error.

Output contract: top-level systemMessage (not hookSpecificOutput).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.policy_config import (  # noqa: E402
    default_hook_state_root,
    pin_plugin_data_path_if_present,
)
from scripts.state_tracker import load_tracker, save_tracker  # noqa: E402


def main() -> None:
    payload_text = sys.stdin.read().strip()
    try:
        payload = json.loads(payload_text) if payload_text else {}
    except Exception:
        payload = {}

    error_type = str(payload.get("error", "unknown"))

    pin_plugin_data_path_if_present()
    state_root = Path(default_hook_state_root())
    state_path = state_root / "state.json"

    try:
        tracker = load_tracker(state_path)
        active_span_id = str(tracker.state.get("active_span_id") or "")
        active_span_intent = str(tracker.state.get("active_span_intent") or "")
    except Exception:
        print("{}")
        return

    if not active_span_id:
        print("{}")
        return

    try:
        tracker.state.pop("active_span_id", None)
        tracker.state.pop("active_span_intent", None)
        save_tracker(state_path, tracker)
        cleared = True
    except Exception:
        cleared = False

    if cleared:
        msg = (
            f"StopFailure ({error_type}): active span {active_span_id} "
            f"({active_span_intent}) cleared. "
            "SessionStart will clean up span-candidates on next session."
        )
    else:
        msg = (
            f"StopFailure ({error_type}): active span {active_span_id} "
            f"({active_span_intent}) found but save failed — "
            "SessionStart will clean up on next session."
        )
    print(json.dumps({"systemMessage": msg}))


if __name__ == "__main__":
    main()
