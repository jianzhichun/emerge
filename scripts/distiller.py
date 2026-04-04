from __future__ import annotations

import json
import re
import time
from pathlib import Path

from scripts.pattern_detector import PatternSummary


class Distiller:
    """Converts a PatternSummary into a canonical intent_signature and
    optionally writes an intent_confirmed event to the EventBus."""

    def __init__(self, event_root: Path | None = None) -> None:
        self._event_root = event_root or (Path.home() / ".emerge" / "operator-events")

    def distill(self, summary: PatternSummary, *, confirmed: bool = False) -> str:
        sig = self._normalise(summary.intent_signature)
        if confirmed:
            self._write_confirmed_events(summary, sig)
        return sig

    @staticmethod
    def _normalise(raw: str) -> str:
        """Normalise intent_signature: lowercase, replace spaces+special chars with _,
        keep dots as segment separators, strip non-ASCII via encode/replace."""
        segments = raw.split(".")
        clean: list[str] = []
        for seg in segments:
            original = seg.strip()
            seg = re.sub(r"[\s\-]+", "_", seg)
            # Replace non-ASCII bytes with '_' so each non-ASCII character contributes a placeholder
            ascii_seg = seg.encode("ascii", errors="replace").decode("ascii")
            ascii_seg = re.sub(r"[^\w]", "_", ascii_seg)
            ascii_seg = re.sub(r"_+", "_", ascii_seg).strip("_").lower()
            if not ascii_seg and original:
                # Segment was entirely non-ASCII; use a generic placeholder
                ascii_seg = "x"
            if ascii_seg:
                clean.append(ascii_seg)
        return ".".join(clean) if clean else "unknown.pattern"

    def _write_confirmed_events(self, summary: PatternSummary, sig: str) -> None:
        for machine_id in summary.machine_ids:
            machine_dir = self._event_root / machine_id
            machine_dir.mkdir(parents=True, exist_ok=True)
            event = {
                "ts_ms": int(time.time() * 1000),
                "machine_id": machine_id,
                "session_role": "monitor_sub",
                "event_type": "intent_confirmed",
                "payload": {
                    "intent_signature": sig,
                    "occurrences": summary.occurrences,
                    "detector_signals": summary.detector_signals,
                    "context_hint": summary.context_hint,
                },
            }
            events_path = machine_dir / "events.jsonl"
            with events_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
