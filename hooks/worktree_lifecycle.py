"""WorktreeCreate / WorktreeRemove hooks — span state isolation for worktrees.

When a subagent uses isolation="worktree", the new worktree shares the
canonical ~/.emerge/hook-state/state.json with the parent session, so it could
see a stale active_span_id from the parent. This hook clears that field on
WorktreeCreate so isolated subagents start clean.

WorktreeRemove is a no-op (state cleanup happens at session end), but
registered so the hook file exists for future extension.

Output contract: top-level systemMessage for the cleared-span case;
{} otherwise. (WorktreeCreate/Remove are not in hookSpecificOutput allowlist.)
"""
from __future__ import annotations

import json
import re
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

    event = str(payload.get("hook_event_name", ""))

    if event == "WorktreeRemove":
        print(json.dumps({}))
        return

    # WorktreeCreate: keep parent state intact; prepare an isolated hook-state
    # sidecar for the worktree session.
    try:
        from scripts.policy_config import default_hook_state_root
        state_root = Path(default_hook_state_root()) / "worktrees"
        state_root.mkdir(parents=True, exist_ok=True)
        raw_id = str(
            payload.get("worktree_id")
            or payload.get("worktree_path")
            or payload.get("path")
            or payload.get("cwd")
            or "unknown"
        )
        worktree_id = re.sub(r"[^A-Za-z0-9._-]+", "_", raw_id).strip("._-") or "unknown"
        sidecar = state_root / f"{worktree_id}.json"
        if not sidecar.exists():
            sidecar.write_text(
                json.dumps({"active_span_id": None, "active_span_intent": None}, ensure_ascii=False),
                encoding="utf-8",
            )
        print(json.dumps({
            "systemMessage": (
                f"Worktree created: isolated span sidecar prepared ({sidecar.name}); "
                "parent session active span remains unchanged."
            )
        }))
        return
    except Exception:
        pass

    print(json.dumps({}))


if __name__ == "__main__":
    main()
