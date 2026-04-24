from pathlib import Path
import sys
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.pipeline_yaml_engine import YAMLScenarioEngine, YAMLStepError


# ── derive / transform ──────────────────────────────────────────────────────

def test_derive_sets_context_vars():
    engine = YAMLScenarioEngine()
    scenario = {
        "steps": [
            {"name": "set-vals", "type": "derive", "set": {"greeting": "hello", "count": "42"}}
        ]
    }
    result = engine.execute(scenario, {}, mode="write")
    assert result["action_result"]["greeting"] == "hello"
    assert result["action_result"]["count"] == "42"


def test_derive_interpolates_template():
    engine = YAMLScenarioEngine()
    scenario = {
        "steps": [
            {"name": "greet", "type": "derive", "set": {"msg": "hello {{ name }}"}}
        ]
    }
    result = engine.execute(scenario, {"name": "world"}, mode="write")
    assert result["action_result"]["msg"] == "hello world"


def test_transform_maps_fields():
    engine = YAMLScenarioEngine()
    scenario = {
        "steps": [
            {"name": "remap", "type": "transform", "mapping": {"doc_id": "{{ source_id }}"}}
        ]
    }
    result = engine.execute(scenario, {"source_id": "abc123"}, mode="write")
    assert result["action_result"]["doc_id"] == "abc123"


# ── branch ──────────────────────────────────────────────────────────────────

def test_branch_takes_when_path_on_true():
    engine = YAMLScenarioEngine()
    scenario = {
        "steps": [
            {
                "name": "decide",
                "type": "branch",
                "condition": "{{ count | int > 10 }}",
                "when": [
                    {"name": "big", "type": "derive", "set": {"density": "coarse"}}
                ],
                "otherwise": [
                    {"name": "small", "type": "derive", "set": {"density": "fine"}}
                ],
            }
        ]
    }
    result = engine.execute(scenario, {"count": "100"}, mode="write")
    assert result["action_result"]["density"] == "coarse"


def test_branch_takes_otherwise_path_on_false():
    engine = YAMLScenarioEngine()
    scenario = {
        "steps": [
            {
                "name": "decide",
                "type": "branch",
                "condition": "{{ count | int > 10 }}",
                "when": [
                    {"name": "big", "type": "derive", "set": {"density": "coarse"}}
                ],
                "otherwise": [
                    {"name": "small", "type": "derive", "set": {"density": "fine"}}
                ],
            }
        ]
    }
    result = engine.execute(scenario, {"count": "5"}, mode="write")
    assert result["action_result"]["density"] == "fine"


def test_branch_raises_on_non_comparison():
    engine = YAMLScenarioEngine()
    scenario = {
        "steps": [
            {"name": "bad", "type": "branch", "condition": "{{ x + y }}", "when": [], "otherwise": []}
        ]
    }
    with pytest.raises(YAMLStepError, match="comparison"):
        engine.execute(scenario, {"x": "1", "y": "2"}, mode="write")


# ── verify section ───────────────────────────────────────────────────────────

def test_verify_section_runs_after_steps():
    engine = YAMLScenarioEngine()
    scenario = {
        "steps": [
            {"name": "set", "type": "derive", "set": {"status": "done"}}
        ],
        "verify": [
            {"name": "check", "type": "derive", "set": {"verified": "yes"}}
        ],
    }
    result = engine.execute(scenario, {}, mode="write")
    assert result["verify_result"]["ok"] is True


def test_verify_failure_sets_ok_false():
    engine = YAMLScenarioEngine()
    scenario = {
        "steps": [],
        "verify": [
            {"name": "fail-step", "type": "cli", "run": "false"}
        ],
    }
    result = engine.execute(scenario, {}, mode="write")
    assert result["verify_result"]["ok"] is False


# ── read mode ────────────────────────────────────────────────────────────────

def test_execute_read_mode_returns_rows():
    engine = YAMLScenarioEngine()
    scenario = {
        "steps": [
            {"name": "seed", "type": "derive", "set": {"__rows": "not-a-list"}}
        ]
    }
    result = engine.execute(scenario, {}, mode="read")
    assert "rows" in result
    assert isinstance(result["rows"], list)


# ── unknown step type ────────────────────────────────────────────────────────

def test_unknown_step_type_raises():
    engine = YAMLScenarioEngine()
    scenario = {"steps": [{"name": "x", "type": "magic_step"}]}
    with pytest.raises(YAMLStepError, match="Unknown step type"):
        engine.execute(scenario, {}, mode="write")


# ── cli step ─────────────────────────────────────────────────────────────────

def test_cli_step_succeeds_on_zero_exit():
    engine = YAMLScenarioEngine()
    scenario = {"steps": [{"name": "ok", "type": "cli", "run": "true"}]}
    result = engine.execute(scenario, {}, mode="write")
    assert result["verify_result"]["ok"] is True


def test_cli_step_raises_on_nonzero_exit():
    engine = YAMLScenarioEngine()
    scenario = {"steps": [{"name": "fail", "type": "cli", "run": "false"}]}
    with pytest.raises(YAMLStepError, match="failed"):
        engine.execute(scenario, {}, mode="write")


def test_cli_step_extracts_stdout():
    engine = YAMLScenarioEngine()
    scenario = {
        "steps": [
            {
                "name": "echo-val",
                "type": "cli",
                "run": "echo hello",
                "extract_stdout": "output",
            }
        ]
    }
    result = engine.execute(scenario, {}, mode="write")
    assert result["action_result"]["output"] == "hello"
