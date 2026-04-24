from pathlib import Path
import sys
import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.pipeline_engine import PipelineEngine


def test_run_read_returns_structured_rows():
    engine = PipelineEngine(root=ROOT)
    result = engine.run_read({"connector": "mock", "pipeline": "layers", "document_id": "d1"})
    assert result["pipeline_id"] == "mock.read.layers"
    assert isinstance(result["rows"], list)
    assert result["rows"][0]["document_id"] == "d1"
    assert result["verify_result"]["ok"] is True
    assert result["verification_state"] == "verified"


def test_run_write_runs_action_and_verify():
    engine = PipelineEngine(root=ROOT)
    result = engine.run_write(
        {"connector": "mock", "pipeline": "add-wall", "wall_id": "W9", "length": 2000}
    )
    assert result["pipeline_id"] == "mock.write.add-wall"
    assert result["action_result"]["wall_id"] == "W9"
    assert result["verify_result"]["ok"] is True
    assert result["verification_state"] == "verified"
    assert result["policy_enforced"] is False
    assert result["stop_triggered"] is False
    assert result["rollback_executed"] is False
    assert result["rollback_result"] is None


def test_run_write_stop_policy_is_enforced_on_verify_failure():
    engine = PipelineEngine(root=ROOT)
    result = engine.run_write(
        {"connector": "mock", "pipeline": "add-wall", "wall_id": "W-stop", "length": 0}
    )
    assert result["verification_state"] == "degraded"
    assert result["rollback_or_stop_policy"] == "stop"
    assert result["policy_enforced"] is True
    assert result["stop_triggered"] is True
    assert result["rollback_executed"] is False


def test_run_write_rollback_policy_executes_rollback_on_failure():
    engine = PipelineEngine(root=ROOT)
    result = engine.run_write(
        {"connector": "mock", "pipeline": "add-wall-rollback", "wall_id": "W-rb", "length": 1200}
    )
    assert result["verification_state"] == "degraded"
    assert result["rollback_or_stop_policy"] == "rollback"
    assert result["policy_enforced"] is True
    assert result["stop_triggered"] is False
    assert result["rollback_executed"] is True
    assert result["rollback_result"]["ok"] is True


def test_run_write_requires_verify_write(tmp_path: Path):
    connector_dir = tmp_path / "connectors" / "demo" / "pipelines" / "write"
    connector_dir.mkdir(parents=True, exist_ok=True)
    (connector_dir / "missing-verify.yaml").write_text(
        "intent_signature: write.demo.missing-verify\n"
        "write_steps:\n"
        "  - a\n"
        "verify_steps:\n"
        "  - b\n"
        "rollback_or_stop_policy: stop\n",
        encoding="utf-8",
    )
    (connector_dir / "missing-verify.py").write_text(
        "def run_write(metadata, args):\n    return {'ok': True}\n",
        encoding="utf-8",
    )
    engine = PipelineEngine(root=tmp_path)
    with pytest.raises(ValueError, match="verify_write is required"):
        engine.run_write({"connector": "demo", "pipeline": "missing-verify"})


def test_run_write_rollback_missing_handler_triggers_stop(tmp_path: Path):
    connector_dir = tmp_path / "connectors" / "demo" / "pipelines" / "write"
    connector_dir.mkdir(parents=True, exist_ok=True)
    (connector_dir / "rollback-missing.yaml").write_text(
        "intent_signature: write.demo.rollback-missing\n"
        "write_steps:\n"
        "  - a\n"
        "verify_steps:\n"
        "  - b\n"
        "rollback_or_stop_policy: rollback\n",
        encoding="utf-8",
    )
    (connector_dir / "rollback-missing.py").write_text(
        "def run_write(metadata, args):\n    return {'ok': True}\n\n"
        "def verify_write(metadata, args, action_result):\n    return {'ok': False}\n",
        encoding="utf-8",
    )
    engine = PipelineEngine(root=tmp_path)
    result = engine.run_write({"connector": "demo", "pipeline": "rollback-missing"})
    assert result["verification_state"] == "degraded"
    assert result["rollback_executed"] is False
    assert result["stop_triggered"] is True
    assert result["rollback_result"]["ok"] is False


def test_load_metadata_rejects_missing_intent_signature(tmp_path):
    from scripts.pipeline_engine import PipelineEngine
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "rollback_or_stop_policy: stop\n"
        "read_steps:\n"
        "  - x\n"
        "verify_steps:\n"
        "  - y\n"
    )
    engine = PipelineEngine()
    with pytest.raises(ValueError, match="intent_signature"):
        engine._load_metadata(bad)


def test_load_metadata_rejects_invalid_policy(tmp_path):
    from scripts.pipeline_engine import PipelineEngine
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "intent_signature: s\n"
        "rollback_or_stop_policy: unknown\n"
        "read_steps:\n"
        "  - x\n"
        "verify_steps:\n"
        "  - y\n"
    )
    engine = PipelineEngine()
    with pytest.raises(ValueError, match="rollback_or_stop_policy"):
        engine._load_metadata(bad)


def test_load_metadata_rejects_missing_steps(tmp_path):
    from scripts.pipeline_engine import PipelineEngine
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "intent_signature: s\n"
        "rollback_or_stop_policy: stop\n"
        "verify_steps:\n"
        "  - y\n"
    )
    engine = PipelineEngine()
    with pytest.raises(ValueError, match="read_steps.*write_steps"):
        engine._load_metadata(bad)


def test_load_metadata_rejects_both_steps_present(tmp_path):
    from scripts.pipeline_engine import PipelineEngine
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "intent_signature: s\n"
        "rollback_or_stop_policy: stop\n"
        "read_steps:\n"
        "  - x\n"
        "write_steps:\n"
        "  - y\n"
        "verify_steps:\n"
        "  - z\n"
    )
    engine = PipelineEngine()
    with pytest.raises(ValueError, match="read_steps.*write_steps"):
        engine._load_metadata(bad)


def test_load_metadata_rejects_missing_verify_steps(tmp_path):
    from scripts.pipeline_engine import PipelineEngine
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "intent_signature: s\n"
        "rollback_or_stop_policy: stop\n"
        "read_steps:\n"
        "  - x\n"
    )
    engine = PipelineEngine()
    with pytest.raises(ValueError, match="verify_steps"):
        engine._load_metadata(bad)


def test_load_metadata_accepts_valid_metadata(tmp_path):
    from scripts.pipeline_engine import PipelineEngine
    good = tmp_path / "good.yaml"
    good.write_text(
        "intent_signature: read.mock.test\n"
        "rollback_or_stop_policy: stop\n"
        "read_steps:\n"
        "  - x\n"
        "verify_steps:\n"
        "  - y\n"
    )
    engine = PipelineEngine()
    data = engine._load_metadata(good)
    assert data["intent_signature"] == "read.mock.test"


def test_missing_pipeline_raises_pipeline_missing_error(tmp_path):
    from scripts.pipeline_engine import PipelineEngine, PipelineMissingError
    import os
    os.environ["EMERGE_CONNECTOR_ROOT"] = str(tmp_path / "connectors")
    try:
        engine = PipelineEngine()
        with pytest.raises(PipelineMissingError) as exc_info:
            engine.run_read({"connector": "nonexistent", "pipeline": "nope"})
        assert "nonexistent" in str(exc_info.value)
    finally:
        os.environ.pop("EMERGE_CONNECTOR_ROOT", None)


def test_load_metadata_raises_on_non_dict_yaml(tmp_path):
    """A YAML file that is a list (not a dict) must raise ValueError."""
    from scripts.pipeline_engine import PipelineEngine
    bad = tmp_path / "bad.yaml"
    bad.write_text("- not\n- a\n- dict\n")
    with pytest.raises(ValueError) as exc_info:
        PipelineEngine._load_metadata(bad)
    assert "object" in str(exc_info.value).lower() or "invalid" in str(exc_info.value).lower()


def test_load_metadata_rejects_json_style_content(tmp_path):
    """JSON-style content in .yaml is explicitly rejected."""
    from scripts.pipeline_engine import PipelineEngine
    bad = tmp_path / "meta.yaml"
    bad.write_text(
        '{"intent_signature": "zwcad.read.state", "rollback_or_stop_policy": "stop", "read_steps": ["x"], "verify_steps": ["y"]}'
    )
    with pytest.raises(ValueError, match="JSON-style content is not allowed"):
        PipelineEngine._load_metadata(bad)


def test_run_read_rejects_path_traversal_connector():
    from scripts.pipeline_engine import PipelineEngine
    pe = PipelineEngine()
    with pytest.raises(ValueError, match="invalid connector"):
        pe.run_read({"connector": "../../etc", "pipeline": "state"})


def test_run_read_rejects_path_traversal_pipeline():
    from scripts.pipeline_engine import PipelineEngine
    pe = PipelineEngine()
    with pytest.raises(ValueError, match="invalid pipeline"):
        pe.run_read({"connector": "zwcad", "pipeline": "../../../etc/passwd"})


def test_run_write_rejects_path_traversal():
    from scripts.pipeline_engine import PipelineEngine
    pe = PipelineEngine()
    with pytest.raises(ValueError, match="invalid connector"):
        pe.run_write({"connector": "../../etc", "pipeline": "state"})


def test_valid_connector_pipeline_not_rejected():
    """Valid names must not be rejected (they'll fail with PipelineMissingError, not ValueError)."""
    from scripts.pipeline_engine import PipelineEngine, PipelineMissingError
    pe = PipelineEngine()
    # These are valid names — they should get past validation and fail on missing file
    with pytest.raises(PipelineMissingError):
        pe.run_read({"connector": "zwcad", "pipeline": "nonexistent-pipeline"})
    with pytest.raises(PipelineMissingError):
        pe.run_read({"connector": "my-connector", "pipeline": "sub.pipeline.name"})


def test_run_workflow_read_steps(tmp_path):
    """workflow pipeline with read_steps dispatches as read."""
    import os
    conn_dir = tmp_path / "connectors" / "myconn" / "pipelines" / "workflow"
    conn_dir.mkdir(parents=True)
    (conn_dir / "daily.yaml").write_text(
        "intent_signature: myconn.workflow.daily\n"
        "rollback_or_stop_policy: stop\n"
        "read_steps:\n  - run_read\n"
        "verify_steps:\n  - verify_read\n"
    )
    (conn_dir / "daily.py").write_text(
        "def run_read(metadata, args):\n    return [{'count': 1}]\n"
        "def verify_read(metadata, args, rows):\n    return {'ok': bool(rows)}\n"
    )
    os.environ["EMERGE_CONNECTOR_ROOT"] = str(tmp_path / "connectors")
    try:
        from scripts.pipeline_engine import PipelineEngine
        result = PipelineEngine().run_workflow({"connector": "myconn", "pipeline": "daily"})
        assert result["pipeline_id"] == "myconn.workflow.daily"
        assert result["intent_signature"] == "myconn.workflow.daily"
        assert result["rows"] == [{"count": 1}]
        assert result["verification_state"] == "verified"
    finally:
        os.environ.pop("EMERGE_CONNECTOR_ROOT", None)


def test_run_workflow_write_steps(tmp_path):
    """workflow pipeline with write_steps dispatches as write."""
    import os
    conn_dir = tmp_path / "connectors" / "myconn" / "pipelines" / "workflow"
    conn_dir.mkdir(parents=True)
    (conn_dir / "notify.yaml").write_text(
        "intent_signature: myconn.workflow.notify\n"
        "rollback_or_stop_policy: stop\n"
        "write_steps:\n  - run_write\n"
        "verify_steps:\n  - verify_write\n"
    )
    (conn_dir / "notify.py").write_text(
        "def run_write(metadata, args):\n    return {'ok': True, 'sent': True}\n"
        "def verify_write(metadata, args, action_result):\n    return {'ok': action_result.get('ok', False)}\n"
    )
    os.environ["EMERGE_CONNECTOR_ROOT"] = str(tmp_path / "connectors")
    try:
        from scripts.pipeline_engine import PipelineEngine
        result = PipelineEngine().run_workflow({"connector": "myconn", "pipeline": "notify"})
        assert result["pipeline_id"] == "myconn.workflow.notify"
        assert result["intent_signature"] == "myconn.workflow.notify"
        assert result["action_result"]["sent"] is True
        assert result["verification_state"] == "verified"
    finally:
        os.environ.pop("EMERGE_CONNECTOR_ROOT", None)


def test_parse_intent_signature_rejects_unknown_mode():
    """Mode not in read/write/workflow must be rejected."""
    from scripts.pipeline_engine import PipelineEngine
    with pytest.raises(ValueError, match="'read', 'write', or 'workflow'"):
        PipelineEngine._parse_intent_signature("conn.foobar.pipeline")


import os


def test_yaml_only_pipeline_routes_to_scenario_engine(tmp_path):
    """A connector dir with only a .yaml (steps: key) — no .py — uses YAMLScenarioEngine."""
    conn_dir = tmp_path / "myconn" / "pipelines" / "read"
    conn_dir.mkdir(parents=True)
    (conn_dir / "status.yaml").write_text("""
intent_signature: myconn.read.status
rollback_or_stop_policy: stop
steps:
  - name: set-status
    type: derive
    set:
      status: ok
verify:
  - name: ok
    type: derive
    set:
      checked: "true"
""")
    os.environ["EMERGE_CONNECTOR_ROOT"] = str(tmp_path)
    try:
        engine = PipelineEngine(root=tmp_path)
        result = engine.run_read({"connector": "myconn", "pipeline": "status"})
        assert result["verification_state"] == "verified"
        assert result["pipeline_id"] == "myconn.read.status"
    finally:
        os.environ.pop("EMERGE_CONNECTOR_ROOT", None)


def test_yaml_only_write_pipeline_returns_action_result(tmp_path):
    conn_dir = tmp_path / "myconn" / "pipelines" / "write"
    conn_dir.mkdir(parents=True)
    (conn_dir / "reset.yaml").write_text("""
intent_signature: myconn.write.reset
rollback_or_stop_policy: stop
steps:
  - name: flag
    type: derive
    set:
      ok: "true"
      op: reset
verify:
  - name: verify
    type: derive
    set: {}
""")
    os.environ["EMERGE_CONNECTOR_ROOT"] = str(tmp_path)
    try:
        engine = PipelineEngine(root=tmp_path)
        result = engine.run_write({"connector": "myconn", "pipeline": "reset"})
        assert result["verification_state"] == "verified"
        assert result["action_result"]["op"] == "reset"
    finally:
        os.environ.pop("EMERGE_CONNECTOR_ROOT", None)
