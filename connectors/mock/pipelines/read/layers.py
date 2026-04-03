from __future__ import annotations

from typing import Any


def run_read(metadata: dict[str, Any], args: dict[str, Any]) -> list[dict[str, Any]]:
    doc_id = args.get("document_id", "doc-mock")
    return [
        {"id": "L1", "name": "walls", "document_id": doc_id, "count": 2},
        {"id": "L2", "name": "doors", "document_id": doc_id, "count": 1},
    ]
