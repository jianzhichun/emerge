from __future__ import annotations

import json
import os
import tempfile
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
        fd, tmp = tempfile.mkstemp(prefix=".metrics-", suffix=".jsonl", dir=str(self._path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                if self._path.exists() and self._path.stat().st_size > 0:
                    f.write(self._path.read_text(encoding="utf-8"))
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
            tmp = ""
        finally:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)


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
