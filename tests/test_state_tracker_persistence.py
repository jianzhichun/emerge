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
        '{"goal": 123, "open_risks": "oops", "deltas": [{"message": 42}, "bad"], "consistency_window_ms":"x"}',
        encoding="utf-8",
    )
    tracker = load_tracker(state_path)
    state = tracker.to_dict()
    assert state["goal"] == "123"
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
