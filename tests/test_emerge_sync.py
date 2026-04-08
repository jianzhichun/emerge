import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.emerge_sync import export_vertical, import_vertical
from scripts.hub_config import save_hub_config


@pytest.fixture()
def connector_home(tmp_path, monkeypatch):
    """Fake ~/.emerge/connectors and hub worktree for tests."""
    connectors = tmp_path / "connectors"
    worktree = tmp_path / "hub-worktree"
    worktree.mkdir()
    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(connectors))
    monkeypatch.setenv("EMERGE_HUB_HOME", str(tmp_path))
    return connectors, worktree


def _make_connector(connectors: Path, name: str) -> None:
    base = connectors / name
    (base / "pipelines" / "read").mkdir(parents=True)
    (base / "pipelines" / "read" / "fetch.py").write_text("# fetch", encoding="utf-8")
    (base / "pipelines" / "read" / "fetch.yaml").write_text("connector: test", encoding="utf-8")
    (base / "NOTES.md").write_text("# Notes", encoding="utf-8")
    candidates = {
        "candidates": {
            "test.read.fetch": {"intent_signature": "test.read.fetch", "status": "stable", "last_ts_ms": 1000}
        }
    }
    (base / "span-candidates.json").write_text(json.dumps(candidates), encoding="utf-8")


def test_export_copies_pipelines_and_notes(connector_home):
    connectors, worktree = connector_home
    _make_connector(connectors, "gmail")
    export_vertical("gmail", connectors_root=connectors, hub_worktree=worktree)
    assert (worktree / "connectors" / "gmail" / "pipelines" / "read" / "fetch.py").exists()
    assert (worktree / "connectors" / "gmail" / "NOTES.md").exists()


def test_export_generates_spans_json_from_stable_candidates(connector_home):
    connectors, worktree = connector_home
    _make_connector(connectors, "gmail")
    export_vertical("gmail", connectors_root=connectors, hub_worktree=worktree)
    spans_path = worktree / "connectors" / "gmail" / "spans.json"
    assert spans_path.exists()
    spans = json.loads(spans_path.read_text())
    assert "test.read.fetch" in spans["spans"]


def test_import_overwrites_local_pipelines(connector_home):
    connectors, worktree = connector_home
    hub_dir = worktree / "connectors" / "gmail" / "pipelines" / "read"
    hub_dir.mkdir(parents=True)
    (hub_dir / "fetch.py").write_text("# remote version", encoding="utf-8")
    (hub_dir / "fetch.yaml").write_text("connector: gmail", encoding="utf-8")
    (worktree / "connectors" / "gmail" / "NOTES.md").write_text("# Remote Notes", encoding="utf-8")
    import_vertical("gmail", connectors_root=connectors, hub_worktree=worktree)
    local_py = connectors / "gmail" / "pipelines" / "read" / "fetch.py"
    assert local_py.read_text(encoding="utf-8") == "# remote version"


def test_import_merges_spans_json_newer_wins(connector_home):
    connectors, worktree = connector_home
    local_dir = connectors / "gmail"
    local_dir.mkdir(parents=True)
    local_spans = {"spans": {"gmail.read.fetch": {"intent_signature": "gmail.read.fetch", "last_ts_ms": 100}}}
    (local_dir / "spans.json").write_text(json.dumps(local_spans), encoding="utf-8")
    hub_dir = worktree / "connectors" / "gmail"
    hub_dir.mkdir(parents=True)
    hub_spans = {
        "spans": {
            "gmail.read.fetch": {"intent_signature": "gmail.read.fetch", "last_ts_ms": 999},
            "gmail.read.send": {"intent_signature": "gmail.read.send", "last_ts_ms": 500},
        }
    }
    (hub_dir / "spans.json").write_text(json.dumps(hub_spans), encoding="utf-8")
    import_vertical("gmail", connectors_root=connectors, hub_worktree=worktree)
    merged = json.loads((local_dir / "spans.json").read_text())
    assert merged["spans"]["gmail.read.fetch"]["last_ts_ms"] == 999
    assert "gmail.read.send" in merged["spans"]
