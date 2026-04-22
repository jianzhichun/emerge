"""SubagentStart hook — inject parent-span ownership guardrail into subagent context.

When the parent session has an active span, informs the subagent that it must NOT
call icc_span_close — the parent session owns the span lifecycle. Subagent PostToolUse
hooks already record icc_* calls into the shared span WAL via state.json.

Output contract: top-level systemMessage (not hookSpecificOutput).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.span_service import SpanService  # noqa: E402


def main() -> None:
    sys.stdin.read()

    active_span_id, active_span_intent = SpanService().get_active()

    if not active_span_id:
        print("{}")
        return

    msg = (
        f"Active span {active_span_id} ({active_span_intent}) is open in the parent session. "
        "Do NOT call icc_span_close — the parent session manages this span's lifecycle. "
        "Your icc_* tool calls will be recorded into the span WAL automatically."
    )
    print(json.dumps({"systemMessage": msg}))


if __name__ == "__main__":
    main()
