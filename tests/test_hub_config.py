import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.hub_config import (
    load_hub_config,
    save_hub_config,
    append_sync_event,
    consume_sync_events,
    load_pending_conflicts,
    save_pending_conflicts,
    is_configured,
    hub_config_path,
    sync_queue_path,
    pending_conflicts_path,
    hub_worktree_path,
)


@pytest.fixture()
def hub_home(tmp_path, monkeypatch):
    """Redirect all hub paths to tmp_path."""
    monkeypatch.setenv("EMERGE_HUB_HOME", str(tmp_path))
    return tmp_path


def test_load_hub_config_returns_empty_when_missing(hub_home):
    cfg = load_hub_config()
    assert cfg == {}


def test_save_and_load_hub_config_roundtrip(hub_home):
    cfg = {
        "remote": "git@quasar:team/hub.git",
        "branch": "emerge-hub",
        "poll_interval_seconds": 300,
        "selected_verticals": ["gmail", "linear"],
        "author": "alice <alice@team.com>",
    }
    save_hub_config(cfg)
    loaded = load_hub_config()
    assert loaded == cfg


def test_is_configured_false_when_missing(hub_home):
    assert is_configured() is False


def test_is_configured_true_when_remote_and_verticals_set(hub_home):
    save_hub_config({"remote": "git@x:y.git", "selected_verticals": ["gmail"]})
    assert is_configured() is True


def test_append_and_consume_sync_events(hub_home):
    append_sync_event({"event": "stable", "connector": "gmail", "pipeline": "fetch"})
    append_sync_event({"event": "reload", "connector": "gmail"})
    all_events = consume_sync_events(lambda e: e.get("event") == "stable")
    assert len(all_events) == 1
    assert all_events[0]["connector"] == "gmail"
    # Remaining event is still in queue
    remaining = consume_sync_events(lambda e: True)
    assert len(remaining) == 1
    assert remaining[0]["event"] == "reload"


def test_pending_conflicts_roundtrip(hub_home):
    data = {
        "conflicts": [
            {
                "conflict_id": "abc123",
                "connector": "gmail",
                "file": "pipelines/read/fetch.py",
                "ours_ts_ms": 1000,
                "theirs_ts_ms": 900,
                "status": "pending",
                "resolution": None,
            }
        ]
    }
    save_pending_conflicts(data)
    loaded = load_pending_conflicts()
    assert loaded["conflicts"][0]["conflict_id"] == "abc123"
