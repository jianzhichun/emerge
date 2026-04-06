import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.goal_control_plane import GoalControlPlane
from scripts.repl_admin import _load_hook_state_summary


def test_goal_migration_from_legacy_state_json(tmp_path: Path):
    root = tmp_path / "hook-state"
    root.mkdir(parents=True, exist_ok=True)
    legacy = {
        "goal": "legacy migration goal",
        "goal_source": "hook_payload",
        "deltas": [],
        "open_risks": [],
    }
    (root / "state.json").write_text(json.dumps(legacy), encoding="utf-8")

    os.environ["CLAUDE_PLUGIN_DATA"] = str(root)
    try:
        summary = _load_hook_state_summary()
        assert summary["goal"] == "legacy migration goal"
        assert summary["goal_source"] == "hook_payload"
        cp = GoalControlPlane(root)
        snap = cp.read_snapshot()
        assert snap["text"] == "legacy migration goal"
        assert snap["source"] == "hook_payload"
    finally:
        os.environ.pop("CLAUDE_PLUGIN_DATA", None)
