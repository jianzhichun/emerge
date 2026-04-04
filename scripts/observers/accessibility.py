# scripts/observers/accessibility.py
from __future__ import annotations
from scripts.observer_plugin import ObserverPlugin


class AccessibilityObserver(ObserverPlugin):
    """Generic OS accessibility observer."""

    def start(self, config: dict) -> None:
        self._config = config
        self._active = True

    def stop(self) -> None:
        self._active = False

    def get_context(self, hint: dict) -> dict:
        import subprocess, sys as _sys
        ctx: dict = {"observer": "accessibility", "hint": hint}
        try:
            if _sys.platform == "darwin":
                result = subprocess.run(
                    ["osascript", "-e",
                     'tell application "System Events" to get name of first process whose frontmost is true'],
                    capture_output=True, text=True, timeout=2,
                )
                ctx["focused_app"] = result.stdout.strip()
        except Exception:
            pass
        return ctx

    def execute(self, intent: str, params: dict) -> dict:
        return {"ok": False, "error": "accessibility observer cannot execute — crystallize a vertical adapter"}


ADAPTER_CLASS = AccessibilityObserver
