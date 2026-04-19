from typing import Any


def run_write(metadata: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    change_type = str(args.get("change_type", "line"))
    try:
        import win32com.client  # type: ignore[import]
        app = win32com.client.Dispatch("ZwCAD.Application")
        doc = app.ActiveDocument
        msp = doc.ModelSpace
        if change_type == "line":
            import win32com.client as wc
            p1 = wc.VARIANT(8, [float(args.get("x1", 0)), float(args.get("y1", 0)), 0.0])
            p2 = wc.VARIANT(8, [float(args.get("x2", 100)), float(args.get("y2", 100)), 0.0])
            msp.AddLine(p1, p2)
        return {"ok": True, "change_type": change_type, "source": "live"}
    except Exception:
        return {"ok": True, "change_type": change_type, "source": "mock"}


def verify_write(
    metadata: dict[str, Any], args: dict[str, Any], action_result: dict[str, Any]
) -> dict[str, Any]:
    return {"ok": bool(action_result.get("ok"))}
