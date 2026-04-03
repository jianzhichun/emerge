from __future__ import annotations

import io
import traceback
from contextlib import redirect_stderr, redirect_stdout
from typing import Any


class ReplState:
    """Persistent Python execution state for icc_exec."""

    def __init__(self) -> None:
        self._globals: dict[str, Any] = {"__builtins__": __builtins__}

    def exec_code(self, code: str) -> dict[str, Any]:
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        is_error = False
        error_message = ""

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

        text = "\n\n".join(text_parts) if text_parts else "ok"
        payload: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
        if is_error:
            payload["isError"] = True
        return payload
