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
        '{"intent_signature":"write.demo.missing-verify","write_steps":["a"],"verify_steps":["b"],"rollback_or_stop_policy":"stop"}',
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
        '{"intent_signature":"write.demo.rollback-missing","write_steps":["a"],"verify_steps":["b"],"rollback_or_stop_policy":"rollback"}',
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
    bad.write_text('{"rollback_or_stop_policy": "stop", "read_steps": ["x"], "verify_steps": ["y"]}')
    engine = PipelineEngine()
    with pytest.raises(ValueError, match="intent_signature"):
        engine._load_metadata(bad)


def test_load_metadata_rejects_invalid_policy(tmp_path):
    from scripts.pipeline_engine import PipelineEngine
    bad = tmp_path / "bad.yaml"
    bad.write_text('{"intent_signature": "s", "rollback_or_stop_policy": "unknown", "read_steps": ["x"], "verify_steps": ["y"]}')
    engine = PipelineEngine()
    with pytest.raises(ValueError, match="rollback_or_stop_policy"):
        engine._load_metadata(bad)


def test_load_metadata_rejects_missing_steps(tmp_path):
    from scripts.pipeline_engine import PipelineEngine
    bad = tmp_path / "bad.yaml"
    bad.write_text('{"intent_signature": "s", "rollback_or_stop_policy": "stop", "verify_steps": ["y"]}')
    engine = PipelineEngine()
    with pytest.raises(ValueError, match="read_steps.*write_steps"):
        engine._load_metadata(bad)


def test_load_metadata_rejects_both_steps_present(tmp_path):
    from scripts.pipeline_engine import PipelineEngine
    bad = tmp_path / "bad.yaml"
    bad.write_text('{"intent_signature": "s", "rollback_or_stop_policy": "stop", "read_steps": ["x"], "write_steps": ["y"], "verify_steps": ["z"]}')
    engine = PipelineEngine()
    with pytest.raises(ValueError, match="read_steps.*write_steps"):
        engine._load_metadata(bad)


def test_load_metadata_rejects_missing_verify_steps(tmp_path):
    from scripts.pipeline_engine import PipelineEngine
    bad = tmp_path / "bad.yaml"
    bad.write_text('{"intent_signature": "s", "rollback_or_stop_policy": "stop", "read_steps": ["x"]}')
    engine = PipelineEngine()
    with pytest.raises(ValueError, match="verify_steps"):
        engine._load_metadata(bad)


def test_load_metadata_accepts_valid_metadata(tmp_path):
    from scripts.pipeline_engine import PipelineEngine
    good = tmp_path / "good.yaml"
    good.write_text('{"intent_signature": "read.mock.test", "rollback_or_stop_policy": "stop", "read_steps": ["x"], "verify_steps": ["y"]}')
    engine = PipelineEngine()
    data = engine._load_metadata(good)
    assert data["intent_signature"] == "read.mock.test"
