from __future__ import annotations

import json
import sys
from typing import Any


def read_json_payload() -> dict[str, Any]:
    """Read a JSON object from stdin, returning empty dict on malformed input."""
    payload_text = sys.stdin.read().strip()
    try:
        payload = json.loads(payload_text) if payload_text else {}
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}
