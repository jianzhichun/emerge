"""Tests for cmd_control_plane_runner_events."""
import json
import time

from scripts.admin.control_plane import cmd_control_plane_runner_events


def test_runner_events_invalid_profile_empty():
    result = cmd_control_plane_runner_events(profile="", limit=20)
    assert result["ok"] is False
    assert "invalid" in result["error"]


def test_runner_events_invalid_profile_special_chars():
    result = cmd_control_plane_runner_events(profile="bad/profile!", limit=20)
    assert result["ok"] is False
    assert "invalid" in result["error"]


def test_runner_events_missing_file(tmp_path, monkeypatch):
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path))
    result = cmd_control_plane_runner_events(profile="myrunner", limit=20)
    assert result["ok"] is True
    assert result["events"] == []
    assert len(result["activity"]) == 10
    assert result["today_events"] == 0
    assert result["today_alerts"] == 0


def test_runner_events_returns_newest_first(tmp_path, monkeypatch):
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path))
    events_dir = tmp_path / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    path = events_dir / "events-myrunner.jsonl"
    now = int(time.time() * 1000)
    path.write_text(
        json.dumps({"type": "runner_event", "ts_ms": now - 60000}) + "\n" +
        json.dumps({"type": "pattern_observed", "ts_ms": now - 1000}) + "\n",
        encoding="utf-8",
    )
    result = cmd_control_plane_runner_events(profile="myrunner", limit=20)
    assert result["ok"] is True
    assert result["events"][0]["type"] == "pattern_observed"  # newest first
    assert result["today_alerts"] == 1
    assert result["today_events"] == 2


def test_runner_events_activity_buckets(tmp_path, monkeypatch):
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path))
    now = int(time.time() * 1000)
    events_dir = tmp_path / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    path = events_dir / "events-myrunner.jsonl"
    # 3 events in the last bucket (last 6 minutes)
    lines = "\n".join(
        json.dumps({"type": "runner_event", "ts_ms": now - i * 60000})
        for i in range(3)
    ) + "\n"
    path.write_text(lines, encoding="utf-8")
    result = cmd_control_plane_runner_events(profile="myrunner", limit=20)
    assert result["ok"] is True
    assert sum(result["activity"]) == 3
    assert result["activity"][-1] >= 1  # most recent bucket has events
