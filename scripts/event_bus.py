from __future__ import annotations

import json
import socket
import time
from pathlib import Path
from typing import Any


def emit_event(event: dict[str, Any]) -> None:
    """Write an operator event to the local EventBus.

    The event is appended to:
      ~/.emerge/operator-events/<machine_id>/events.jsonl

    `ts_ms` and `machine_id` are injected when missing.
    Caller-provided values win.
    """
    machine_id = socket.gethostname()
    event_dir = Path.home() / ".emerge" / "operator-events" / machine_id
    event_dir.mkdir(parents=True, exist_ok=True)
    event_path = event_dir / "events.jsonl"
    payload: dict[str, Any] = {
        "ts_ms": int(time.time() * 1000),
        "machine_id": machine_id,
    }
    payload.update(event)
    with event_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=True) + "\n")
