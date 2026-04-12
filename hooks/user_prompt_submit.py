from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.goal_control_plane import EVENT_HOOK_PAYLOAD, init_goal_control_plane  # noqa: E402
from scripts.policy_config import default_hook_state_root, pin_plugin_data_path_if_present  # noqa: E402
from scripts.state_tracker import load_tracker, save_tracker  # noqa: E402


def _drain_pending_actions(state_root: Path) -> str:
    """Read and consume pending cockpit actions. Returns formatted text or ''."""
    from scripts.pending_actions import format_pending_actions
    for name in ("pending-actions.processed.json", "pending-actions.json"):
        p = state_root / name
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        actions = data.get("actions", [])
        if not actions:
            continue
        delivered = state_root / "pending-actions.delivered.json"
        try:
            p.rename(delivered)
        except OSError:
            pass
        return format_pending_actions(actions)
    return ""


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
    goal_cp = init_goal_control_plane(state_root, tracker)
    _mutated = False
    if "goal" in payload:
        goal_cp.ingest(
            event_type=EVENT_HOOK_PAYLOAD,
            source="hook_payload",
            actor="UserPromptSubmit",
            text=str(payload["goal"]),
            rationale="UserPromptSubmit hook payload goal",
            confidence=0.5,
        )
        _mutated = True

    raw_budget = payload.get("budget_chars", 0)
    try:
        budget_chars = int(raw_budget)
        if budget_chars <= 0:
            budget_chars = None
    except Exception:
        budget_chars = None
    snap = goal_cp.read_snapshot()
    context_text = tracker.format_additional_context(
        budget_chars=budget_chars,
        goal_override=str(snap.get("text", "")),
        goal_source_override=str(snap.get("source", "unset")),
    )
    if _mutated:
        save_tracker(state_path, tracker)

    pending_text = _drain_pending_actions(state_root)
    if pending_text:
        context_text = pending_text + "\n\n" + context_text

    out = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context_text,
        }
    }
    print(json.dumps(out))


if __name__ == "__main__":
    main()
