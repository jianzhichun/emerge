from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.policy_config import default_hook_state_root, _plugin_data_pin_path  # noqa: E402
from scripts.state_tracker import load_tracker, save_tracker  # noqa: E402


def _pin_plugin_data() -> None:
    plugin_data = os.environ.get("CLAUDE_PLUGIN_DATA", "").strip()
    if plugin_data:
        pin = _plugin_data_pin_path()
        try:
            pin.write_text(plugin_data, encoding="utf-8")
        except OSError:
            pass


def main() -> None:
    payload_text = sys.stdin.read().strip()
    try:
        payload = json.loads(payload_text) if payload_text else {}
    except Exception:
        payload = {}
    _pin_plugin_data()
    state_path = Path(
        os.environ.get("CLAUDE_PLUGIN_DATA", str(default_hook_state_root()))
    ) / "state.json"
    tracker = load_tracker(state_path)
    if "goal" in payload:
        tracker.set_goal(str(payload["goal"]), source="hook_payload")
    save_tracker(state_path, tracker)
    context_text = tracker.format_additional_context()

    out = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context_text,
        }
    }
    print(json.dumps(out))


if __name__ == "__main__":
    main()
