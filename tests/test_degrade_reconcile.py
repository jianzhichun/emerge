from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.state_tracker import LEVEL_CORE_CRITICAL, StateTracker


def test_mismatch_enters_degraded_and_blocks_auto_chain():
    tracker = StateTracker()
    tracker.add_delta("Write wall", level=LEVEL_CORE_CRITICAL, verification_state="verified")
    tracker.mark_degraded("state/event mismatch")
    assert tracker.to_dict()["verification_state"] == "degraded"
    assert tracker.can_auto_chain_high_risk_write() is False


def test_reconcile_can_recover_when_confirmed():
    tracker = StateTracker()
    delta_id = tracker.add_delta(
        "Provisional write result",
        level=LEVEL_CORE_CRITICAL,
        verification_state="verified",
        provisional=True,
    )
    tracker.reconcile_delta(delta_id, "confirm")
    item = [d for d in tracker.to_dict()["deltas"] if d["id"] == delta_id][0]
    assert item["provisional"] is False
    assert item["reconcile_outcome"] == "confirm"
