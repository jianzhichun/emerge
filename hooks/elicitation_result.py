from __future__ import annotations

import json
import os
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

    # Only log emerge elicitations
    mcp_server = str(payload.get("mcp_server_name", "") or "")
    if "emerge" not in mcp_server:
        print(json.dumps({}))
        return

    try:
        data_root_env = os.environ.get("CLAUDE_PLUGIN_DATA", "")
        if data_root_env:
            log_dir = Path(data_root_env)
        else:
            from scripts.policy_config import default_hook_state_root, pin_plugin_data_path_if_present
            pin_plugin_data_path_if_present()
            log_dir = Path(default_hook_state_root())
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "elicitation-log.jsonl"
        entry = {
            "ts_ms": int(time.time() * 1000),
            "elicitation_id": str(payload.get("elicitation_id", "") or ""),
            "mcp_server_name": mcp_server,
            "action": str(payload.get("action", "") or ""),
            "content": payload.get("content") or {},
            "mode": str(payload.get("mode", "") or ""),
        }
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=True) + "\n")
    except Exception:
        pass

    # ElicitationResult uses top-level systemMessage or empty
    print(json.dumps({}))


if __name__ == "__main__":
    main()
