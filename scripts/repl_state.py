from __future__ import annotations

import io
import json
import os
import tempfile
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout
from hashlib import sha256
from pathlib import Path
from typing import Any

from scripts.policy_config import default_repl_root


class ReplState:
    """Persistent Python execution state for icc_exec."""

    def __init__(self, state_root: Path | None = None, session_id: str = "default") -> None:
        self._globals: dict[str, Any] = {"__builtins__": __builtins__}
        base = state_root or default_repl_root()
        self._session_dir = base / session_id
        self._wal_path = self._session_dir / "wal.jsonl"
        self._checkpoint_path = self._session_dir / "checkpoint.json"
        self._recovery_path = self._session_dir / "recovery.json"
        self._seq = 0
        self._wal_seq_applied = 0
        self._recovery_issues: list[dict[str, Any]] = []
        self._ensure_paths()
        self._restore_from_disk()

    def exec_code(
        self,
        code: str,
        *,
        metadata: dict[str, Any] | None = None,
        inject_vars: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        is_error = False
        error_message = ""
        start_ts_ms = int(time.time() * 1000)
        if inject_vars:
            for key, value in inject_vars.items():
                self._globals[key] = value

        try:
            with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
                exec(code, self._globals, self._globals)
        except Exception:
            is_error = True
            error_message = traceback.format_exc()

        stdout = stdout_buf.getvalue()
        stderr = stderr_buf.getvalue()

        text_parts = []
        if stdout:
            text_parts.append(f"stdout:\n{stdout}".rstrip())
        if stderr:
            text_parts.append(f"stderr:\n{stderr}".rstrip())
        if is_error:
            text_parts.append(f"error:\n{error_message}".rstrip())

        if is_error:
            self._append_wal(
                {
                    "status": "error",
                    "code": code,
                    "started_at_ms": start_ts_ms,
                    "finished_at_ms": int(time.time() * 1000),
                    "error": error_message,
                    "metadata": metadata or {},
                }
            )
        else:
            seq = self._append_wal(
                {
                    "status": "success",
                    "code": code,
                    "started_at_ms": start_ts_ms,
                    "finished_at_ms": int(time.time() * 1000),
                    "metadata": metadata or {},
                }
            )
            self._write_checkpoint(seq)

        text = "\n\n".join(text_parts) if text_parts else "ok"
        payload: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
        if is_error:
            payload["isError"] = True
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
        with self._wal_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=True) + os.linesep)
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
            self._wal_seq_applied = wal_seq_applied
        finally:
            if os.path.exists(tmp_path):
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
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
