from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.policy_config import default_emerge_home, _plugin_data_pin_path  # noqa: E402


def main() -> None:
    payload_text = sys.stdin.read().strip()
    try:
        payload = json.loads(payload_text) if payload_text else {}
    except Exception:
        payload = {}

    # Ensure required directories exist
    emerge_home = default_emerge_home()
    for subdir in ("hook-state", "connectors", "repl"):
        (emerge_home / subdir).mkdir(parents=True, exist_ok=True)

    # Pin CLAUDE_PLUGIN_DATA so non-hook processes (cockpit server, repl_admin)
    # can find the same state.json that hooks read/write.
    plugin_data = os.environ.get("CLAUDE_PLUGIN_DATA", "").strip()
    if plugin_data:
        pin = _plugin_data_pin_path()
        try:
            pin.write_text(plugin_data, encoding="utf-8")
        except OSError:
            pass

    out = {
        "hookSpecificOutput": {
            "hookEventName": "Setup",
            "additionalContext": f"emerge plugin ready. Home: {emerge_home}",
        }
    }
    print(json.dumps(out))


if __name__ == "__main__":
    main()
