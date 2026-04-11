# scripts/event_router.py
from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Callable, Literal

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler, FileSystemEvent
    _WATCHDOG_AVAILABLE = True
except ImportError:
    _WATCHDOG_AVAILABLE = False


class EventRouter:
    """Dispatches file system events to registered callbacks.

    Uses watchdog (inotify/FSEvents) when available; falls back to a 1s
    mtime-polling thread when watchdog is not installed.

    Usage::

        router = EventRouter({
            Path("~/.emerge/sync-queue.jsonl").expanduser(): on_sync_queue,
            Path("~/.emerge/exec/pending-actions.json").expanduser(): on_pending,
        })
        router.start()   # drains existing files, then watches
        ...
        router.stop()
    """

    def __init__(self, handlers: dict[Path, Callable[[Path], None]]) -> None:
        self._handlers = handlers
        self._mode: Literal["inotify", "polling"] = "polling"
        self._observer: "Observer | None" = None
        self._poll_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    @property
    def mode(self) -> Literal["inotify", "polling"]:
        return self._mode

    def _dispatch(self, path: Path) -> None:
        """Call handler registered for *path* (exact match or child). Swallows exceptions."""
        for watch_path, callback in self._handlers.items():
            if path == watch_path or (watch_path.is_dir() and path.is_relative_to(watch_path)):
                try:
                    callback(path)
                except Exception:
                    pass

    def start(self) -> None:
        """Drain existing watched files, then begin watching."""
        for path in self._handlers:
            if path.exists():
                self._dispatch(path)

        if _WATCHDOG_AVAILABLE:
            self._start_watchdog()
            self._mode = "inotify"
        else:
            print(
                "[EventRouter] watchdog not installed — using polling fallback. "
                "Install with: pip install watchdog",
                file=sys.stderr,
            )
            self._start_polling()
            self._mode = "polling"

    def stop(self) -> None:
        self._stop_event.set()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)

    def _start_watchdog(self) -> None:
        handler = _RouterHandler(self._handlers)
        observer = Observer()
        watched_dirs: set[str] = set()
        for path in self._handlers:
            watch_dir = str(path) if path.is_dir() else str(path.parent)
            if watch_dir not in watched_dirs:
                observer.schedule(handler, watch_dir, recursive=True)
                watched_dirs.add(watch_dir)
        observer.start()
        self._observer = observer

    def _start_polling(self) -> None:
        mtimes: dict[Path, float] = {}

        def _poll() -> None:
            while not self._stop_event.wait(1.0):
                for path in list(self._handlers):
                    if not path.exists():
                        continue
                    try:
                        mtime = path.stat().st_mtime
                    except OSError:
                        continue
                    if mtime != mtimes.get(path):
                        mtimes[path] = mtime
                        self._dispatch(path)

        self._poll_thread = threading.Thread(target=_poll, daemon=True, name="EventRouter-poll")
        self._poll_thread.start()


if _WATCHDOG_AVAILABLE:
    class _RouterHandler(FileSystemEventHandler):
        def __init__(self, handlers: dict[Path, Callable[[Path], None]]) -> None:
            self._handlers = handlers

        def _try_dispatch(self, src: str) -> None:
            p = Path(src)
            for watch_path, callback in self._handlers.items():
                if p == watch_path or (watch_path.is_dir() and p.is_relative_to(watch_path)):
                    try:
                        callback(p)
                    except Exception:
                        pass

        def on_modified(self, event: "FileSystemEvent") -> None:
            if not event.is_directory:
                self._try_dispatch(event.src_path)

        def on_created(self, event: "FileSystemEvent") -> None:
            if not event.is_directory:
                self._try_dispatch(event.src_path)

        def on_moved(self, event: "FileSystemEvent") -> None:
            if not event.is_directory:
                self._try_dispatch(event.dest_path)
else:
    class _RouterHandler:  # type: ignore[no-redef]
        pass
