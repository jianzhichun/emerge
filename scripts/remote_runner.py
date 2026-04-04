from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# Ensure plugin root is on sys.path so this script is self-contained
# regardless of how it is launched (no PYTHONPATH required).
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.pipeline_engine import PipelineEngine
from scripts.policy_config import derive_profile_token, derive_session_id, default_repl_root
from scripts.exec_session import ExecSession

ROOT = Path(__file__).resolve().parents[1]
_START_TIME = time.time()
_LOG_FILE = ROOT / ".runner.log"
_LOG_MAX_BYTES = 2 * 1024 * 1024  # 2 MB rolling


def _setup_logging() -> None:
    handler = logging.handlers.RotatingFileHandler(
        _LOG_FILE, maxBytes=_LOG_MAX_BYTES, backupCount=2, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)


class RunnerExecutor:
    def __init__(self, root: Path | None = None, state_root: Path | None = None) -> None:
        resolved_root = root or ROOT
        self._root = resolved_root
        self._state_root = (state_root or default_repl_root()).expanduser().resolve()
        self._base_session_id = derive_session_id(os.environ.get("REPL_SESSION_ID"), resolved_root)
        self._repl_by_profile: dict[str, ExecSession] = {}
        self._repl_lock = threading.Lock()
        self.pipeline = PipelineEngine(root=resolved_root)

    def run(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name == "icc_exec":
            profile = str(arguments.get("target_profile", "default"))
            repl = self._get_repl(profile)
            mode = str(arguments.get("mode", "inline_code"))
            code = self._resolve_exec_code(mode=mode, arguments=arguments)
            return repl.exec_code(
                code,
                metadata={
                    "mode": mode,
                    "target_profile": profile,
                    "intent_signature": arguments.get("intent_signature", ""),
                    "script_ref": arguments.get("script_ref", ""),
                    "no_replay": bool(arguments.get("no_replay", False)),
                },
                inject_vars={"__args": arguments.get("script_args", {})},
            )
        if tool_name == "icc_read":
            result = self.pipeline.run_read(arguments)
            return {"isError": False, "content": [{"type": "text", "text": json.dumps(result)}]}
        if tool_name == "icc_write":
            result = self.pipeline.run_write(arguments)
            return {"isError": False, "content": [{"type": "text", "text": json.dumps(result)}]}
        return {"isError": True, "content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}]}

    def _get_repl(self, target_profile: str) -> ExecSession:
        normalized = (target_profile or "default").strip() or "default"
        profile_key = "__default__" if normalized == "default" else derive_profile_token(normalized)
        if profile_key not in self._repl_by_profile:
            with self._repl_lock:
                if profile_key not in self._repl_by_profile:
                    session_id = (
                        self._base_session_id
                        if normalized == "default"
                        else f"{self._base_session_id}__{profile_key}"
                    )
                    self._repl_by_profile[profile_key] = ExecSession(
                        state_root=self._state_root, session_id=session_id
                    )
        return self._repl_by_profile[profile_key]

    def _resolve_exec_code(self, mode: str, arguments: dict[str, Any]) -> str:
        if mode == "script_ref":
            ref = str(arguments.get("script_ref", "")).strip()
            if not ref:
                raise ValueError("script_ref is required when mode=script_ref")
            script_path = Path(ref)
            if not script_path.is_absolute():
                script_path = (self._root / script_path).resolve()
            else:
                script_path = script_path.resolve()
            return script_path.read_text(encoding="utf-8")
        return str(arguments.get("code", ""))


class RunnerHTTPHandler(BaseHTTPRequestHandler):
    executor: RunnerExecutor

    def _send_json(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/run":
            self._send_json(404, {"ok": False, "error": "not_found"})
            return
        t0 = time.time()
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(content_length).decode("utf-8")
            req = json.loads(raw) if raw else {}
            if not isinstance(req, dict):
                raise ValueError("request must be an object")
            tool_name = str(req.get("tool_name", ""))
            arguments = req.get("arguments", {})
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be an object")
            result = self.executor.run(tool_name=tool_name, arguments=arguments)
            elapsed = round(time.time() - t0, 3)
            logging.info("POST /run tool=%s elapsed=%.3fs ok=True", tool_name, elapsed)
            self._send_json(200, {"ok": True, "result": result})
        except Exception as exc:
            elapsed = round(time.time() - t0, 3)
            logging.warning("POST /run elapsed=%.3fs error=%s", elapsed, exc)
            self._send_json(400, {"ok": False, "error": str(exc)})

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send_json(
                200,
                {
                    "ok": True,
                    "service": "emerge-remote-runner",
                    "status": "ready",
                    "uptime_s": round(time.time() - _START_TIME),
                },
            )
            return
        if self.path == "/status":
            self._send_json(
                200,
                {
                    "ok": True,
                    "service": "emerge-remote-runner",
                    "uptime_s": round(time.time() - _START_TIME),
                    "log_file": str(_LOG_FILE),
                    "pid": os.getpid(),
                    "python": sys.executable,
                    "root": str(ROOT),
                },
            )
            return
        if self.path.startswith("/logs"):
            try:
                n = int(self.path.split("?n=")[-1]) if "?n=" in self.path else 100
                n = min(max(n, 1), 2000)
                lines: list[str] = []
                if _LOG_FILE.exists():
                    with open(_LOG_FILE, encoding="utf-8", errors="replace") as f:
                        lines = f.readlines()
                tail = "".join(lines[-n:])
                body = tail.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
            return
        self._send_json(404, {"ok": False, "error": "not_found"})

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


def run_server(host: str, port: int, *, root: Path | None = None, state_root: Path | None = None) -> None:
    _setup_logging()
    logging.info("emerge-remote-runner starting host=%s port=%d pid=%d", host, port, os.getpid())
    executor = RunnerExecutor(root=root, state_root=state_root)
    handler_cls = type(
        "BoundRunnerHTTPHandler",
        (RunnerHTTPHandler,),
        {"executor": executor},
    )
    server = ThreadingHTTPServer((host, port), handler_cls)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Emerge remote runner")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--state-root", default="")
    args = parser.parse_args()
    state_root = Path(args.state_root).expanduser().resolve() if args.state_root else None
    run_server(args.host, args.port, root=ROOT, state_root=state_root)


if __name__ == "__main__":
    main()
