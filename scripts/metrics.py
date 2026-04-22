from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from scripts.event_appender import EventAppender


class NullSink:
    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        pass


class LocalJSONLSink:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._appender = EventAppender(flush_interval_s=0.1, batch_size=128)

    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        event = {"ts_ms": int(time.time() * 1000), "event_type": event_type, **payload}
        self._appender.append_critical(self._path, event, ensure_ascii=True)


def get_sink(
    settings: dict[str, Any],
    *,
    default_path: Path | None = None,
) -> "LocalJSONLSink | NullSink":
    kind = str(settings.get("metrics_sink", "local_jsonl"))
    if kind == "null":
        return NullSink()
    path = default_path or (Path.home() / ".emerge" / "metrics.jsonl")
    return LocalJSONLSink(path=path)
