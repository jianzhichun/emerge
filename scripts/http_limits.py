from __future__ import annotations

import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading


DEFAULT_MAX_REQUEST_BYTES = 16 * 1024 * 1024
DEFAULT_HTTP_MAX_CONNECTIONS = 64


class RequestTooLarge(ValueError):
    pass


def max_request_bytes() -> int:
    raw = os.environ.get("EMERGE_MAX_REQUEST_BYTES", "").strip()
    if not raw:
        return DEFAULT_MAX_REQUEST_BYTES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_REQUEST_BYTES
    return value if value > 0 else DEFAULT_MAX_REQUEST_BYTES


def http_max_connections() -> int:
    raw = os.environ.get("EMERGE_HTTP_MAX_CONNECTIONS", "").strip()
    if not raw:
        return DEFAULT_HTTP_MAX_CONNECTIONS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_HTTP_MAX_CONNECTIONS
    return value if value > 0 else DEFAULT_HTTP_MAX_CONNECTIONS


def read_limited_body(handler: BaseHTTPRequestHandler, *, max_bytes: int | None = None) -> bytes:
    raw = handler.headers.get("Content-Length", "0")
    try:
        length = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid Content-Length") from exc
    if length < 0:
        raise ValueError("invalid Content-Length")
    limit = max_request_bytes() if max_bytes is None else int(max_bytes)
    if length > limit:
        raise RequestTooLarge(f"request body too large: {length} > {limit}")
    return handler.rfile.read(length) if length else b""


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, max_connections: int | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._connection_sem = threading.BoundedSemaphore(max_connections or http_max_connections())

    def process_request(self, request, client_address) -> None:
        if not self._connection_sem.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Connection: close\r\n"
                    b"Content-Length: 0\r\n\r\n"
                )
            except OSError:
                pass
            self.shutdown_request(request)
            return
        return super().process_request(request, client_address)

    def process_request_thread(self, request, client_address) -> None:
        try:
            return super().process_request_thread(request, client_address)
        finally:
            self._connection_sem.release()
