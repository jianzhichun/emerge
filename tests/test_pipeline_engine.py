from pathlib import Path
import sys


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


def test_run_write_runs_action_and_verify():
    engine = PipelineEngine(root=ROOT)
    result = engine.run_write(
        {"connector": "mock", "pipeline": "add-wall", "wall_id": "W9", "length": 2000}
    )
    assert result["pipeline_id"] == "mock.write.add-wall"
    assert result["action_result"]["wall_id"] == "W9"
    assert result["verify_result"]["ok"] is True
    assert result["verification_state"] == "verified"
