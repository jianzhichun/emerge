import json
from pathlib import Path
from unittest.mock import patch

from scripts.state_tracker import StateTracker, save_tracker, LEVEL_CORE_CRITICAL


def _make_state_json(tmp: Path, deltas=None, risks=None):
    tracker = StateTracker()
    for d in (deltas or []):
        tracker.add_delta(**d)
    for r in (risks or []):
        tracker.add_risk(r)
    save_tracker(tmp / "state.json", tracker)
    return tmp / "state.json"


def test_cmd_control_plane_state_returns_deltas_and_risks(tmp_path):
    from scripts.repl_admin import cmd_control_plane_state
    _make_state_json(tmp_path, deltas=[
        {"message": "test delta", "intent_signature": "mock.read.x"},
    ], risks=["test risk"])
    with patch("scripts.repl_admin.default_hook_state_root", return_value=str(tmp_path)):
        result = cmd_control_plane_state()
    assert result["ok"]
    assert len(result["deltas"]) == 1
    assert result["deltas"][0]["intent_signature"] == "mock.read.x"
    assert len(result["risks"]) == 1
    assert result["risks"][0]["text"] == "test risk"
    assert "verification_state" in result


def test_cmd_control_plane_delta_reconcile(tmp_path):
    from scripts.repl_admin import cmd_control_plane_delta_reconcile
    tracker = StateTracker()
    delta_id = tracker.add_delta(message="test", intent_signature="mock.read.x")
    save_tracker(tmp_path / "state.json", tracker)
    with patch("scripts.repl_admin.default_hook_state_root", return_value=str(tmp_path)):
        result = cmd_control_plane_delta_reconcile(delta_id=delta_id, outcome="confirm")
    assert result["ok"]
    assert result["outcome"] == "confirm"


def test_cmd_control_plane_risk_update(tmp_path):
    from scripts.repl_admin import cmd_control_plane_risk_update
    tracker = StateTracker()
    tracker.add_risk("test risk")
    save_tracker(tmp_path / "state.json", tracker)
    risk_id = tracker.to_dict()["open_risks"][0]["risk_id"]
    with patch("scripts.repl_admin.default_hook_state_root", return_value=str(tmp_path)):
        result = cmd_control_plane_risk_update(risk_id=risk_id, action="handle", reason="fixed")
    assert result["ok"]


def test_cmd_control_plane_policy_freeze(tmp_path):
    from scripts.repl_admin import cmd_control_plane_policy_freeze
    reg = {"pipelines": {"mock.read.layers": {"status": "explore"}}}
    (tmp_path / "pipelines-registry.json").write_text(json.dumps(reg))
    with patch("scripts.repl_admin._resolve_state_root", return_value=tmp_path):
        result = cmd_control_plane_policy_freeze(key="mock.read.layers")
    assert result["ok"]
    updated = json.loads((tmp_path / "pipelines-registry.json").read_text())
    assert updated["pipelines"]["mock.read.layers"]["frozen"] is True


def test_cmd_control_plane_session_reset_requires_confirm(tmp_path):
    from scripts.repl_admin import cmd_control_plane_session_reset
    result = cmd_control_plane_session_reset(confirm="nope")
    assert not result["ok"]
    assert "RESET" in result["error"]
