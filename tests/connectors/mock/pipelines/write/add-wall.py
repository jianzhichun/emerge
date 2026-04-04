from __future__ import annotations

from typing import Any


def run_write(metadata: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    length = int(args.get("length", 1000))
    wall_id = args.get("wall_id", "W-new")
    return {"wall_id": wall_id, "length": length, "created": True}


def verify_write(
    metadata: dict[str, Any], args: dict[str, Any], action_result: dict[str, Any]
) -> dict[str, Any]:
    ok = bool(action_result.get("created")) and int(action_result.get("length", 0)) > 0
    return {
        "ok": ok,
        "verified_wall_id": action_result.get("wall_id"),
        "observed_length": action_result.get("length"),
    }
