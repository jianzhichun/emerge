from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.policy_config import default_hook_state_root  # noqa: E402
from scripts.state_tracker import load_tracker, save_tracker  # noqa: E402


def main() -> None:
    payload_text = sys.stdin.read().strip()
    try:
        payload = json.loads(payload_text) if payload_text else {}
    except Exception:
        payload = {}
    goal = payload.get("goal", "Initialize Emerge session")

    state_path = Path(
        os.environ.get("CLAUDE_PLUGIN_DATA", str(default_hook_state_root()))
    ) / "state.json"
    tracker = load_tracker(state_path)
    tracker.set_goal(goal)
    save_tracker(state_path, tracker)
    context = tracker.format_context()

    out = {
        "hookEventName": "SessionStart",
        "hookSpecificOutput": {
            "additionalContext": (
                f"Goal\n{context['Goal']}\n\n"
                f"Delta\n{context['Delta']}\n\n"
                f"Open Risks\n{context['Open Risks']}"
            )
        },
    }
    print(json.dumps(out))


if __name__ == "__main__":
    main()
