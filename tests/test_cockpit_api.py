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


def test_cmd_control_plane_reflection_cache_missing(tmp_path):
    from scripts.repl_admin import cmd_control_plane_reflection_cache
    with patch("scripts.repl_admin._resolve_state_root", return_value=tmp_path):
        result = cmd_control_plane_reflection_cache()
    assert result["ok"] is True
    assert result["exists"] is False
    assert result["source"] == "lightweight"


def test_cmd_control_plane_reflection_cache_fresh(tmp_path):
    from scripts.repl_admin import cmd_control_plane_reflection_cache
    cache_dir = tmp_path / "reflection-cache"
    cache_dir.mkdir(parents=True)
    now_ms = 1234567890000
    (cache_dir / "global.json").write_text(
        json.dumps(
            {
                "generated_at_ms": now_ms,
                "summary_text": "Muscle memory (deep)\nHigh-confidence intents: lark.read.get-doc",
                "meta": {"builder": "test"},
            }
        ),
        encoding="utf-8",
    )
    with patch("scripts.repl_admin._resolve_state_root", return_value=tmp_path), \
         patch("scripts.repl_admin.time.time", return_value=now_ms / 1000):
        result = cmd_control_plane_reflection_cache(ttl_ms=900000)
    assert result["ok"] is True
    assert result["exists"] is True
    assert result["is_fresh"] is True
    assert result["source"] == "deep_cache"
    assert "Muscle memory (deep)" in result["summary_preview"]


def test_cmd_control_plane_reflection_cache_stale(tmp_path):
    from scripts.repl_admin import cmd_control_plane_reflection_cache
    cache_dir = tmp_path / "reflection-cache"
    cache_dir.mkdir(parents=True)
    generated_ms = 1000
    now_ms = generated_ms + 901000
    (cache_dir / "global.json").write_text(
        json.dumps({"generated_at_ms": generated_ms, "summary_text": "stale cache"}),
        encoding="utf-8",
    )
    with patch("scripts.repl_admin._resolve_state_root", return_value=tmp_path), \
         patch("scripts.repl_admin.time.time", return_value=now_ms / 1000):
        result = cmd_control_plane_reflection_cache(ttl_ms=900000)
    assert result["ok"] is True
    assert result["exists"] is True
    assert result["is_fresh"] is False
    assert result["source"] == "lightweight"
    assert result["age_ms"] == 901000


def test_cmd_control_plane_reflection_cache_invalid_json(tmp_path):
    from scripts.repl_admin import cmd_control_plane_reflection_cache
    cache_dir = tmp_path / "reflection-cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "global.json").write_text("{broken", encoding="utf-8")
    with patch("scripts.repl_admin._resolve_state_root", return_value=tmp_path):
        result = cmd_control_plane_reflection_cache()
    assert result["ok"] is True
    assert result["exists"] is True
    assert result["is_fresh"] is False
    assert result["source"] == "lightweight"
    assert result["error"] == "invalid_cache_json"


def test_cmd_control_plane_session_reset_full_clears_session_artifacts(tmp_path):
    from scripts.repl_admin import cmd_control_plane_session_reset

    hook_state = tmp_path / "hook"
    hook_state.mkdir(parents=True, exist_ok=True)
    (hook_state / "state.json").write_text("{}", encoding="utf-8")

    session_dir = tmp_path / "sess"
    session_dir.mkdir(parents=True, exist_ok=True)
    for name in ["wal.jsonl", "checkpoint.json", "recovery.json", "exec-events.jsonl", "pipeline-events.jsonl"]:
        (session_dir / name).write_text("x", encoding="utf-8")

    with patch("scripts.repl_admin.default_hook_state_root", return_value=str(hook_state)), \
         patch("scripts.repl_admin._session_paths", return_value=(session_dir, session_dir / "wal.jsonl", session_dir / "checkpoint.json")):
        result = cmd_control_plane_session_reset(confirm="RESET", full=True)

    assert result["ok"] is True
    assert result["full"] is True
    for name in ["wal.jsonl", "checkpoint.json", "recovery.json", "exec-events.jsonl", "pipeline-events.jsonl"]:
        assert not (session_dir / name).exists()
