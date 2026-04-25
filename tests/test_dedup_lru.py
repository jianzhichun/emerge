from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_lru_set_bounds_size_and_moves_hits_to_end():
    from scripts.daemon_http import _LRUSet

    seen = _LRUSet(maxsize=3)
    seen.add("a")
    seen.add("b")
    seen.add("c")
    assert "a" in seen

    seen.add("d")

    assert "a" in seen
    assert "b" not in seen
    assert "c" in seen
    assert "d" in seen
    assert len(seen) == 3
