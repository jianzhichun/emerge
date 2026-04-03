from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.policy_config import default_hook_state_root  # noqa: E402
from scripts.state_tracker import (  # noqa: E402
    LEVEL_CORE_CRITICAL,
    LEVEL_CORE_SECONDARY,
    LEVEL_PERIPHERAL,
    load_tracker,
    save_tracker,
)


def _classify_level(tool_name: str) -> str:
    if tool_name.endswith("__icc_write"):
        return LEVEL_CORE_CRITICAL
    if tool_name.endswith("__icc_read"):
        return LEVEL_CORE_SECONDARY
    return LEVEL_PERIPHERAL


def main() -> None:
    payload_text = sys.stdin.read().strip()
    try:
        payload = json.loads(payload_text) if payload_text else {}
    except Exception:
        payload = {}

    tool_name = payload.get("tool_name", "")
    raw_result = payload.get("tool_result", {})
    result = raw_result if isinstance(raw_result, dict) else {}
    state_path = Path(
        os.environ.get("CLAUDE_PLUGIN_DATA", str(default_hook_state_root()))
    ) / "state.json"
    tracker = load_tracker(state_path)

    message = payload.get("delta_message") or f"Tool used: {tool_name or 'unknown'}"
    level = _classify_level(tool_name)
    provisional = bool(payload.get("provisional", False))
    verification_state = str(result.get("verification_state", "verified"))
    delta_id = tracker.add_delta(
        message=message,
        level=level,
        verification_state=verification_state,
        provisional=provisional,
    )

    if payload.get("mismatch_reason"):
        tracker.mark_degraded(str(payload["mismatch_reason"]))

    reconcile = payload.get("reconcile")
    if isinstance(reconcile, dict) and "delta_id" in reconcile and "outcome" in reconcile:
        tracker.reconcile_delta(str(reconcile["delta_id"]), str(reconcile["outcome"]))

    raw_budget = payload.get("budget_chars", 0)
    try:
        budget_chars = int(raw_budget)
        if budget_chars <= 0:
            budget_chars = None
    except Exception:
        budget_chars = None
    context_text = tracker.format_additional_context(budget_chars=budget_chars)
    save_tracker(state_path, tracker)

    output = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": context_text,
        }
    }
    if isinstance(payload.get("tool_result"), dict):
        output["hookSpecificOutput"]["updatedMCPToolOutput"] = payload["tool_result"]
    print(json.dumps(output))


if __name__ == "__main__":
    main()
