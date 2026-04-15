from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.remote_runner import RunnerSSEClient


class _FakeSSEStream:
    def __init__(self, payload: str) -> None:
        self._data = payload.encode("utf-8")
        self._idx = 0

    def read(self, n: int = 1) -> bytes:
        if self._idx >= len(self._data):
            return b""
        end = min(self._idx + n, len(self._data))
        chunk = self._data[self._idx:end]
        self._idx = end
        return chunk

    def readline(self) -> bytes:
        if self._idx >= len(self._data):
            return b""
        nl = self._data.find(b"\n", self._idx)
        if nl == -1:
            end = len(self._data)
        else:
            end = nl + 1
        line = self._data[self._idx:end]
        self._idx = end
        return line


def _build_sse_payload(event_count: int, *, body_size: int = 8) -> str:
    chunks: list[str] = []
    for i in range(event_count):
        body = ("x" * body_size) + f"-{i}"
        cmd = {
            "type": "notify",
            "popup_id": f"p-{i}",
            "ui_spec": {"type": "toast", "body": body},
        }
        chunks.append(f"event: notify\ndata: {json.dumps(cmd, ensure_ascii=False)}\n\n")
    return "".join(chunks)


def _build_multiline_data_event(*, popup_id: str, body: str) -> str:
    # Deliberately split JSON across two data lines; SSE should join with "\n".
    return (
        "event: notify\n"
        f'data: {{"type":"notify","popup_id":"{popup_id}",\n'
        f'data: "ui_spec":{{"type":"toast","body":"{body}"}}}}\n'
        "\n"
    )


def _parse_legacy_byte_by_byte(payload: str) -> int:
    resp = _FakeSSEStream(payload)
    count = 0
    buf = ""
    while True:
        chunk = resp.read(1)
        if not chunk:
            break
        buf += chunk.decode("utf-8", errors="replace")
        if "\n\n" in buf:
            parts = buf.split("\n\n")
            buf = parts[-1]
            for part in parts[:-1]:
                for line in part.splitlines():
                    if line.startswith("data: "):
                        try:
                            json.loads(line[6:])
                            count += 1
                        except json.JSONDecodeError:
                            pass
    return count


def _run_line_parser(payload: str, monkeypatch: pytest.MonkeyPatch) -> tuple[int, float]:
    import scripts.remote_runner as rr

    class _ImmediateThread:
        def __init__(self, target, args=(), daemon=True, name=None):
            self._target = target
            self._args = args

        def start(self):
            self._target(*self._args)

    monkeypatch.setattr(rr.threading, "Thread", _ImmediateThread)

    client = RunnerSSEClient.__new__(RunnerSSEClient)
    client._stop = type("_Stop", (), {"is_set": staticmethod(lambda: False)})()
    parsed_events: list[dict] = []
    client._dispatch_command = lambda cmd: parsed_events.append(cmd)
    client._runner_profile = "bench"
    client._url = "http://localhost"
    client._show_notify = lambda spec: {"action": "dismissed", "value": ""}
    t0 = time.perf_counter()
    client._consume_sse_stream(_FakeSSEStream(payload))
    return len(parsed_events), (time.perf_counter() - t0)


@pytest.mark.parametrize(
    "event_count,body_size",
    [
        (2000, 8),    # small payload
        (1000, 2048), # large payload
    ],
)
def test_sse_line_parser_benchmark(monkeypatch, event_count: int, body_size: int):
    payload = _build_sse_payload(event_count=event_count, body_size=body_size)

    legacy_t0 = time.perf_counter()
    legacy_count = _parse_legacy_byte_by_byte(payload)
    legacy_elapsed = time.perf_counter() - legacy_t0

    line_count, line_elapsed = _run_line_parser(payload, monkeypatch)

    assert legacy_count == event_count
    assert line_count == event_count
    # Allow wide margin for noisy environments; this catches only hard regressions.
    assert line_elapsed <= legacy_elapsed * 2.5


def test_sse_line_parser_supports_multiline_data(monkeypatch):
    payload = _build_multiline_data_event(popup_id="p-1", body="hello")
    line_count, _ = _run_line_parser(payload, monkeypatch)
    assert line_count == 1
