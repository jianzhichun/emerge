from __future__ import annotations

from typing import Any


def run_write(metadata: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    return {
        "wall_id": args.get("wall_id", "W-rb"),
        "length": int(args.get("length", 1000)),
        "created": True,
    }


def verify_write(
    metadata: dict[str, Any], args: dict[str, Any], action_result: dict[str, Any]
) -> dict[str, Any]:
    return {"ok": False, "reason": "forced_failure_for_rollback"}


def rollback_write(
    metadata: dict[str, Any], args: dict[str, Any], action_result: dict[str, Any]
) -> dict[str, Any]:
    return {"ok": True, "rolled_back_wall_id": action_result.get("wall_id")}
