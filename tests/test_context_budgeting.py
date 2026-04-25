from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.state_tracker import (
    LEVEL_CORE_CRITICAL,
    LEVEL_CORE_SECONDARY,
    LEVEL_PERIPHERAL,
    StateTracker,
)


def test_budget_trims_peripheral_then_aggregates_secondary():
    tracker = StateTracker()
    tracker.add_delta("Critical wall length changed", level=LEVEL_CORE_CRITICAL)
    tracker.add_delta("Secondary read detail A", level=LEVEL_CORE_SECONDARY)
    tracker.add_delta("Secondary read detail B", level=LEVEL_CORE_SECONDARY)
    tracker.add_delta("Peripheral debug line", level=LEVEL_PERIPHERAL)
    tracker.add_delta("Peripheral trace line", level=LEVEL_PERIPHERAL)

    full_ctx = tracker.format_context()
    assert "Peripheral debug line" in full_ctx["Delta"]

    trimmed_ctx = tracker.format_context(budget_chars=80)
    assert "Peripheral debug line" not in trimmed_ctx["Delta"]
    assert "Secondary changes: 2 (aggregated)" in trimmed_ctx["Delta"]
    assert "Critical wall length changed" in trimmed_ctx["Delta"]


def test_format_additional_context_respects_total_budget():
    big_deltas = [
        {
            "id": f"d-{i}",
            "level": LEVEL_CORE_CRITICAL,
            "message": "delta " + ("X" * 200),
            "verification_state": "verified",
            "provisional": False,
            "ts_ms": 1000 + i,
        }
        for i in range(20)
    ]
    big_risks = [
        {
            "risk_id": f"r-{i}",
            "text": "risk " + ("Y" * 200),
            "status": "open",
            "created_at_ms": 2000 + i,
        }
        for i in range(20)
    ]
    tracker = StateTracker(
        state={
            "deltas": big_deltas,
            "open_risks": big_risks,
            "verification_state": "verified",
            "consistency_window_ms": 0,
        }
    )

    output = tracker.format_additional_context(budget_chars=500)

    assert len(output) <= 500
    assert "FLYWHEEL_TOKEN" in output
    assert "Delta" in output
