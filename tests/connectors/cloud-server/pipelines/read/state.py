from typing import Any


def run_read(metadata: dict[str, Any], args: dict[str, Any]) -> list[dict[str, Any]]:
    """Read cloud server environment health state.

    Returns mock health rows in test/offline mode.
    """
    return [
        {"id": "env-1", "name": "production", "status": "healthy", "source": "mock"},
        {"id": "env-2", "name": "staging", "status": "healthy", "source": "mock"},
    ]


def verify_read(
    metadata: dict[str, Any], args: dict[str, Any], rows: list[dict[str, Any]]
) -> dict[str, Any]:
    if not rows:
        return {"ok": False, "row_count": 0}
    ok = all("id" in r and "name" in r and "status" in r for r in rows)
    return {"ok": ok, "row_count": len(rows)}
