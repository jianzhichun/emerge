from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.goal_control_plane import GoalControlPlane  # noqa: E402
from scripts.policy_config import default_hook_state_root, pin_plugin_data_path_if_present  # noqa: E402
from scripts.state_tracker import StateTracker, load_tracker, save_tracker  # noqa: E402

_BUDGET_CHARS = 800


def main() -> None:
    payload_text = sys.stdin.read().strip()
    try:
        payload = json.loads(payload_text) if payload_text else {}
    except Exception:
        payload = {}

    pin_plugin_data_path_if_present()
    state_root = Path(default_hook_state_root())
    state_path = state_root / "state.json"
    tracker = load_tracker(state_path)
    goal_cp = GoalControlPlane(state_root)
    goal_cp.ensure_initialized()
    goal_cp.migrate_legacy_goal(
        legacy_goal=str(tracker.to_dict().get("goal", "")),
        legacy_source=str(tracker.to_dict().get("goal_source", "legacy")),
    )
    snap = goal_cp.read_snapshot()

    token = tracker.format_recovery_token(
        budget_chars=_BUDGET_CHARS,
        goal_override=str(snap.get("text", "")),
        goal_source_override=str(snap.get("source", "unset")),
    )
    token_json = json.dumps(token, ensure_ascii=True, separators=(",", ":"))
    context_text = (
        f"Goal\n{str(snap.get('text', '')) or 'Not set.'}\n\n"
        f"Open Risks\n"
        + ("\n".join(f"- {r}" for r in token.get("open_risks", [])) or "- None.")
        + f"\n\nFLYWHEEL_TOKEN\n{token_json}"
    )

    # Reset tracker so the next session starts fresh — stale deltas/risks are cleared.
    fresh = StateTracker()
    save_tracker(state_path, fresh)

    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreCompact",
            "additionalContext": context_text,
        }
    }
    print(json.dumps(out))


if __name__ == "__main__":
    main()
