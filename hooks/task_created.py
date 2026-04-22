"""TaskCreated hook — associate task creation with the active span WAL.

When a task is created (via TaskCreate) while a flywheel span is open,
this hook records a task_created entry in the span's WAL. This enriches
crystallization data and gives the pipeline a complete picture of what
high-level work happened during the span.

Only fires when there is an active_span_id in state.json.

Output contract: hookSpecificOutput.additionalContext (TaskCreated is in the
CC hookSpecificOutput allowed list per SubagentStart precedent — if not
allowed, falls back to systemMessage silently).
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

    try:
        from scripts.span_service import SpanService
        task_subject = str(payload.get("subject") or payload.get("task_subject") or "")
        task_id = str(payload.get("task_id") or "")
        SpanService().append_task_created_action(task_id=task_id, task_subject=task_subject)
    except Exception:
        pass

    print(json.dumps({}))


if __name__ == "__main__":
    main()
