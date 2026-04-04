from __future__ import annotations
from pathlib import Path
from scripts.observer_plugin import ObserverPlugin


class FilesystemObserver(ObserverPlugin):
    """Polls a watched directory for file changes."""

    def __init__(self) -> None:
        self._watch_path = Path(".")
        self._active = False
        self._last_scan: dict[str, float] = {}

    def start(self, config: dict) -> None:
        self._watch_path = Path(config.get("path", ".")).expanduser()
        self._last_scan: dict[str, float] = {}
        self._active = True

    def stop(self) -> None:
        self._active = False

    def get_context(self, hint: dict) -> dict:
        if not self._watch_path.exists():
            return {"observer": "filesystem", "files": []}
        files = [
            {"name": f.name, "mtime": f.stat().st_mtime}
            for f in self._watch_path.iterdir()
            if f.is_file()
        ]
        return {"observer": "filesystem", "path": str(self._watch_path), "files": files}

    def execute(self, intent: str, params: dict) -> dict:
        return {"ok": False, "error": "filesystem observer cannot execute — crystallize a vertical adapter"}


ADAPTER_CLASS = FilesystemObserver
