from __future__ import annotations
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.distiller import Distiller
from scripts.pattern_detector import PatternSummary


def _summary(intent_sig: str, app: str = "zwcad") -> PatternSummary:
    return PatternSummary(
        machine_ids=["m1"],
        intent_signature=intent_sig,
        occurrences=4,
        window_minutes=19.0,
        detector_signals=["frequency"],
        context_hint={"app": app, "layer": "标注", "samples": ["主卧", "次卧"]},
    )


def test_distiller_returns_intent_signature():
    d = Distiller()
    sig = d.distill(_summary("zwcad.标注"))
    assert isinstance(sig, str)
    assert len(sig) > 0


def test_distiller_normalises_non_ascii(tmp_path):
    d = Distiller()
    sig = d.distill(_summary("zwcad.标注层"))
    # Non-ASCII in the middle segment gets transliterated or replaced
    assert sig.startswith("zwcad.")
    assert all(c.isascii() or c in (".", "_") for c in sig)


def test_distiller_preserves_clean_signature():
    d = Distiller()
    sig = d.distill(_summary("zwcad.annotate.room_labels"))
    assert sig == "zwcad.annotate.room_labels"


def test_distiller_writes_intent_confirmed_event(tmp_path):
    import json
    event_dir = tmp_path / "operator-events" / "m1"
    event_dir.mkdir(parents=True)
    d = Distiller(event_root=tmp_path / "operator-events")
    summary = _summary("zwcad.annotate.room_labels")
    sig = d.distill(summary, confirmed=True)
    events_file = event_dir / "events.jsonl"
    assert events_file.exists()
    lines = [json.loads(l) for l in events_file.read_text().splitlines() if l.strip()]
    assert any(e.get("event_type") == "intent_confirmed" for e in lines)
    confirmed = next(e for e in lines if e.get("event_type") == "intent_confirmed")
    assert confirmed["payload"]["intent_signature"] == sig
