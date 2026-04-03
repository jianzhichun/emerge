from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.state_tracker import load_tracker, save_tracker  # noqa: E402


def main() -> None:
    payload_text = sys.stdin.read().strip()
    payload = json.loads(payload_text) if payload_text else {}

    state_path = Path(
        os.environ.get("CLAUDE_PLUGIN_DATA", str(Path.home() / ".emerge" / "hook-state"))
    ) / "state.json"
    tracker = load_tracker(state_path)
    if "goal" in payload:
        tracker.set_goal(str(payload["goal"]))

    budget_chars = int(payload.get("budget_chars", 0)) or None
    context = tracker.format_context(budget_chars=budget_chars)
    save_tracker(state_path, tracker)

    out = {
        "hookEventName": "UserPromptSubmit",
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
