"""InstructionsLoaded hook — lightweight span guardrail on instruction load."""
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
        json.loads(payload_text) if payload_text else {}
    except Exception:
        pass

    from scripts.policy_config import default_hook_state_root
    from scripts.state_tracker import load_tracker

    state_root = Path(default_hook_state_root())

    try:
        tracker = load_tracker(state_root / "state.json")
        span_id = tracker.state.get("active_span_id")
        span_intent = tracker.state.get("active_span_intent") or ""
        if span_id:
            print(json.dumps({
                "systemMessage": (
                    f"[Span active: {span_intent or span_id}] "
                    "Do NOT call icc_span_open — call icc_span_close when done."
                )
            }))
            return
    except Exception:
        pass

    print(json.dumps({}))


if __name__ == "__main__":
    main()
