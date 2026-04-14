from __future__ import annotations
import json
import pytest
from pathlib import Path


def test_monitors_returns_empty_when_no_state(tmp_path):
    """cmd_control_plane_monitors returns empty runners when no state file."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.repl_admin import cmd_control_plane_monitors
    result = cmd_control_plane_monitors(state_root=tmp_path)
    assert result["runners"] == []
    assert "team_active" in result


def test_monitors_reads_state_file(tmp_path):
    """cmd_control_plane_monitors reads runner-monitor-state.json."""
    from scripts.repl_admin import cmd_control_plane_monitors
    state = {
        "runners": [
            {
                "runner_profile": "mycader-1",
                "connected": True,
                "connected_at_ms": 1000,
                "last_event_ts_ms": 2000,
                "machine_id": "wkst-A",
                "last_alert": None,
            }
        ],
        "team_active": False,
    }
    (tmp_path / "runner-monitor-state.json").write_text(json.dumps(state))
    result = cmd_control_plane_monitors(state_root=tmp_path)
    assert len(result["runners"]) == 1
    assert result["runners"][0]["runner_profile"] == "mycader-1"
