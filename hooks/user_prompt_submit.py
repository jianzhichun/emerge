from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.policy_config import REFLECTION_CACHE_TTL_MS, default_state_root, default_hook_state_root  # noqa: E402
from scripts.span_tracker import SpanTracker  # noqa: E402
from scripts.state_tracker import load_tracker, save_tracker  # noqa: E402

_REFLECTION_TURN_THRESHOLD = 1
_SPAN_REMINDER_INTERVAL = 5


def main() -> None:
    payload_text = sys.stdin.read().strip()
    try:
        payload = json.loads(payload_text) if payload_text else {}
    except Exception:
        payload = {}

    state_root = Path(default_hook_state_root())
    state_path = state_root / "state.json"
    tracker = load_tracker(state_path)
    turn_count = int(tracker.state.get("turn_count", 0) or 0) + 1
    tracker.state["turn_count"] = turn_count
    save_tracker(state_path, tracker)

    raw_budget = payload.get("budget_chars", 0)
    try:
        budget_chars = int(raw_budget)
        if budget_chars <= 0:
            budget_chars = None
    except Exception:
        budget_chars = None
    context_text = tracker.format_additional_context(budget_chars=budget_chars)
    if turn_count == _REFLECTION_TURN_THRESHOLD:
        exec_root = Path(os.environ.get("EMERGE_STATE_ROOT", str(default_state_root())))
        reflection = SpanTracker(
            state_root=exec_root,
            hook_state_root=state_root,
        ).format_reflection_with_cache(cache_ttl_ms=REFLECTION_CACHE_TTL_MS)
        if reflection:
            context_text = reflection + "\n\n" + context_text

    active_span_id = str(tracker.state.get("active_span_id", "") or "")
    if not active_span_id and turn_count > 1 and turn_count % _SPAN_REMINDER_INTERVAL == 0:
        _skip_reminder = False
        if turn_count == _SPAN_REMINDER_INTERVAL:
            try:
                _raw = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
                _skip_reminder = bool(_raw.get("_span_nudge_sent"))
            except Exception:
                pass
        if not _skip_reminder:
            reminder = (
                "[Span] No active span. "
                "If this turn involves repeatable tool use, open one first: "
                "icc_span_open(intent_signature='<connector>.(read|write).<name>') "
                "e.g. 'lark.read.get-doc'."
            )
            context_text = reminder + "\n\n" + context_text

    out = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context_text,
        }
    }
    print(json.dumps(out))


if __name__ == "__main__":
    main()
