from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.state_tracker import StateTracker, load_tracker, save_tracker


def test_load_tracker_recovers_from_invalid_json(tmp_path: Path):
    state_path = tmp_path / "state.json"
    state_path.write_text("{bad json", encoding="utf-8")
    tracker = load_tracker(state_path)
    ctx = tracker.format_context()
    assert ctx["Goal"] == "Not set."
    assert ctx["Delta"] == "- No changes."


def test_load_tracker_normalizes_wrong_shapes(tmp_path: Path):
    state_path = tmp_path / "state.json"
    state_path.write_text(
        '{"goal": 123, "goal_source":"legacy","open_risks": "oops", "deltas": [{"message": 42}, "bad"], "consistency_window_ms":"x"}',
        encoding="utf-8",
    )
    tracker = load_tracker(state_path)
    state = tracker.to_dict()
    assert state["goal"] == "123"
    assert state["goal_source"] == "legacy"
    assert state["open_risks"] == []
    assert len(state["deltas"]) == 1
    assert state["deltas"][0]["message"] == "42"
    assert state["consistency_window_ms"] == 0


def test_save_tracker_writes_valid_json_atomically(tmp_path: Path):
    state_path = tmp_path / "state.json"
    tracker = StateTracker()
    tracker.set_goal("atomic")
    save_tracker(state_path, tracker)
    loaded = load_tracker(state_path)
    assert loaded.to_dict()["goal"] == "atomic"


def test_format_recovery_token_includes_schema_and_deltas():
    tracker = StateTracker()
    tracker.set_goal("recover", source="test")
    tracker.add_delta("core update")
    token = tracker.format_recovery_token()
    assert token["schema_version"] == "flywheel.v1"
    assert token["goal"] == "recover"
    assert token["goal_source"] == "test"
    assert token["deltas"]


def test_set_goal_truncates_to_token_budget():
    tracker = StateTracker()
    tracker.set_goal("x" * 300, source="test")
    token = tracker.format_recovery_token()
    assert len(token["goal"]) == 120


def test_format_recovery_token_hard_budget_cap():
    """Token must fit within budget_chars even when all deltas are CORE_CRITICAL."""
    import json
    from scripts.state_tracker import LEVEL_CORE_CRITICAL
    tracker = StateTracker()
    tracker.set_goal("stress test", source="test")
    # Add many large critical deltas — total will far exceed 800 chars
    for i in range(30):
        tracker.add_delta(
            message=f"critical delta {i}: " + "x" * 60,
            level=LEVEL_CORE_CRITICAL,
        )
    budget = 800
    token = tracker.format_recovery_token(budget_chars=budget)
    encoded = json.dumps(token, ensure_ascii=True, separators=(",", ":"))
    assert len(encoded) <= budget, (
        f"Token ({len(encoded)} chars) exceeds budget ({budget} chars)"
    )
    # Must still have schema and goal
    assert token["schema_version"] == "flywheel.v1"
    assert token["goal"] == "stress test"
