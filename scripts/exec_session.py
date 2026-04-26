from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout
from hashlib import sha256
from pathlib import Path
from typing import Any

from scripts.policy_config import default_state_root, exec_limits, sessions_root


class _BoundedBuffer(io.TextIOBase):
    """Stream-like sink that caps total bytes written and records truncation.

    Once the byte budget is exhausted further writes are dropped silently but
    ``truncated_bytes`` keeps accumulating so the caller can surface an accurate
    "output truncated at N bytes (M more dropped)" warning.
    """

    def __init__(self, max_bytes: int) -> None:
        super().__init__()
        self._buf = io.StringIO()
        self._max = max(0, int(max_bytes))
        self._written = 0
        self._truncated_bytes = 0

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:  # type: ignore[override]
        if not s:
            return 0
        encoded_len = len(s.encode("utf-8", errors="replace"))
        remaining = self._max - self._written
        if remaining <= 0:
            self._truncated_bytes += encoded_len
            return len(s)
        if encoded_len <= remaining:
            self._buf.write(s)
            self._written += encoded_len
            return len(s)
        # Partial: take a prefix that fits in the remaining byte budget.
        raw = s.encode("utf-8", errors="replace")
        prefix = raw[:remaining].decode("utf-8", errors="ignore")
        self._buf.write(prefix)
        self._written += len(prefix.encode("utf-8", errors="replace"))
        self._truncated_bytes += encoded_len - (len(prefix.encode("utf-8", errors="replace")))
        return len(s)

    def getvalue(self) -> str:
        return self._buf.getvalue()

    @property
    def truncated_bytes(self) -> int:
        return self._truncated_bytes

    @property
    def written_bytes(self) -> int:
        return self._written


class ExecSession:
    """Persistent Python execution state for icc_exec.

    Resource limits (wall-clock timeout, captured stdout/stderr byte caps) are
    resolved per call from :func:`exec_limits`, so operators can tune them via
    environment variables without restarting the daemon.

    Session telemetry (``created_at_ms``, ``last_active_at_ms``, ``exec_count``,
    ``bytes_out_total``) is persisted in ``checkpoint.json`` alongside the
    replayable globals. This makes the session identity observable — callers
    can report per-session activity and the daemon can evict idle sessions.
    """

    def __init__(self, state_root: Path | None = None, session_id: str = "default") -> None:
        self._globals: dict[str, Any] = {"__builtins__": __builtins__}
        base = sessions_root(state_root or default_state_root())
        self._session_dir = base / session_id
        self._session_id = session_id
        self._wal_path = self._session_dir / "wal.jsonl"
        self._checkpoint_path = self._session_dir / "checkpoint.json"
        self._recovery_path = self._session_dir / "recovery.json"
        self._seq = 0
        self._wal_seq_applied = 0
        self._recovery_issues: list[dict[str, Any]] = []
        _now_ms = int(time.time() * 1000)
        self._meta: dict[str, Any] = {
            "session_id": session_id,
            "created_at_ms": _now_ms,
            "last_active_at_ms": _now_ms,
            "exec_count": 0,
            "bytes_out_total": 0,
            "truncated_bytes_total": 0,
            "timeout_count": 0,
        }
        # Serialises concurrent exec_code calls for a single profile.
        # ThreadingHTTPServer dispatches requests on separate threads; without
        # this lock two concurrent execs would race on _globals and _seq.
        self._exec_lock = threading.Lock()
        # When a timed-out thread is still running we mark the session poisoned.
        # Subsequent execs fail fast until the thread finishes, preventing
        # concurrent mutation of _globals by two threads.
        self._poisoned_thread: threading.Thread | None = None
        self._ensure_paths()
        self._restore_from_disk()

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def last_active_at_ms(self) -> int:
        return int(self._meta.get("last_active_at_ms", 0))

    def session_meta(self) -> dict[str, Any]:
        """Return a shallow copy of persistent session telemetry."""
        return dict(self._meta)

    def exec_code(
        self,
        code: str,
        *,
        metadata: dict[str, Any] | None = None,
        inject_vars: dict[str, Any] | None = None,
        result_var: str | None = None,
    ) -> dict[str, Any]:
        """Execute *code* in the persistent global namespace.

        ``metadata["no_replay"]`` (bool, default False) — when True the WAL
        entry is marked and skipped during restart replay.  Use this for code
        with side-effects that must not be re-executed (COM calls, file writes,
        network requests, etc.).  The globals mutation still applies for the
        current session; only the replay-on-restart behaviour changes.

        Note: only JSON-serialisable scalar/list/dict values survive a
        checkpoint.  Objects such as COM wrappers, file handles, or class
        instances are silently dropped from the checkpoint.  They are normally
        restored by replaying the code that created them — so mark such
        creation code ``no_replay=False`` (the default) and mark the
        *side-effectful* call sites ``no_replay=True``.
        """
        meta = metadata or {}
        no_replay = bool(meta.get("no_replay", False))
        limits = exec_limits()
        _exec_timeout = limits["timeout_s"]
        _stdout_cap = limits["stdout_bytes"]
        _stderr_cap = limits["stderr_bytes"]

        with self._exec_lock:
            # If a previous timed-out thread is still running, refuse to exec —
            # it would race on _globals and corrupt session state.
            if self._poisoned_thread is not None:
                if self._poisoned_thread.is_alive():
                    return {
                        "ok": False,
                        "is_error": True,
                        "error": (
                            "ExecSession is poisoned: a previous timed-out execution is still "
                            "running in the background. Start a fresh session to continue."
                        ),
                        "text": "",
                        "stdout": "",
                        "stderr": "",
                        "session_meta": self.session_meta(),
                    }
                self._poisoned_thread = None

            stdout_buf = _BoundedBuffer(_stdout_cap)
            stderr_buf = _BoundedBuffer(_stderr_cap)
            is_error = False
            error_message = ""
            timed_out = False
            start_ts_ms = int(time.time() * 1000)
            if inject_vars:
                for key, value in inject_vars.items():
                    self._globals[key] = value

            _exc_holder: list[BaseException] = []

            def _run() -> None:
                try:
                    with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
                        exec(code, self._globals, self._globals)  # noqa: S102
                except Exception as _e:
                    _exc_holder.append(_e)

            _t = threading.Thread(target=_run, daemon=True)
            _t.start()
            _t.join(timeout=_exec_timeout)
            if _t.is_alive():
                self._poisoned_thread = _t
                is_error = True
                timed_out = True
                error_message = (
                    f"ExecTimeout: code execution exceeded {_exec_timeout}s limit. "
                    f"Session is now poisoned — start a fresh session to continue. "
                    f"(Thread running daemon=True, will not block process exit.)"
                )
            elif _exc_holder:
                is_error = True
                error_message = "".join(
                    traceback.format_exception(type(_exc_holder[0]), _exc_holder[0], _exc_holder[0].__traceback__)
                )

            stdout = stdout_buf.getvalue()
            stderr = stderr_buf.getvalue()
            stdout_truncated = stdout_buf.truncated_bytes
            stderr_truncated = stderr_buf.truncated_bytes

            # Update session meta before composing the response so the numbers
            # reflect this call.
            finished_ts_ms = int(time.time() * 1000)
            self._meta["last_active_at_ms"] = finished_ts_ms
            self._meta["exec_count"] = int(self._meta.get("exec_count", 0)) + 1
            self._meta["bytes_out_total"] = int(self._meta.get("bytes_out_total", 0)) + (
                stdout_buf.written_bytes + stderr_buf.written_bytes
            )
            self._meta["truncated_bytes_total"] = int(
                self._meta.get("truncated_bytes_total", 0)
            ) + (stdout_truncated + stderr_truncated)
            if timed_out:
                self._meta["timeout_count"] = int(self._meta.get("timeout_count", 0)) + 1

            text_parts = []
            if stdout:
                text_parts.append(f"stdout:\n{stdout}".rstrip())
            if stdout_truncated:
                text_parts.append(
                    f"stdout_truncated: dropped {stdout_truncated} bytes (limit {_stdout_cap})"
                )
            if stderr:
                text_parts.append(f"stderr:\n{stderr}".rstrip())
            if stderr_truncated:
                text_parts.append(
                    f"stderr_truncated: dropped {stderr_truncated} bytes (limit {_stderr_cap})"
                )
            if is_error:
                text_parts.append(f"error:\n{error_message}".rstrip())

            if not is_error and result_var and result_var in self._globals:
                encoded_for_wal = self._serialize_value(self._globals.get(result_var))
                if encoded_for_wal is not None:
                    meta["result_var_name"] = result_var
                    meta["result_var_value"] = encoded_for_wal

            wal_entry_common = {
                "started_at_ms": start_ts_ms,
                "finished_at_ms": finished_ts_ms,
                "stdout_bytes": stdout_buf.written_bytes,
                "stderr_bytes": stderr_buf.written_bytes,
                "stdout_truncated_bytes": stdout_truncated,
                "stderr_truncated_bytes": stderr_truncated,
                "metadata": meta,
            }
            if is_error:
                self._append_wal(
                    {
                        "status": "error",
                        "code": code,
                        "error": error_message,
                        "timed_out": timed_out,
                        **wal_entry_common,
                    }
                )
                # Persist updated meta even on error so TTL/observability stay accurate.
                self._write_checkpoint(self._wal_seq_applied)
            else:
                seq = self._append_wal(
                    {
                        "status": "success",
                        "no_replay": no_replay,
                        "code": code,
                        **wal_entry_common,
                    }
                )
                self._write_checkpoint(seq)

            text = "\n\n".join(text_parts) if text_parts else "ok"
            payload: dict[str, Any] = {
                "content": [{"type": "text", "text": text}],
                "session_meta": self.session_meta(),
            }
            if stdout_truncated or stderr_truncated:
                payload["truncation"] = {
                    "stdout_bytes": stdout_truncated,
                    "stderr_bytes": stderr_truncated,
                    "stdout_limit": _stdout_cap,
                    "stderr_limit": _stderr_cap,
                }
            if is_error:
                payload["isError"] = True
                parsed = self._parse_exec_error(error_message, code)
                payload["error_class"] = "ExecTimeout" if timed_out else parsed["error_class"]
                payload["error_summary"] = parsed["error_summary"] if not timed_out else error_message.splitlines()[0]
                payload["failed_line"] = parsed["failed_line"]
                payload["recovery_suggestion"] = "exec"
                if timed_out:
                    payload["timed_out"] = True
            elif result_var:
                payload["result_var_name"] = result_var
                if result_var not in self._globals:
                    result_error = f"result var not found: {result_var}"
                    payload["result_var_error"] = result_error
                    payload["isError"] = True
                    payload["content"] = [{"type": "text", "text": result_error}]
                    payload["error_class"] = "ResultVarError"
                    payload["error_summary"] = result_error
                    payload["failed_line"] = 0
                    payload["recovery_suggestion"] = "exec"
                else:
                    encoded = self._serialize_value(self._globals.get(result_var))
                    if encoded is None:
                        result_error = f"result var not serializable: {result_var}"
                        payload["result_var_error"] = result_error
                        payload["isError"] = True
                        payload["content"] = [{"type": "text", "text": result_error}]
                        payload["error_class"] = "ResultVarError"
                        payload["error_summary"] = result_error
                        payload["failed_line"] = 0
                        payload["recovery_suggestion"] = "exec"
                    else:
                        payload["result_var_value"] = encoded
            return payload

    def _ensure_paths(self) -> None:
        self._session_dir.mkdir(parents=True, exist_ok=True)

    def _restore_from_disk(self) -> None:
        if self._checkpoint_path.exists():
            try:
                checkpoint = json.loads(self._checkpoint_path.read_text(encoding="utf-8"))
                if not isinstance(checkpoint, dict):
                    raise ValueError("checkpoint must be a JSON object")
                restored = checkpoint.get("globals", {})
                if isinstance(restored, dict):
                    self._globals.update(restored)
                self._wal_seq_applied = int(checkpoint.get("wal_seq_applied", 0))
                self._seq = self._wal_seq_applied
                # Restore persistent meta if present; only trust recognised keys.
                stored_meta = checkpoint.get("session_meta") or {}
                if isinstance(stored_meta, dict):
                    for key in (
                        "created_at_ms",
                        "last_active_at_ms",
                        "exec_count",
                        "bytes_out_total",
                        "truncated_bytes_total",
                        "timeout_count",
                    ):
                        if key in stored_meta:
                            self._meta[key] = stored_meta[key]
            except Exception as exc:
                self._recovery_issues.append(
                    {
                        "seq": -1,
                        "error": f"invalid_checkpoint: {exc}",
                        "code_preview": "checkpoint.json",
                    }
                )
                self._wal_seq_applied = 0
                self._seq = 0
        self._replay_wal_after_checkpoint()

    def _replay_wal_after_checkpoint(self) -> None:
        if not self._wal_path.exists():
            return
        with self._wal_path.open("r", encoding="utf-8") as f:
            for line in f:
                text = line.strip()
                if not text:
                    continue
                try:
                    item = json.loads(text)
                except Exception as exc:
                    self._recovery_issues.append(
                        {"seq": -1, "error": f"invalid_wal_json: {exc}", "code_preview": text[:200]}
                    )
                    continue
                try:
                    seq = int(item.get("seq", 0))
                except Exception as exc:
                    self._recovery_issues.append(
                        {
                            "seq": -1,
                            "error": f"invalid_wal_seq: {exc}",
                            "code_preview": text[:200],
                        }
                    )
                    continue
                self._seq = max(self._seq, seq)
                if seq <= self._wal_seq_applied:
                    continue
                if item.get("status") == "success":
                    if item.get("no_replay", False):
                        # Side-effectful code (COM calls, file writes, etc.) —
                        # skip replay to avoid re-executing on restart.
                        self._wal_seq_applied = seq
                        continue
                    code = str(item.get("code", ""))
                    try:
                        exec(code, self._globals, self._globals)
                        self._wal_seq_applied = seq
                    except Exception as exc:
                        self._recovery_issues.append(
                            {
                                "seq": seq,
                                "error": str(exc),
                                "code_preview": code[:200],
                            }
                        )
                        # Stop replay at first failed WAL step to avoid applying a non-prefix tail.
                        break
        # Persist the new replay point so startup remains fast after crash recovery.
        if self._wal_seq_applied:
            self._write_checkpoint(self._wal_seq_applied)
        self._write_recovery_status()

    def _append_wal(self, payload: dict[str, Any]) -> int:
        self._seq += 1
        row = {"seq": self._seq, **payload}
        with self._wal_path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(row, ensure_ascii=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return self._seq

    def _write_checkpoint(self, wal_seq_applied: int) -> None:
        serializable_globals: dict[str, Any] = {}
        for key, value in self._globals.items():
            if key == "__builtins__":
                continue
            encoded = self._serialize_value(value)
            if encoded is not None:
                serializable_globals[key] = encoded

        body = {
            "wal_seq_applied": wal_seq_applied,
            "globals": serializable_globals,
            "state_hash": sha256(
                json.dumps(serializable_globals, sort_keys=True, ensure_ascii=True).encode(
                    "utf-8"
                )
            ).hexdigest(),
            "updated_at_ms": int(time.time() * 1000),
            "session_meta": dict(self._meta),
        }
        fd, tmp_path = tempfile.mkstemp(
            prefix="checkpoint-", suffix=".json", dir=str(self._session_dir)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                json.dump(body, tmp, ensure_ascii=True, indent=2)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_path, self._checkpoint_path)
            tmp_path = ""
            self._wal_seq_applied = wal_seq_applied
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def _serialize_value(self, value: Any) -> Any | None:
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, list):
            out = []
            for item in value:
                encoded = self._serialize_value(item)
                if encoded is None:
                    return None
                out.append(encoded)
            return out
        if isinstance(value, dict):
            out_dict: dict[str, Any] = {}
            for k, v in value.items():
                if not isinstance(k, str):
                    return None
                encoded = self._serialize_value(v)
                if encoded is None:
                    return None
                out_dict[k] = encoded
            return out_dict
        return None

    def _write_recovery_status(self) -> None:
        body = {
            "recovery_degraded": bool(self._recovery_issues),
            "issues": self._recovery_issues[-20:],
            "updated_at_ms": int(time.time() * 1000),
        }
        fd, tmp_path = tempfile.mkstemp(
            prefix="recovery-", suffix=".json", dir=str(self._session_dir)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                json.dump(body, tmp, ensure_ascii=True, indent=2)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_path, self._recovery_path)
            tmp_path = ""
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    @staticmethod
    def _parse_exec_error(error_message: str, code: str) -> dict:
        """Extract structured fields from a traceback string.

        Returns dict with keys: error_class (str), error_summary (str), failed_line (int).
        """
        import re
        error_class = "Exception"
        error_summary = error_message.strip().splitlines()[-1] if error_message.strip() else ""
        failed_line = 0

        last_line = error_summary
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*):\s*(.*)", last_line)
        if m:
            error_class = m.group(1).split(".")[-1]
            error_summary = m.group(2).strip()

        for line in error_message.splitlines():
            lm = re.search(r",\s*line\s+(\d+)", line)
            if lm:
                failed_line = int(lm.group(1))

        return {
            "error_class": error_class,
            "error_summary": error_summary,
            "failed_line": failed_line,
        }
