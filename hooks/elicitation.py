from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _ci_mode() -> bool:
    return os.environ.get("EMERGE_CI", "").strip() in ("1", "true", "yes")


def _auto_response(message: str, schema: dict) -> dict | None:
    """Return auto-response content for known emerge elicitation patterns, or None."""
    msg_lower = message.lower()
    props = schema.get("properties", {}) if isinstance(schema, dict) else {}

    # icc_span_approve: "activate pipeline ..." → {confirmed: true}
    if "activate pipeline" in msg_lower and "confirmed" in props:
        return {"confirmed": True}

    # icc_reconcile: "choose the reconciliation outcome ..." → {outcome: "confirm"}
    if "reconciliation outcome" in msg_lower and "outcome" in props:
        # Allow override via env var for CI pipelines that need a specific outcome
        outcome = os.environ.get("EMERGE_CI_RECONCILE_OUTCOME", "confirm").strip()
        if outcome not in ("confirm", "correct", "retract"):
            outcome = "confirm"
        return {"outcome": outcome}

    # icc_hub resolve: "choose the resolution strategy ..." → {resolution: "ours"}
    if "resolution strategy" in msg_lower and "resolution" in props:
        resolution = os.environ.get("EMERGE_CI_HUB_RESOLUTION", "ours").strip()
        if resolution not in ("ours", "theirs", "skip"):
            resolution = "ours"
        return {"resolution": resolution}

    return None


def main() -> None:
    payload_text = sys.stdin.read().strip()
    try:
        payload = json.loads(payload_text) if payload_text else {}
    except Exception:
        payload = {}

    # Only intercept emerge elicitations
    mcp_server = str(payload.get("mcp_server_name", "") or "")
    if "emerge" not in mcp_server:
        print(json.dumps({}))
        return

    # Only auto-respond in CI mode
    if not _ci_mode():
        print(json.dumps({}))
        return

    message = str(payload.get("message", "") or "")
    schema = payload.get("requested_schema") or {}
    content = _auto_response(message, schema)

    if content is None:
        # Unknown elicitation pattern in CI — decline rather than silently skip
        out = {
            "hookSpecificOutput": {
                "hookEventName": "Elicitation",
                "action": "decline",
                "content": {},
            }
        }
    else:
        out = {
            "hookSpecificOutput": {
                "hookEventName": "Elicitation",
                "action": "accept",
                "content": content,
            }
        }

    print(json.dumps(out))


if __name__ == "__main__":
    main()
