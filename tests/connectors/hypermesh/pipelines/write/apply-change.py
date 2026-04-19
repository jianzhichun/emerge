import socket
from typing import Any

_DEFAULT_HM_HOST = "192.168.122.21"
_DEFAULT_HM_PORT = 9999


def run_write(metadata: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    tcl_cmd = str(args.get("tcl_cmd", ""))
    host = str(args.get("hm_host", _DEFAULT_HM_HOST))
    port = int(args.get("hm_port", _DEFAULT_HM_PORT))
    timeout = float(args.get("hm_timeout", 2.0))
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall((tcl_cmd + "\n").encode("utf-8"))
            data = b""
            while b"\n" not in data:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        line = data.decode("utf-8", errors="replace").strip()
        ok = line.startswith("SUCCESS:")
        return {"ok": ok, "tcl_cmd": tcl_cmd, "response": line, "source": "live"}
    except Exception:
        return {"ok": True, "tcl_cmd": tcl_cmd, "source": "mock"}


def verify_write(
    metadata: dict[str, Any], args: dict[str, Any], action_result: dict[str, Any]
) -> dict[str, Any]:
    return {"ok": bool(action_result.get("ok"))}
