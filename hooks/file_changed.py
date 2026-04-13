from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _state_root() -> Path:
    data_root_env = os.environ.get("CLAUDE_PLUGIN_DATA", "")
    if data_root_env:
        return Path(data_root_env)
    try:
        from scripts.policy_config import default_hook_state_root, pin_plugin_data_path_if_present
        pin_plugin_data_path_if_present()
        return Path(default_hook_state_root())
    except Exception:
        return Path.home() / ".emerge"


def _watch_paths(state_root: Path) -> list[str]:
    """Absolute paths CC should keep watching."""
    return [str(state_root / "pending-actions.json")]


def main() -> None:
    payload_text = sys.stdin.read().strip()
    try:
        payload = json.loads(payload_text) if payload_text else {}
    except Exception:
        payload = {}

    file_path = str(payload.get("file_path", "") or "")
    state_root = _state_root()
    watch_list = _watch_paths(state_root)

    # Only process pending-actions files written by the cockpit
    is_pending = file_path.endswith("pending-actions.json") and "pending-actions.delivered" not in file_path

    if not is_pending:
        # Return watchPaths to keep the watch list alive even for unrelated events
        print(json.dumps({"watchPaths": watch_list}))
        return

    p = Path(file_path)
    if not p.exists():
        print(json.dumps({"watchPaths": watch_list}))
        return

    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print(json.dumps({"watchPaths": watch_list}))
        return

    actions = data.get("actions", [])
    if not actions:
        print(json.dumps({"watchPaths": watch_list}))
        return

    # Deliver by renaming (same contract as UserPromptSubmit drain)
    delivered = p.parent / "pending-actions.delivered.json"
    try:
        p.rename(delivered)
    except OSError:
        pass

    from scripts.pending_actions import format_pending_actions
    ctx = format_pending_actions(actions)

    out = {
        "hookSpecificOutput": {
            "hookEventName": "FileChanged",
            "additionalContext": ctx,
        },
        "watchPaths": watch_list,
    }
    print(json.dumps(out))


if __name__ == "__main__":
    main()
