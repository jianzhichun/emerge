from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.goal_control_plane import EVENT_HOOK_PAYLOAD, GoalControlPlane  # noqa: E402
from scripts.policy_config import default_hook_state_root, pin_plugin_data_path_if_present  # noqa: E402
from scripts.state_tracker import load_tracker, save_tracker  # noqa: E402


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
    if "goal" in payload:
        goal_cp.ingest(
            event_type=EVENT_HOOK_PAYLOAD,
            source="hook_payload",
            actor="SessionStart",
            text=str(payload["goal"]),
            rationale="SessionStart hook payload goal",
            confidence=0.5,
        )
    save_tracker(state_path, tracker)
    snap = goal_cp.read_snapshot()
    context_text = tracker.format_additional_context(
        goal_override=str(snap.get("text", "")),
        goal_source_override=str(snap.get("source", "unset")),
    )

    out = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context_text,
        }
    }
    print(json.dumps(out))


if __name__ == "__main__":
    main()
