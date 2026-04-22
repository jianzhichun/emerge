from __future__ import annotations

import json
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any


class EventAppender:
    """Single-writer JSONL appender with bounded async batching."""

    def __init__(
        self,
        *,
        flush_interval_s: float = 0.1,
        batch_size: int = 128,
        queue_size: int = 4096,
    ) -> None:
        self._flush_interval_s = max(0.01, float(flush_interval_s))
        self._batch_size = max(1, int(batch_size))
        self._queue: queue.Queue[tuple[Path, str, bool, threading.Event | None]] = queue.Queue(
            maxsize=max(1, int(queue_size))
        )
        self._stop = threading.Event()
        self._worker = threading.Thread(
            target=self._run,
            daemon=True,
            name="EventAppender",
        )
        self._worker.start()

    def append(self, path: Path, event: dict[str, Any], *, ensure_ascii: bool = False) -> None:
        line = json.dumps(event, ensure_ascii=ensure_ascii) + "\n"
        self._put(path, line, False, None)

    def append_wait(
        self, path: Path, event: dict[str, Any], *, ensure_ascii: bool = False, timeout_s: float = 1.0
    ) -> bool:
        line = json.dumps(event, ensure_ascii=ensure_ascii) + "\n"
        done = threading.Event()
        self._put(path, line, False, done)
        return done.wait(timeout=max(0.1, timeout_s))

    def append_critical(
        self, path: Path, event: dict[str, Any], *, ensure_ascii: bool = False, timeout_s: float = 2.0
    ) -> bool:
        line = json.dumps(event, ensure_ascii=ensure_ascii) + "\n"
        done = threading.Event()
        self._put(path, line, True, done)
        return done.wait(timeout=max(0.1, timeout_s))

    def stop(self, *, timeout_s: float = 2.0) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        self._worker.join(timeout=max(0.1, timeout_s))

    def queue_depth(self) -> int:
        return self._queue.qsize()

    def _put(
        self, path: Path, line: str, force_fsync: bool, waiter: threading.Event | None
    ) -> None:
        item = (path, line, force_fsync, waiter)
        try:
            self._queue.put_nowait(item)
            return
        except queue.Full:
            # Degrade to sync write rather than dropping events.
            self._write_sync(path, line, fsync=force_fsync)
            if waiter is not None:
                waiter.set()

    def _run(self) -> None:
        pending: list[tuple[Path, str, bool, threading.Event | None]] = []
        last_flush_at = time.monotonic()
        while not self._stop.is_set() or not self._queue.empty() or pending:
            timeout = max(0.0, self._flush_interval_s - (time.monotonic() - last_flush_at))
            try:
                item = self._queue.get(timeout=timeout)
                pending.append(item)
            except queue.Empty:
                pass

            should_flush = (
                bool(pending)
                and (
                    len(pending) >= self._batch_size
                    or (time.monotonic() - last_flush_at) >= self._flush_interval_s
                    or self._stop.is_set()
                )
            )
            if not should_flush:
                continue

            by_path: dict[Path, list[tuple[str, bool, threading.Event | None]]] = {}
            for path, line, fsync, waiter in pending:
                by_path.setdefault(path, []).append((line, fsync, waiter))
            pending.clear()

            for path, entries in by_path.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as f:
                    needs_fsync = False
                    for line, fsync, _waiter in entries:
                        f.write(line)
                        needs_fsync = needs_fsync or fsync
                    f.flush()
                    if needs_fsync:
                        os.fsync(f.fileno())
                for _line, _fsync, waiter in entries:
                    if waiter is not None:
                        waiter.set()
            last_flush_at = time.monotonic()

    @staticmethod
    def _write_sync(path: Path, line: str, *, fsync: bool) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            if fsync:
                os.fsync(f.fileno())
