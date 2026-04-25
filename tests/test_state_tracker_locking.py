from __future__ import annotations

import sys
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_with_locked_tracker_preserves_concurrent_mutations(tmp_path):
    from scripts.state_tracker import with_locked_tracker

    state_path = tmp_path / "state.json"

    def add_delta(name: str) -> None:
        def mutate(tracker):
            time.sleep(0.01)
            tracker.add_delta(message=name, tool_name=name)

        with_locked_tracker(state_path, mutate)

    threads = [
        threading.Thread(target=add_delta, args=("first",)),
        threading.Thread(target=add_delta, args=("second",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    from scripts.state_tracker import load_tracker

    tracker = load_tracker(state_path)
    messages = sorted(delta["message"] for delta in tracker.state["deltas"])
    assert messages == ["first", "second"]
