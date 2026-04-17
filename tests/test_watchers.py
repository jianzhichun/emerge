"""Tests for the watcher heartbeat SLO (scripts/watchers.py + watch_emerge.py)."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.watchers import (  # noqa: E402
    compute_watcher_status,
    read_all_heartbeats,
    watcher_health_summary,
    watcher_heartbeat_path,
    write_heartbeat,
)


def test_write_heartbeat_roundtrip(tmp_path: Path) -> None:
    record = {
        "watcher_id": "global",
        "pid": 1234,
        "target": "events/events.jsonl",
        "started_at_ms": 1000,
        "last_loop_ts_ms": 2000,
        "events_read": 5,
        "events_delivered": 4,
    }
    path = write_heartbeat(tmp_path, record)
    assert path == watcher_heartbeat_path(tmp_path, "global")
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["watcher_id"] == "global"
    assert data["events_read"] == 5
    assert data["updated_at_ms"] >= 1000


def test_write_heartbeat_rejects_missing_id(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(ValueError):
        write_heartbeat(tmp_path, {"pid": 1})


def test_read_all_heartbeats_is_sorted_most_recent_first(tmp_path: Path) -> None:
    write_heartbeat(tmp_path, {"watcher_id": "a", "last_loop_ts_ms": 1000})
    write_heartbeat(tmp_path, {"watcher_id": "b", "last_loop_ts_ms": 3000})
    write_heartbeat(tmp_path, {"watcher_id": "c", "last_loop_ts_ms": 2000})
    records = read_all_heartbeats(tmp_path)
    assert [r["watcher_id"] for r in records] == ["b", "c", "a"]


def test_compute_watcher_status_marks_alive(tmp_path: Path) -> None:
    now = int(time.time() * 1000)
    record = {
        "watcher_id": "global",
        "last_loop_ts_ms": now - 500,
        "events_read": 3,
        "events_delivered": 2,
    }
    status = compute_watcher_status(record, now_ms=now, stale_after_s=30)
    assert status["alive"] is True
    assert status["lag_ms"] == 500
    assert status["events_delivered"] == 2


def test_compute_watcher_status_marks_stale(tmp_path: Path) -> None:
    now = int(time.time() * 1000)
    record = {"watcher_id": "global", "last_loop_ts_ms": now - 120_000}
    status = compute_watcher_status(record, now_ms=now, stale_after_s=30)
    assert status["alive"] is False
    assert status["lag_ms"] == 120_000


def test_compute_watcher_status_marks_stopped_as_not_alive(tmp_path: Path) -> None:
    now = int(time.time() * 1000)
    record = {
        "watcher_id": "global",
        "last_loop_ts_ms": now,
        "stopped_at_ms": now,
    }
    status = compute_watcher_status(record, now_ms=now, stale_after_s=30)
    assert status["alive"] is False
    assert status["stopped_at_ms"] == now


def test_watcher_health_summary_aggregates(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EMERGE_WATCHER_STALE_S", "30")
    now = int(time.time() * 1000)
    write_heartbeat(tmp_path, {"watcher_id": "global", "last_loop_ts_ms": now - 500})
    write_heartbeat(tmp_path, {"watcher_id": "runner-x", "last_loop_ts_ms": now - 60_000})
    summary = watcher_health_summary(tmp_path)
    assert summary["total"] == 2
    assert summary["alive_count"] == 1
    assert summary["stale_count"] == 1
    assert summary["healthy"] is False
    assert "runner-x" in summary["stale_watcher_ids"]


def test_watcher_health_summary_empty_is_healthy(tmp_path: Path) -> None:
    summary = watcher_health_summary(tmp_path)
    assert summary["total"] == 0
    assert summary["healthy"] is True


def test_cmd_control_plane_watchers(tmp_path: Path, monkeypatch) -> None:
    from scripts.admin.control_plane import cmd_control_plane_watchers

    now = int(time.time() * 1000)
    write_heartbeat(tmp_path, {"watcher_id": "global", "last_loop_ts_ms": now - 100})
    result = cmd_control_plane_watchers(state_root=tmp_path)
    assert result["ok"] is True
    assert result["total"] == 1
    assert result["watchers"][0]["watcher_id"] == "global"
    assert result["watchers"][0]["alive"] is True


def test_watch_emerge_writes_heartbeat_on_start(tmp_path: Path) -> None:
    events_root = tmp_path / "events"
    events_root.mkdir(parents=True, exist_ok=True)

    proc = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "scripts" / "watch_emerge.py"),
            "--state-root",
            str(tmp_path),
            "--watcher-id",
            "test-global",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.time() + 3.0
        hb_path = watcher_heartbeat_path(tmp_path, "test-global")
        while time.time() < deadline and not hb_path.exists():
            time.sleep(0.1)
        assert hb_path.exists(), "heartbeat file must be written shortly after startup"
        record = json.loads(hb_path.read_text(encoding="utf-8"))
        assert record["watcher_id"] == "test-global"
        assert record["pid"] == proc.pid
        assert str(record["target"]).endswith("events.jsonl")
    finally:
        proc.terminate()
        proc.wait(timeout=3)


def test_watch_emerge_marks_heartbeat_stopped_on_exit(tmp_path: Path) -> None:
    proc = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "scripts" / "watch_emerge.py"),
            "--state-root",
            str(tmp_path),
            "--watcher-id",
            "test-stop",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    hb_path = watcher_heartbeat_path(tmp_path, "test-stop")
    deadline = time.time() + 3.0
    while time.time() < deadline and not hb_path.exists():
        time.sleep(0.1)
    assert hb_path.exists()
    proc.terminate()
    proc.wait(timeout=3)
    # After shutdown the heartbeat must reflect the stopped state.
    record = json.loads(hb_path.read_text(encoding="utf-8"))
    assert record.get("stopped_at_ms")


def test_watch_emerge_survives_malformed_event_line(tmp_path: Path) -> None:
    """A malformed (non-JSON) line must not take the watcher down; heartbeat keeps flowing."""
    events_root = tmp_path / "events"
    events_root.mkdir(parents=True, exist_ok=True)
    events_file = events_root / "events.jsonl"

    proc = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "scripts" / "watch_emerge.py"),
            "--state-root",
            str(tmp_path),
            "--watcher-id",
            "test-resilient",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        hb_path = watcher_heartbeat_path(tmp_path, "test-resilient")
        deadline = time.time() + 3.0
        while time.time() < deadline and not hb_path.exists():
            time.sleep(0.1)
        assert hb_path.exists()

        # Append garbage then a well-formed event.
        with events_file.open("a", encoding="utf-8") as f:
            f.write("this is not json\n")
            f.write(json.dumps({
                "type": "runner_discovered",
                "ts_ms": 1,
                "runner_profile": "x",
                "machine_id": "m",
            }) + "\n")
        time.sleep(1.2)

        record = json.loads(hb_path.read_text(encoding="utf-8"))
        # Process is still alive and reading.
        assert proc.poll() is None
        assert record["events_read"] >= 1
    finally:
        proc.terminate()
        proc.wait(timeout=3)


def test_cockpit_status_exposes_watcher_summary(tmp_path: Path, monkeypatch) -> None:
    import urllib.request

    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(tmp_path / "connectors"))
    (tmp_path / "connectors").mkdir(exist_ok=True)
    monkeypatch.setenv("EMERGE_WATCHER_STALE_S", "30")

    now = int(time.time() * 1000)
    write_heartbeat(tmp_path, {"watcher_id": "global", "last_loop_ts_ms": now - 500})
    write_heartbeat(tmp_path, {"watcher_id": "runner-foo", "last_loop_ts_ms": now - 90_000})

    from scripts.repl_admin import cmd_serve

    base = cmd_serve(port=0, open_browser=False)["url"]
    with urllib.request.urlopen(f"{base}/api/status") as resp:
        status = json.loads(resp.read())
    assert status["watchers_total"] == 2
    assert status["watchers_alive_count"] == 1
    assert status["watchers_healthy"] is False
    assert "runner-foo" in status["watchers_stale_ids"]

    with urllib.request.urlopen(f"{base}/api/control-plane/watchers") as resp:
        watchers = json.loads(resp.read())
    assert watchers["ok"] is True
    ids = {w["watcher_id"] for w in watchers["watchers"]}
    assert ids == {"global", "runner-foo"}


def test_watch_emerge_no_heartbeat_flag_disables_file(tmp_path: Path) -> None:
    proc = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "scripts" / "watch_emerge.py"),
            "--state-root",
            str(tmp_path),
            "--watcher-id",
            "test-disabled",
            "--no-heartbeat",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        time.sleep(0.8)
        hb_path = watcher_heartbeat_path(tmp_path, "test-disabled")
        assert not hb_path.exists()
    finally:
        proc.terminate()
        proc.wait(timeout=3)
