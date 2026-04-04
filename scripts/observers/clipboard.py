from __future__ import annotations
import sys as _sys
from scripts.observer_plugin import ObserverPlugin


class ClipboardObserver(ObserverPlugin):
    """Reads OS clipboard content."""

    def start(self, config: dict) -> None:
        self._active = True

    def stop(self) -> None:
        self._active = False

    def get_context(self, hint: dict) -> dict:
        content = self._read_clipboard()
        return {"observer": "clipboard", "content": content}

    def execute(self, intent: str, params: dict) -> dict:
        return {"ok": False, "error": "clipboard observer cannot execute — crystallize a vertical adapter"}

    @staticmethod
    def _read_clipboard() -> str:
        try:
            if _sys.platform == "darwin":
                import subprocess
                r = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=2)
                return r.stdout
            if _sys.platform == "win32":
                import subprocess
                r = subprocess.run(
                    ["powershell", "-command", "Get-Clipboard"],
                    capture_output=True, text=True, timeout=2,
                )
                return r.stdout
        except Exception:
            pass
        return ""


ADAPTER_CLASS = ClipboardObserver
