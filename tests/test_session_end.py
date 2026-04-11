import json
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
    """session_end hook must exit 0 and emit valid SessionEnd hookSpecificOutput."""
    out = _run_hook()
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionEnd"


def test_session_end_hook_returns_cleanup_summary():
    """session_end hook output must include a cleanup_performed key."""
    out = _run_hook()
    assert "cleanup_performed" in out["hookSpecificOutput"]
