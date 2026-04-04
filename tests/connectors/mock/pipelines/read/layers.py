from __future__ import annotations

from typing import Any


def run_read(metadata: dict[str, Any], args: dict[str, Any]) -> list[dict[str, Any]]:
    doc_id = args.get("document_id", "doc-mock")
    return [
        {"id": "L1", "name": "walls", "document_id": doc_id, "count": 2},
        {"id": "L2", "name": "doors", "document_id": doc_id, "count": 1},
    ]


def verify_read(metadata: dict[str, Any], args: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok = bool(rows) and all("id" in row and "name" in row for row in rows)
    return {"ok": ok, "row_count": len(rows)}
