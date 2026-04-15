"""TaskCompleted hook — block task completion when a flywheel span is open.

Exit code 2 + stderr: task not marked complete; stderr fed back to model as feedback.
Exit code 0 + '{}':   task completes normally.

Output contract: no hookSpecificOutput (TaskCompleted not in allowed list).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.policy_config import default_hook_state_root, pin_plugin_data_path_if_present  # noqa: E402
from scripts.state_tracker import load_tracker  # noqa: E402


def main() -> None:
    sys.stdin.read()

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

    sig = active_span_intent or active_span_id
    print(
        f"Active span {active_span_id} ({sig}) is open. "
        "Call icc_span_close(outcome='aborted') before marking this task complete.",
        file=sys.stderr,
    )
    sys.exit(2)


if __name__ == "__main__":
    main()
