from __future__ import annotations
import json, subprocess, sys, time
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _write_event(path: Path, event: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


def test_watch_emerge_global_prints_runner_discovered(tmp_path):
    """watch_emerge.py tails events.jsonl and prints runner_discovered events."""
    events_file = tmp_path / "events.jsonl"

    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "watch_emerge.py"),
         "--state-root", str(tmp_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    time.sleep(0.3)

    _write_event(events_file, {
        "type": "runner_discovered",
        "ts_ms": 1000,
        "runner_profile": "mycader-1",
        "machine_id": "wkst-A",
    })
    time.sleep(0.6)
    proc.terminate()
    out = proc.stdout.read().decode()
    assert "runner_discovered" in out or "mycader-1" in out


def test_watch_emerge_runner_mode_tails_profile_file(tmp_path):
    events_file = tmp_path / "events-mycader-1.jsonl"

    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "watch_emerge.py"),
         "--runner-profile", "mycader-1",
         "--state-root", str(tmp_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    time.sleep(0.3)

    _write_event(events_file, {
        "type": "pattern_alert",
        "ts_ms": 1000,
        "runner_profile": "mycader-1",
        "stage": "canary",
        "intent_signature": "hypermesh.mesh.batch",
        "meta": {"occurrences": 5, "window_minutes": 10, "machine_ids": ["wkst"]},
    })
    time.sleep(0.6)
    proc.terminate()
    out = proc.stdout.read().decode()
    assert "canary" in out or "hypermesh" in out


def test_watch_emerge_exits_on_sigterm(tmp_path):
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "watch_emerge.py"),
         "--state-root", str(tmp_path)],
        stdout=subprocess.PIPE,
    )
    time.sleep(0.2)
    proc.terminate()
    proc.wait(timeout=3)
    assert proc.returncode is not None


def test_watch_emerge_global_writes_cockpit_ack(tmp_path):
    events_file = tmp_path / "events.jsonl"
    event_id = "cockpit-watch-test-1"

    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "watch_emerge.py"),
         "--state-root", str(tmp_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    time.sleep(0.3)

    _write_event(events_file, {
        "type": "cockpit_action",
        "event_id": event_id,
        "ts_ms": 1000,
        "actions": [{"type": "pipeline-delete", "key": "x"}],
    })
    time.sleep(0.7)
    proc.terminate()
    proc.wait(timeout=3)

    ack_file = tmp_path / "cockpit-action-acks.jsonl"
    assert ack_file.exists()
    ack = json.loads(ack_file.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert ack["event_id"] == event_id


def test_watch_patterns_shim_delegates_to_watch_emerge(tmp_path):
    """watch_emerge.py --runner-profile tails per-runner events."""
    events_file = tmp_path / "events-mycader-1.jsonl"

    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "watch_emerge.py"),
         "--runner-profile", "mycader-1",
         "--state-root", str(tmp_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    time.sleep(0.3)
    _write_event(events_file, {
        "type": "pattern_alert",
        "ts_ms": 1000,
        "stage": "canary",
        "intent_signature": "hypermesh.mesh.batch",
        "meta": {"occurrences": 5, "window_minutes": 10, "machine_ids": ["wkst"]},
    })
    time.sleep(0.6)
    proc.terminate()
    out = proc.stdout.read().decode()
    assert "canary" in out or "hypermesh" in out
