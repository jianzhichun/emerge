from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.goal_control_plane import GoalControlPlane  # noqa: E402
from scripts.policy_config import default_emerge_home, pin_plugin_data_path_if_present  # noqa: E402


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

    # Pin CLAUDE_PLUGIN_DATA so non-hook processes can resolve the same state root.
    pin_plugin_data_path_if_present()
    GoalControlPlane().ensure_initialized()

    emerge_pending = emerge_home / "pending-actions.json"
    # watchPaths seeds CC's FileChanged watch list for pending-actions delivery.
    # Setup uses top-level systemMessage + watchPaths (hookSpecificOutput not allowed on Setup).
    out = {
        "systemMessage": f"emerge plugin ready. Home: {emerge_home}",
        "watchPaths": [str(emerge_pending)],
    }
    print(json.dumps(out))


if __name__ == "__main__":
    main()
