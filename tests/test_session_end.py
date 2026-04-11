import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run_hook(stdin_payload: dict | None = None) -> dict:
    result = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "session_end.py")],
        input=json.dumps(stdin_payload or {}),
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert result.returncode == 0, f"hook exited {result.returncode}: {result.stderr}"
    return json.loads(result.stdout)


def test_session_end_hook_exits_cleanly():
    """session_end must exit 0; SessionEnd does not use hookSpecificOutput (CC schema)."""
    out = _run_hook()
    assert "hookSpecificOutput" not in out
    assert out == {} or "systemMessage" in out


def test_session_end_hook_returns_cleanup_summary(tmp_path: Path, monkeypatch):
    """When active_span_id is set, session_end reports cleanup via top-level systemMessage."""
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    from scripts.policy_config import default_hook_state_root, pin_plugin_data_path_if_present
    from scripts.state_tracker import load_tracker, save_tracker

    pin_plugin_data_path_if_present()
    state_path = Path(default_hook_state_root()) / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tracker = load_tracker(state_path)
    tracker.state["active_span_id"] = "span-test"
    tracker.state["active_span_intent"] = "mock.read.x"
    save_tracker(state_path, tracker)

    env = os.environ.copy()
    env["CLAUDE_PLUGIN_DATA"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "session_end.py")],
        input=json.dumps({}),
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        env=env,
        check=True,
    )
    out = json.loads(result.stdout.strip())
    assert "hookSpecificOutput" not in out
    assert "systemMessage" in out
    assert "cleared_active_span" in out["systemMessage"]
