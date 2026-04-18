from __future__ import annotations

from typing import Any


def run_read(metadata: dict[str, Any], args: dict[str, Any]) -> list[dict[str, Any]]:
    """Read active document state from ZWCAD via COM automation.

    Falls back to a mock snapshot (source="mock") when win32com is unavailable.
    verify_read will reject mock-source rows so the bridge records a
    verification failure rather than a false success.
    """
    doc_id = args.get("document_id", "zwcad-doc-1")
    try:
        import win32com.client  # type: ignore[import]

        app = win32com.client.Dispatch("ZwCAD.Application")
        doc = app.ActiveDocument
        rows = []
        for i, layer in enumerate(doc.Layers):
            rows.append(
                {
                    "id": f"L{i}",
                    "name": layer.Name,
                    "document_id": doc.Name,
                    "on": layer.LayerOn,
                    "source": "live",
                }
            )
        return rows if rows else _mock_rows(doc_id)
    except Exception:
        return _mock_rows(doc_id)


def _mock_rows(doc_id: str) -> list[dict[str, Any]]:
    return [
        {"id": "L0", "name": "0", "document_id": doc_id, "on": True, "source": "mock"},
        {"id": "L1", "name": "Defpoints", "document_id": doc_id, "on": True, "source": "mock"},
    ]


def verify_read(
    metadata: dict[str, Any], args: dict[str, Any], rows: list[dict[str, Any]]
) -> dict[str, Any]:
    if not rows:
        return {"ok": False, "row_count": 0}
    if all(r.get("source") == "mock" for r in rows):
        return {"ok": False, "row_count": len(rows), "why": "mock_fallback"}
    ok = all("id" in r and "name" in r for r in rows)
    return {"ok": ok, "row_count": len(rows)}
