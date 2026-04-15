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
import time
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
        from scripts.policy_config import default_hook_state_root, pin_plugin_data_path_if_present
        pin_plugin_data_path_if_present()
        state_root = Path(default_hook_state_root())
        from scripts.state_tracker import load_tracker
        tracker = load_tracker(state_root / "state.json")
        span_id = tracker.state.get("active_span_id")
        span_intent = tracker.state.get("active_span_intent")
        if not span_id:
            print(json.dumps({}))
            return

        # Write a task_created action into the span WAL
        task_subject = str(payload.get("subject") or payload.get("task_subject") or "")
        task_id = str(payload.get("task_id") or "")
        wal_dir = state_root / "span-wal"
        wal_dir.mkdir(parents=True, exist_ok=True)
        entry = {
            "span_id": span_id,
            "intent_signature": span_intent,
            "action": "task_created",
            "task_id": task_id,
            "task_subject": task_subject,
            "ts_ms": int(time.time() * 1000),
        }
        with (wal_dir / "spans.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass

    print(json.dumps({}))


if __name__ == "__main__":
    main()
