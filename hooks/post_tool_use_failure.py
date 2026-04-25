from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.policy_config import default_hook_state_root  # noqa: E402
from scripts.state_tracker import with_locked_tracker  # noqa: E402


def main() -> None:
    payload_text = sys.stdin.read().strip()
    try:
        payload = json.loads(payload_text) if payload_text else {}
    except Exception:
        payload = {}

    tool_name = payload.get("tool_name", "unknown")
    error_text = str(payload.get("error", "unknown error"))
    is_interrupt = bool(payload.get("is_interrupt", False))
    state_path = Path(default_hook_state_root()) / "state.json"

    try:
        def _mutate(tracker):
            if is_interrupt:
                return
            tracker.mark_degraded(f"Tool failure: {tool_name} — {error_text[:120]}")

        with_locked_tracker(state_path, _mutate)
    except Exception as exc:
        print(f"post_tool_use_failure: tracker update failed: {exc}", file=sys.stderr)

    # PostToolUseFailure does not accept `hookSpecificOutput` —
    # use top-level `systemMessage` for context injection.
    out = {"systemMessage": f"Tool {tool_name} failed: {error_text[:200]}"}
    print(json.dumps(out))


if __name__ == "__main__":
    main()
