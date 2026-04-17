from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.policy_config import default_hook_state_root  # noqa: E402
from scripts.state_tracker import load_tracker, save_tracker  # noqa: E402


def main() -> None:
    sys.stdin.read()  # consume stdin (unused by SessionEnd)

    state_root = Path(default_hook_state_root())
    state_path = state_root / "state.json"

    cleanup_performed: list[str] = []

    # Clear stale active_span_id — if a span was open when session ended,
    # it is unresolvable; SessionStart will also clear it, but belt+suspenders.
    try:
        tracker = load_tracker(state_path)
        if tracker.state.get("active_span_id"):
            tracker.state.pop("active_span_id", None)
            tracker.state.pop("active_span_intent", None)
            save_tracker(state_path, tracker)
            cleanup_performed.append("cleared_active_span")
    except Exception:
        pass

    # SessionEnd does not accept `hookSpecificOutput` —
    # use top-level `systemMessage` (or empty object when nothing to report).
    if cleanup_performed:
        out = {"systemMessage": f"session_end: cleanup_performed={','.join(cleanup_performed)}"}
    else:
        out = {}
    print(json.dumps(out))


if __name__ == "__main__":
    main()
