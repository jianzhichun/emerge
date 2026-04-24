from __future__ import annotations

import json
import threading
from pathlib import Path

from scripts.intent_registry import IntentRegistry, registry_path


def test_load_returns_empty_shape_when_missing(tmp_path: Path):
    data = IntentRegistry.load(tmp_path)
    assert data == {"intents": {}, "schema_version": 1}


def test_update_persists_entry(tmp_path: Path):
    entry = IntentRegistry.update(
        tmp_path,
        "mock.read.layers",
        stage="canary",
        rollout_pct=20,
        attempts=7,
        successes=6,
    )
    assert entry["stage"] == "canary"
    saved = json.loads(registry_path(tmp_path).read_text(encoding="utf-8"))
    assert saved["intents"]["mock.read.layers"]["rollout_pct"] == 20


def test_iter_helpers_filter_entries(tmp_path: Path):
    IntentRegistry.update(tmp_path, "a.read.x", stage="stable", persistent=True)
    IntentRegistry.update(tmp_path, "a.write.y", stage="explore", persistent=False)
    stable = IntentRegistry.iter_by_stage(tmp_path, "stable")
    persistent = IntentRegistry.iter_persistent(tmp_path)
    assert list(stable.keys()) == ["a.read.x"]
    assert list(persistent.keys()) == ["a.read.x"]


def test_concurrent_updates_keep_valid_json(tmp_path: Path):
    lock = threading.Lock()

    def _write(i: int) -> None:
        with lock:
            IntentRegistry.update(
                tmp_path,
                f"mock.read.{i}",
                stage="explore",
                attempts=i,
            )

    threads = [threading.Thread(target=_write, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    data = json.loads(registry_path(tmp_path).read_text(encoding="utf-8"))
    assert len(data["intents"]) == 10
