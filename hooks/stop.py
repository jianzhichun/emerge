from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.span_service import SpanService  # noqa: E402
from hooks.hook_io import read_json_payload  # noqa: E402


def main() -> None:
    read_json_payload()

    active_span_id, active_span_intent = SpanService().get_active()

    if active_span_id:
        sig = active_span_intent or active_span_id
        out = {
            "decision": "block",
            "reason": (
                f"emerge: active span for '{sig}' is still open. "
                "Call icc_span_close(outcome='aborted') to close it (safe — no data lost, "
                "marks the span incomplete in the WAL), then stop."
            ),
        }
    else:
        # Stop / SubagentStop do not accept `hookSpecificOutput` —
        # the CC schema validator only allows it for PreToolUse / UserPromptSubmit /
        # PostToolUse / SessionStart. Safe path: emit an empty object.
        out = {}
    print(json.dumps(out))


if __name__ == "__main__":
    main()
