"""TeammateIdle hook — keep emerge-monitors watcher agents alive.

When a teammate in the `emerge-monitors` team whose name ends with `-watcher`
is about to go idle, exit with code 2. CC sends the stderr text back to the
agent as feedback, causing it to continue working instead of going idle.

All other agents (other teams, or non-watcher teammates in emerge-monitors)
are allowed to go idle normally — return `{}`, exit 0.

Output contract: TeammateIdle is NOT in CC's hookSpecificOutput-allowed list.
Use raw stderr text + exit code 2 to keep the agent running.
"""
from __future__ import annotations

import json
import sys


def main() -> None:
    payload_text = sys.stdin.read().strip()
    try:
        payload = json.loads(payload_text) if payload_text else {}
    except Exception:
        payload = {}

    team_name = str(payload.get("team_name") or "")
    teammate_name = str(payload.get("teammate_name") or "")

    if team_name == "emerge-monitors" and teammate_name.endswith("-watcher"):
        profile = teammate_name.removesuffix("-watcher")
        print(
            f"[emerge/TeammateIdle] {teammate_name} is an emerge monitor agent — do NOT go idle. "
            f"Re-launch the Monitor tool with: command "
            f"`python3 ${{CLAUDE_PLUGIN_ROOT}}/scripts/watch_emerge.py --runner-profile {profile}`, "
            "description `pattern alert watcher`, persistent: true. "
            "Then wait for the next pattern alert and apply the stage→action protocol.",
            file=sys.stderr,
        )
        sys.exit(2)

    # All other agents: allow idle normally
    print(json.dumps({}))


if __name__ == "__main__":
    main()
