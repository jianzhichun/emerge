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


def _validate_machine_id(machine_id: str) -> None:
    """Reject machine_id values that could escape the event root via path traversal."""
    if not machine_id or machine_id != machine_id.strip():
        raise ValueError("machine_id is required and must not have leading/trailing whitespace")
    p = Path(machine_id)
    if p.name != machine_id or ".." in machine_id or "/" in machine_id or "\\" in machine_id:
        raise ValueError(f"Invalid machine_id: {machine_id!r}")


class RunnerExecutor:
    def __init__(self, root: Path | None = None, state_root: Path | None = None, runner_config_path: Path | None = None) -> None:
        resolved_root = root or ROOT
        self._root = resolved_root
        self._script_roots = self._resolve_script_roots()
        self._state_root = (state_root or default_exec_root()).expanduser().resolve()
        self._base_session_id = derive_session_id(os.environ.get("EMERGE_SESSION_ID"), resolved_root)
        self._sessions_by_profile: dict[str, ExecSession] = {}
        self._repl_lock = threading.Lock()
        self._event_write_lock = threading.Lock()
        self._event_root = self._state_root.parent / "operator-events"

        # Team lead config (optional — runners in agents-team mode)
        import json as _json
        cfg_path = runner_config_path or (Path.home() / ".emerge" / "runner-config.json")
        self._team_lead_url: str = ""
        self._runner_profile: str = ""
        try:
            data = _json.loads(cfg_path.read_text(encoding="utf-8"))
            self._team_lead_url = str(data.get("team_lead_url", "")).rstrip("/")
            self._runner_profile = str(data.get("runner_profile", "")).strip()
        except (OSError, ValueError):
            pass

    def write_operator_event(self, event: dict) -> None:
        machine_id = str(event.get("machine_id", "")).strip()
        _validate_machine_id(machine_id)
        machine_dir = self._event_root / machine_id
        machine_dir.mkdir(parents=True, exist_ok=True)
        events_path = machine_dir / "events.jsonl"
        with self._event_write_lock:
            with events_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")

        # Forward to team lead daemon (fire-and-forget, non-blocking)
        if self._team_lead_url and self._runner_profile:
            threading.Thread(
                target=self._forward_event_to_daemon,
                args=(event,),
                daemon=True,
            ).start()

    def _forward_event_to_daemon(self, event: dict) -> bool:
        """Forward event to team lead daemon. Best-effort, never blocks operator.

        Returns True on success, False on connection failure.
        """
        import urllib.request as _ur
        import urllib.error as _ue
        import json as _j
        url = f"{self._team_lead_url}/runner/event"
        payload = {**event, "runner_profile": self._runner_profile}
        body = _j.dumps(payload, ensure_ascii=True).encode()
        req = _ur.Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            with _ur.urlopen(req, timeout=3):
                pass
            return True
        except (_ue.URLError, OSError):
            return False  # best-effort, never block operator

    def _post_operator_message(self, text: str) -> None:
        """Forward operator tray message to daemon as an operator_message event.

        Shows a non-blocking error toast if the daemon is unreachable.
        """
        import socket as _sock
        import time as _time
        try:
            machine_id = _sock.gethostname()
        except OSError:
            machine_id = "unknown"
        event = {
            "type": "operator_message",
            "text": text,
            "profile": self._runner_profile,
            "machine_id": machine_id,
            "ts_ms": int(_time.time() * 1000),
        }
        ok = bool(
            self._team_lead_url
            and self._runner_profile
            and self._forward_event_to_daemon(event)
        )
        if not ok:
            try:
                from scripts.operator_popup import show_notify
                show_notify({"type": "toast", "body": "发送失败，daemon 未连接", "timeout_s": 4})
            except (ImportError, OSError):
                pass  # headless or module-unavailable — skip feedback silently

    def _start_tray(self) -> None:
        """Start system tray icon in a background thread.

        No-op if:
        - pystray or Pillow are not installed (logs a warning), or
        - no team-lead URL is configured (tray would be non-functional).
        """
        if not self._team_lead_url:
            return
        try:
            import pystray
            from PIL import Image, ImageDraw
        except ImportError:
            logging.warning("pystray/Pillow not installed — tray icon disabled")
            return
        img = Image.new("RGB", (64, 64), color=(30, 30, 30))
        try:
            draw = ImageDraw.Draw(img)
            draw.text((20, 16), "E", fill=(255, 255, 255))
        except Exception:
            pass  # proceed with plain solid image

        def _on_send_message(icon: Any, item: Any) -> None:
            from scripts.operator_popup import show_input_bubble
            threading.Thread(
                target=show_input_bubble,
                args=(self._post_operator_message,),
                daemon=True,
            ).start()

        menu = pystray.Menu(
            pystray.MenuItem("发送消息", _on_send_message),
            pystray.MenuItem("退出", lambda icon, item: icon.stop()),
        )
        icon = pystray.Icon("emerge", img, "emerge runner", menu)
        try:
            icon.run_detached()
        except (NotImplementedError, AttributeError):
            # run_detached() not available on this backend — fall back to daemon thread
            threading.Thread(target=icon.run, daemon=True, name="EmergeTrayIcon").start()

    def show_notify(self, params: dict) -> dict:
        """Show OS-native notification dialog. Blocks until user responds.

        Expects params = {"ui_spec": {...}}. Passes ui_spec to show_notify.
        """
        from scripts.operator_popup import show_notify
        ui_spec = params.get("ui_spec", {})
        if not isinstance(ui_spec, dict):
            ui_spec = {}
        return show_notify(ui_spec)

    def read_operator_events(self, machine_id: str, since_ms: int = 0, limit: int = 200) -> list[dict]:
        _validate_machine_id(machine_id)
        machine_dir = self._event_root / machine_id
        if not machine_dir.exists():
            return []
        events_path = machine_dir / "events.jsonl"
        if not events_path.exists():
            return []
        results = []
        with events_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("ts_ms", 0) > since_ms:
                    results.append(e)
        return results[-limit:]

    def run(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute a tool request. The runner only handles icc_exec — pipeline
        operations (icc_read/icc_write) and icc_crystallize are handled by the
        daemon using locally-loaded connector assets sent as inline code."""
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
                result_var=str(arguments.get("result_var", "")).strip() or None,
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
            if not self._is_allowed_script_path(script_path):
                raise PermissionError(
                    f"script_ref path is outside allowed roots: {script_path}"
                )
            return script_path.read_text(encoding="utf-8")
        return str(arguments.get("code", ""))

    def _resolve_script_roots(self) -> list[Path]:
        raw = os.environ.get("EMERGE_SCRIPT_ROOTS", "").strip()
        if raw:
            return [Path(p).expanduser().resolve() for p in raw.split(",") if p.strip()]
        return [
            (self._root / "connectors").resolve(),
            (Path.home() / ".emerge" / "assets").resolve(),
        ]

    def _is_allowed_script_path(self, path: Path) -> bool:
        resolved = path.resolve()
        for root in self._script_roots:
            try:
                resolved.relative_to(root.resolve())
                return True
            except ValueError:
                continue
        return False

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
        if self.path == "/notify":
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(content_length).decode("utf-8")
                body = json.loads(raw) if raw else {}
                if not isinstance(body, dict):
                    raise ValueError("notify body must be an object")
                result = self.executor.show_notify(body)
                self._send_json(200, {"ok": True, "result": result})
            except Exception as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
            return
        if self.path == "/operator-event":
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(content_length).decode("utf-8")
                event = json.loads(raw) if raw else {}
                if not isinstance(event, dict):
                    raise ValueError("event must be an object")
                self.executor.write_operator_event(event)
                self._send_json(200, {"ok": True})
            except Exception as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
            return
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
        if self.path.startswith("/operator-events"):
            try:
                from urllib.parse import urlparse, parse_qs
                parsed = urlparse(self.path)
                qs = parse_qs(parsed.query)
                machine_id = (qs.get("machine_id") or [""])[0]
                since_ms = int((qs.get("since_ms") or ["0"])[0])
                limit = int((qs.get("limit") or ["200"])[0])
                if not machine_id:
                    self._send_json(400, {"ok": False, "error": "machine_id required"})
                    return
                events = self.executor.read_operator_events(machine_id, since_ms, min(limit, 1000))
                self._send_json(200, {"ok": True, "events": events})
            except Exception as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
            return
        self._send_json(404, {"ok": False, "error": "not_found"})

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


class RunnerSSEClient:
    """Connects to daemon /runner/sse, dispatches received commands.

    Runs in a background daemon thread. Auto-reconnects on disconnect.
    """

    def __init__(
        self,
        team_lead_url: str,
        runner_profile: str,
        executor_show_notify,
    ) -> None:
        self._url = team_lead_url.rstrip("/")
        self._runner_profile = runner_profile
        self._show_notify = executor_show_notify
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="RunnerSSEClient"
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        import urllib.request as _ur
        import urllib.error as _ue
        backoff = 1.0
        while not self._stop.is_set():
            try:
                url = f"{self._url}/runner/sse?runner_profile={self._runner_profile}"
                req = _ur.Request(url, headers={"Accept": "text/event-stream"})
                with _ur.urlopen(req, timeout=None) as resp:
                    backoff = 1.0
                    buf = ""
                    while not self._stop.is_set():
                        chunk = resp.read(1)
                        if not chunk:
                            break
                        buf += chunk.decode("utf-8", errors="replace")
                        if "\n\n" in buf:
                            parts = buf.split("\n\n")
                            buf = parts[-1]
                            for part in parts[:-1]:
                                for line in part.splitlines():
                                    if line.startswith("data: "):
                                        try:
                                            cmd = json.loads(line[6:])
                                            threading.Thread(
                                                target=self._dispatch_command,
                                                args=(cmd,), daemon=True
                                            ).start()
                                        except json.JSONDecodeError:
                                            pass
            except (_ue.URLError, OSError):
                if not self._stop.is_set():
                    self._stop.wait(timeout=min(backoff, 30))
                    backoff = min(backoff * 2, 30)

    def _dispatch_command(self, cmd: dict) -> None:
        cmd_type = cmd.get("type")
        if cmd_type == "notify":
            popup_id = str(cmd.get("popup_id", ""))
            ui_spec = cmd.get("ui_spec", {})
            try:
                result = self._show_notify(ui_spec)
            except Exception:
                result = {"value": None}
            if ui_spec.get("type") != "toast":
                self._post_result(popup_id, result)

    def _post_result(self, popup_id: str, result: dict) -> None:
        import urllib.request as _ur
        import urllib.error as _ue
        payload = {"popup_id": popup_id, "value": result.get("value")}
        body = json.dumps(payload, ensure_ascii=False).encode()
        req = _ur.Request(
            f"{self._url}/runner/popup-result",
            data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with _ur.urlopen(req, timeout=5):
                pass
        except (_ue.URLError, OSError):
            pass


def _start_sse_client(executor: "RunnerExecutor") -> "RunnerSSEClient | None":
    """Start SSE client if team_lead_url is configured."""
    if not (executor._team_lead_url and executor._runner_profile):
        return None
    import urllib.request as _ur
    import urllib.error as _ue
    # POST /runner/online to register
    try:
        import socket as _sock
        machine_id = _sock.gethostname()
        body = json.dumps({
            "runner_profile": executor._runner_profile,
            "machine_id": machine_id,
        }).encode()
        req = _ur.Request(
            f"{executor._team_lead_url}/runner/online",
            data=body, headers={"Content-Type": "application/json"}
        )
        with _ur.urlopen(req, timeout=5):
            pass
    except (_ue.URLError, OSError):
        pass
    client = RunnerSSEClient(
        team_lead_url=executor._team_lead_url,
        runner_profile=executor._runner_profile,
        executor_show_notify=executor.show_notify,
    )
    client.start()
    return client


def run_server(host: str, port: int, *, root: Path | None = None, state_root: Path | None = None) -> None:
    _setup_logging()
    logging.info("emerge-remote-runner starting host=%s port=%d pid=%d", host, port, os.getpid())
    executor = RunnerExecutor(root=root, state_root=state_root)
    _start_sse_client(executor)
    executor._start_tray()
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
