from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


class NullSink:
    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        pass


class LocalJSONLSink:
    def __init__(self, path: Path) -> None:
        self._path = path

    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        event = {"ts_ms": int(time.time() * 1000), "event_type": event_type, **payload}
        line = json.dumps(event, ensure_ascii=True) + "\n"
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())


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
