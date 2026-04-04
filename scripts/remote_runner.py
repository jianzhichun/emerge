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

from scripts.pipeline_engine import PipelineEngine, PipelineMissingError
from scripts.policy_config import derive_profile_token, derive_session_id, default_exec_root
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
        self._state_root = (state_root or default_exec_root()).expanduser().resolve()
        self._base_session_id = derive_session_id(os.environ.get("EMERGE_SESSION_ID"), resolved_root)
        self._sessions_by_profile: dict[str, ExecSession] = {}
        self._repl_lock = threading.Lock()
        self.pipeline = PipelineEngine(root=resolved_root)

    def run(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name == "icc_exec":
            profile = str(arguments.get("target_profile", "default"))
            repl = self._get_session(profile)
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
            try:
                result = self.pipeline.run_read(arguments)
            except PipelineMissingError as exc:
                info = {"pipeline_missing": True, "connector": exc.connector, "mode": exc.mode, "pipeline": exc.pipeline}
                return {"isError": False, "pipeline_missing": True, "connector": exc.connector, "mode": exc.mode, "pipeline": exc.pipeline, "content": [{"type": "text", "text": json.dumps(info)}]}
            return {"isError": False, "content": [{"type": "text", "text": json.dumps(result)}]}
        if tool_name == "icc_write":
            try:
                result = self.pipeline.run_write(arguments)
            except PipelineMissingError as exc:
                info = {"pipeline_missing": True, "connector": exc.connector, "mode": exc.mode, "pipeline": exc.pipeline}
                return {"isError": False, "pipeline_missing": True, "connector": exc.connector, "mode": exc.mode, "pipeline": exc.pipeline, "content": [{"type": "text", "text": json.dumps(info)}]}
            return {"isError": False, "content": [{"type": "text", "text": json.dumps(result)}]}
        if tool_name == "icc_crystallize":
            intent_signature = str(arguments.get("intent_signature", "")).strip()
            connector = str(arguments.get("connector", "")).strip()
            pipeline_name = str(arguments.get("pipeline_name", "")).strip()
            mode = str(arguments.get("mode", "read")).strip()
            target_profile = str(arguments.get("target_profile", "default")).strip()
            return self._crystallize(
                intent_signature=intent_signature,
                connector=connector,
                pipeline_name=pipeline_name,
                mode=mode,
                target_profile=target_profile,
            )
        return {"isError": True, "content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}]}

    def _get_session(self, target_profile: str) -> ExecSession:
        normalized = (target_profile or "default").strip() or "default"
        profile_key = "__default__" if normalized == "default" else derive_profile_token(normalized)
        if profile_key not in self._sessions_by_profile:
            with self._repl_lock:
                if profile_key not in self._sessions_by_profile:
                    session_id = (
                        self._base_session_id
                        if normalized == "default"
                        else f"{self._base_session_id}__{profile_key}"
                    )
                    self._sessions_by_profile[profile_key] = ExecSession(
                        state_root=self._state_root, session_id=session_id
                    )
        return self._sessions_by_profile[profile_key]

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

    def _crystallize(
        self,
        *,
        intent_signature: str,
        connector: str,
        pipeline_name: str,
        mode: str,
        target_profile: str = "default",
    ) -> dict[str, Any]:
        import textwrap
        import time as _time

        from scripts.pipeline_engine import _USER_CONNECTOR_ROOT

        # Locate WAL for this profile (same logic as _get_session)
        normalized = (target_profile or "default").strip() or "default"
        profile_key = "__default__" if normalized == "default" else derive_profile_token(normalized)
        session_id = (
            self._base_session_id
            if normalized == "default"
            else f"{self._base_session_id}__{profile_key}"
        )
        wal_path = self._state_root / session_id / "wal.jsonl"

        best_code: str | None = None
        if wal_path.exists():
            with wal_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if (
                        entry.get("status") == "success"
                        and not entry.get("no_replay", False)
                        and entry.get("metadata", {}).get("intent_signature") == intent_signature
                    ):
                        best_code = str(entry.get("code", "")).strip()

        if not best_code:
            return {
                "isError": True,
                "content": [{"type": "text", "text": (
                    f"icc_crystallize: no synthesizable WAL entry found for "
                    f"intent_signature='{intent_signature}'. Run icc_exec with "
                    f"intent_signature='{intent_signature}' and no_replay=false first."
                )}],
            }

        ts = int(_time.time())
        indented = textwrap.indent(best_code, "    ")

        if mode == "read":
            py_src = (
                f"# auto-generated by icc_crystallize — review before promoting\n"
                f"# intent_signature: {intent_signature}\n"
                f"# synthesized_at: {ts}\n"
                f"\n"
                f"def run_read(metadata, args):\n"
                f"    __args = args  # compat with exec __args scope\n"
                f"    # --- CRYSTALLIZED ---\n"
                f"{indented}\n"
                f"    # --- END ---\n"
                f"    return __result  # exec code must set __result = [{{...}}]\n"
                f"\n"
                f"\n"
                f"def verify_read(metadata, args, rows):\n"
                f"    return {{\"ok\": bool(rows)}}\n"
            )
            yaml_src = (
                f"intent_signature: {intent_signature}\n"
                f"rollback_or_stop_policy: stop\n"
                f"read_steps:\n"
                f"  - run_read\n"
                f"verify_steps:\n"
                f"  - verify_read\n"
                f"synthesized: true\n"
                f"synthesized_at: {ts}\n"
            )
        else:  # write
            py_src = (
                f"# auto-generated by icc_crystallize — review before promoting\n"
                f"# intent_signature: {intent_signature}\n"
                f"# synthesized_at: {ts}\n"
                f"\n"
                f"def run_write(metadata, args):\n"
                f"    __args = args  # compat with exec __args scope\n"
                f"    # --- CRYSTALLIZED ---\n"
                f"{indented}\n"
                f"    # --- END ---\n"
                f"    return __action  # exec code must set __action = {{\"ok\": True, ...}}\n"
                f"\n"
                f"\n"
                f"def verify_write(metadata, args, action_result):\n"
                f"    return {{\"ok\": bool(action_result.get(\"ok\"))}}\n"
            )
            yaml_src = (
                f"intent_signature: {intent_signature}\n"
                f"rollback_or_stop_policy: stop\n"
                f"write_steps:\n"
                f"  - run_write\n"
                f"verify_steps:\n"
                f"  - verify_write\n"
                f"synthesized: true\n"
                f"synthesized_at: {ts}\n"
            )

        env_root_str = os.environ.get("EMERGE_CONNECTOR_ROOT", "").strip()
        target_root = Path(env_root_str).expanduser() if env_root_str else _USER_CONNECTOR_ROOT
        pipeline_dir = target_root / connector / "pipelines" / mode
        pipeline_dir.mkdir(parents=True, exist_ok=True)

        py_path = pipeline_dir / f"{pipeline_name}.py"
        yaml_path = pipeline_dir / f"{pipeline_name}.yaml"
        py_path.write_text(py_src, encoding="utf-8")
        yaml_path.write_text(yaml_src, encoding="utf-8")

        preview_lines = py_src.splitlines()[:20]
        return {
            "ok": True,
            "py_path": str(py_path),
            "yaml_path": str(yaml_path),
            "code_preview": "\n".join(preview_lines),
            "content": [{"type": "text", "text": json.dumps({
                "ok": True,
                "py_path": str(py_path),
                "yaml_path": str(yaml_path),
            })}],
        }


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
