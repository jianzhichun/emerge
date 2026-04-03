from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.policy_config import default_hook_state_root  # noqa: E402
from scripts.state_tracker import load_tracker  # noqa: E402

_BUDGET_CHARS = 800


def main() -> None:
    payload_text = sys.stdin.read().strip()
    try:
        payload = json.loads(payload_text) if payload_text else {}
    except Exception:
        payload = {}

    state_path = Path(
        os.environ.get("CLAUDE_PLUGIN_DATA", str(default_hook_state_root()))
    ) / "state.json"
    tracker = load_tracker(state_path)

    token = tracker.format_recovery_token(budget_chars=_BUDGET_CHARS)
    token_json = json.dumps(token, ensure_ascii=True, separators=(",", ":"))
    context_text = (
        f"Goal\n{token.get('goal') or 'Not set.'}\n\n"
        f"Open Risks\n"
        + ("\n".join(f"- {r}" for r in token.get("open_risks", [])) or "- None.")
        + f"\n\nL1_5_TOKEN\n{token_json}"
    )

    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreCompact",
            "additionalContext": context_text,
        }
    }
    print(json.dumps(out))


if __name__ == "__main__":
    main()
