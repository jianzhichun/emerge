"""WorktreeCreate / WorktreeRemove hooks — span state isolation for worktrees.

When a subagent uses isolation="worktree", the new worktree shares the
canonical ~/.emerge/hook-state/state.json with the parent session, so it could
see a stale active_span_id from the parent. This hook clears that field on
WorktreeCreate so isolated subagents start clean.

WorktreeRemove is a no-op (state cleanup happens at session end), but
registered so the hook file exists for future extension.

Output contract: top-level systemMessage for the cleared-span case;
{} otherwise. (WorktreeCreate/Remove are not in hookSpecificOutput allowlist.)
"""
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

    event = str(payload.get("hook_event_name", ""))

    if event == "WorktreeRemove":
        print(json.dumps({}))
        return

    # WorktreeCreate: clear stale span state so the new worktree starts clean.
    try:
        from scripts.policy_config import default_hook_state_root
        state_root = Path(default_hook_state_root())
        from scripts.state_tracker import load_tracker, save_tracker
        state_path = state_root / "state.json"
        tracker = load_tracker(state_path)
        cleared = False
        for field in ("active_span_id", "active_span_intent"):
            if tracker.state.pop(field, None) is not None:
                cleared = True
        if cleared:
            save_tracker(state_path, tracker)
            print(json.dumps({
                "systemMessage": (
                    "Worktree created: cleared inherited active_span_id from parent session. "
                    "This worktree has no active span."
                )
            }))
            return
    except Exception:
        pass

    print(json.dumps({}))


if __name__ == "__main__":
    main()
