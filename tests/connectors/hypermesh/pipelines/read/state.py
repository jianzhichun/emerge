from __future__ import annotations

import socket
from typing import Any


_DEFAULT_HM_HOST = "192.168.122.21"
_DEFAULT_HM_PORT = 9999
_DEFAULT_TIMEOUT = 2.0


def run_read(metadata: dict[str, Any], args: dict[str, Any]) -> list[dict[str, Any]]:
    """Read active model state from HyperMesh via TCP/Tcl socket bridge.

    Connects to the HyperMesh Tcl socket server (hm_tcl_server.tcl) listening
    on port 9999 on the remote VM. Protocol: send one Tcl line, receive
    "SUCCESS: <result>" or "ERROR: <msg>".

    Falls back to a mock snapshot (source="mock") when unreachable.
    verify_read will reject mock-source rows so the bridge records a
    verification failure rather than a false success.
    """
    host = str(args.get("hm_host", _DEFAULT_HM_HOST))
    port = int(args.get("hm_port", _DEFAULT_HM_PORT))
    timeout = float(args.get("hm_timeout", _DEFAULT_TIMEOUT))
    model_name = str(args.get("model_name", "hm-model-1"))

    try:
        rows = _query_via_tcl(host, port, timeout, model_name)
        if rows:
            return rows
        return _mock_rows(model_name)
    except Exception:
        return _mock_rows(model_name)


def _query_via_tcl(
    host: str, port: int, timeout: float, model_name: str
) -> list[dict[str, Any]]:
    batch_cmd = (
        "set _n [hm_getentitydisplaycount nodes]; "
        "set _e [hm_getentitydisplaycount elements]; "
        "set _c [hm_getentitydisplaycount comps]; "
        "list $_n $_e $_c"
    )
    raw = _tcl_call(host, port, timeout, batch_cmd)
    parts = raw.strip().split()
    node_count = _parse_int(parts[0]) if len(parts) > 0 else 0
    elem_count = _parse_int(parts[1]) if len(parts) > 1 else 0
    comp_count = _parse_int(parts[2]) if len(parts) > 2 else 0

    return [
        {
            "model_name": model_name,
            "node_count": node_count,
            "element_count": elem_count,
            "component_count": comp_count,
            "source": "live",
        }
    ]


def _tcl_call(host: str, port: int, timeout: float, cmd: str) -> str:
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall((cmd + "\n").encode("utf-8"))
        data = b""
        while b"\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
    line = data.decode("utf-8", errors="replace").strip()
    if line.startswith("SUCCESS: "):
        return line[len("SUCCESS: "):]
    if line.startswith("ERROR: "):
        raise RuntimeError(line[len("ERROR: "):])
    return line


def _parse_int(s: str) -> int:
    try:
        return int(s.strip())
    except (ValueError, AttributeError):
        return 0


def _mock_rows(model_name: str) -> list[dict[str, Any]]:
    return [
        {
            "model_name": model_name,
            "node_count": 0,
            "element_count": 0,
            "component_count": 0,
            "source": "mock",
        }
    ]


def verify_read(
    metadata: dict[str, Any], args: dict[str, Any], rows: list[dict[str, Any]]
) -> dict[str, Any]:
    if not rows:
        return {"ok": False, "row_count": 0}
    if all(r.get("source") == "mock" for r in rows):
        return {"ok": False, "row_count": len(rows), "why": "mock_fallback"}
    ok = all("node_count" in r and "element_count" in r for r in rows)
    return {"ok": ok, "row_count": len(rows)}
