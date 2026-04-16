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
    assert ctx["Delta"] == "- No changes."


def test_load_tracker_normalizes_wrong_shapes(tmp_path: Path):
    state_path = tmp_path / "state.json"
    state_path.write_text(
        '{"open_risks": "oops", "deltas": [{"message": 42}, "bad"], "consistency_window_ms":"x"}',
        encoding="utf-8",
    )
    tracker = load_tracker(state_path)
    state = tracker.to_dict()
    assert state["open_risks"] == []
    assert len(state["deltas"]) == 1
    assert state["deltas"][0]["message"] == "42"
    assert state["consistency_window_ms"] == 0


def test_save_tracker_writes_valid_json_atomically(tmp_path: Path):
    state_path = tmp_path / "state.json"
    tracker = StateTracker()
    tracker.add_delta("atomic delta")
    save_tracker(state_path, tracker)
    loaded = load_tracker(state_path)
    assert loaded.to_dict()["deltas"][0]["message"] == "atomic delta"


def test_format_recovery_token_includes_schema_and_deltas():
    tracker = StateTracker()
    tracker.add_delta("core update")
    token = tracker.format_recovery_token()
    assert token["schema_version"] == "flywheel.v1"
    assert token["deltas"]


def test_format_recovery_token_hard_budget_cap():
    """Token must fit within budget_chars even when all deltas are CORE_CRITICAL."""
    import json
    from scripts.state_tracker import LEVEL_CORE_CRITICAL
    tracker = StateTracker()
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
    assert token["schema_version"] == "flywheel.v1"


def test_add_risk_deduplicates_exact_duplicates():
    """add_risk must not add the same risk string twice."""
    from scripts.state_tracker import StateTracker
    tracker = StateTracker()
    tracker.add_risk("pipeline verification failed: zwcad.write.apply-change")
    tracker.add_risk("pipeline verification failed: zwcad.write.apply-change")
    tracker.add_risk("pipeline verification failed: zwcad.write.apply-change")
    assert len(tracker.state["open_risks"]) == 1, (
        "duplicate risk entries must be suppressed"
    )


def test_add_risk_keeps_distinct_risks():
    """Different risk strings must all be preserved."""
    from scripts.state_tracker import StateTracker
    tracker = StateTracker()
    tracker.add_risk("pipeline verification failed: mock.read.state")
    tracker.add_risk("pipeline verification failed: mock.write.apply-change")
    tracker.add_risk("runner unreachable: mycader-1")
    assert len(tracker.state["open_risks"]) == 3
