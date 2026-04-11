from __future__ import annotations

import json
import os
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

    data_root_env = os.environ.get("EMERGE_DATA_ROOT", "")
    if data_root_env:
        state_path = Path(data_root_env) / "state.json"
    else:
        try:
            from scripts.policy_config import default_hook_state_root, pin_plugin_data_path_if_present
            pin_plugin_data_path_if_present()
            state_path = Path(default_hook_state_root()) / "state.json"
        except Exception:
            state_path = Path.home() / ".emerge" / "state.json"

    active_span_id: str = ""
    active_span_intent: str = ""
    try:
        if state_path.exists():
            raw = json.loads(state_path.read_text(encoding="utf-8"))
            active_span_id = str(raw.get("active_span_id") or "")
            active_span_intent = str(raw.get("active_span_intent") or "")
    except Exception:
        pass

    if active_span_id:
        sig = active_span_intent or active_span_id
        out = {
            "decision": "block",
            "reason": (
                f"emerge: active span for '{sig}' is still open. "
                "Call icc_span_close(outcome='aborted') before stopping, "
                "or the flywheel WAL will have an incomplete record."
            ),
        }
    else:
        out = {
            "hookSpecificOutput": {
                "hookEventName": payload.get("hook_event_name", "Stop"),
                "additionalContext": "emerge: no active span — safe to stop",
            }
        }
    print(json.dumps(out))


if __name__ == "__main__":
    main()
