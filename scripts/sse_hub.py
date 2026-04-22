from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Any


@dataclass
class _ClientState:
    key: str
    wfile: Any
    q: queue.Queue[bytes]
    thread: threading.Thread
    dropped: int = 0


class SSEHub:
    """Queue-backed SSE writer hub to isolate slow clients."""

    def __init__(self, *, queue_size: int = 64) -> None:
        self._queue_size = max(1, int(queue_size))
        self._lock = threading.Lock()
        self._clients: dict[str, _ClientState] = {}

    def register(self, key: str, wfile: Any) -> None:
        self.unregister(key)
        q: queue.Queue[bytes] = queue.Queue(maxsize=self._queue_size)
        thread = threading.Thread(
            target=self._writer_loop,
            args=(key,),
            daemon=True,
            name=f"SSEHub-{key}",
        )
        with self._lock:
            self._clients[key] = _ClientState(key=key, wfile=wfile, q=q, thread=thread)
        thread.start()

    def unregister(self, key: str) -> None:
        with self._lock:
            state = self._clients.pop(key, None)
        if state is not None:
            try:
                state.q.put_nowait(b"")
            except queue.Full:
                pass

    def send(self, key: str, payload: bytes) -> bool:
        with self._lock:
            state = self._clients.get(key)
        if state is None:
            return False
        try:
            state.q.put_nowait(payload)
            return True
        except queue.Full:
            try:
                state.q.get_nowait()  # drop oldest
            except queue.Empty:
                pass
            state.dropped += 1
            try:
                state.q.put_nowait(payload)
                return True
            except queue.Full:
                state.dropped += 1
                return False

    def broadcast(self, payload: bytes) -> None:
        with self._lock:
            keys = list(self._clients.keys())
        for key in keys:
            self.send(key, payload)

    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def _writer_loop(self, key: str) -> None:
        while True:
            with self._lock:
                state = self._clients.get(key)
            if state is None:
                return
            try:
                payload = state.q.get(timeout=0.5)
            except queue.Empty:
                continue
            if payload == b"":
                return
            try:
                state.wfile.write(payload)
                state.wfile.flush()
            except OSError:
                self.unregister(key)
                return
