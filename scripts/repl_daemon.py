from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.pipeline_engine import PipelineEngine  # noqa: E402
from scripts.repl_state import ReplState  # noqa: E402


class ReplDaemon:
    def __init__(self, root: Path | None = None) -> None:
        self.repl = ReplState()
        self.pipeline = PipelineEngine(root=root)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "icc_exec":
            code = str(arguments.get("code", ""))
            return self.repl.exec_code(code)
        if name == "icc_read":
            result = self.pipeline.run_read(arguments)
            return {"content": [{"type": "text", "text": json.dumps(result)}]}
        if name == "icc_write":
            result = self.pipeline.run_write(arguments)
            return {"content": [{"type": "text", "text": json.dumps(result)}]}
        return {"isError": True, "content": [{"type": "text", "text": f"Unknown tool: {name}"}]}

    def handle_jsonrpc(self, request: dict[str, Any]) -> dict[str, Any]:
        req_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params", {})

        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "tools": [
                        {"name": "icc_exec", "description": "Persistent Python exec"},
                        {"name": "icc_read", "description": "Run read pipeline"},
                        {"name": "icc_write", "description": "Run write pipeline"},
                    ]
                },
            }

        if method == "tools/call":
            name = params.get("name", "")
            arguments = params.get("arguments", {}) or {}
            result = self.call_tool(name, arguments)
            return {"jsonrpc": "2.0", "id": req_id, "result": result}

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }


def run_stdio() -> None:
    daemon = ReplDaemon()
    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue
        try:
            req = json.loads(text)
            resp = daemon.handle_jsonrpc(req)
        except Exception as exc:  # pragma: no cover
            resp = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32603, "message": str(exc)},
            }
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    run_stdio()
