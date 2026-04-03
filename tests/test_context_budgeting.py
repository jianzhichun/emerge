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
    tracker.set_goal("Reduce token usage")
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
