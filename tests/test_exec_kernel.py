"""Tests for ExecSession resource-limit hardening and EmergeDaemon session TTL."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.exec_session import ExecSession  # noqa: E402


def _load_checkpoint(state_root: Path, session_id: str) -> dict:
    path = state_root / "sessions" / session_id / "checkpoint.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_exec_session_meta_initialised(tmp_path: Path) -> None:
    session = ExecSession(state_root=tmp_path, session_id="meta-init")
    meta = session.session_meta()
    assert meta["session_id"] == "meta-init"
    assert meta["exec_count"] == 0
    assert meta["bytes_out_total"] == 0
    assert meta["created_at_ms"] > 0
    assert meta["last_active_at_ms"] == meta["created_at_ms"]


def test_exec_code_updates_session_meta(tmp_path: Path) -> None:
    session = ExecSession(state_root=tmp_path, session_id="meta-update")
    before = session.session_meta()
    time.sleep(0.002)
    result = session.exec_code("print('hi')", metadata={"intent_signature": "m.read.x"})
    after = session.session_meta()

    assert result.get("isError") is not True
    assert "session_meta" in result
    assert result["session_meta"]["exec_count"] == 1
    assert after["exec_count"] == 1
    assert after["bytes_out_total"] >= len("hi\n")
    assert after["last_active_at_ms"] >= before["last_active_at_ms"]


def test_exec_session_meta_persists_across_restart(tmp_path: Path) -> None:
    session_a = ExecSession(state_root=tmp_path, session_id="meta-persist")
    session_a.exec_code("y = 7", metadata={"intent_signature": "m.read.x"})
    meta_a = session_a.session_meta()

    session_b = ExecSession(state_root=tmp_path, session_id="meta-persist")
    meta_b = session_b.session_meta()

    assert meta_b["created_at_ms"] == meta_a["created_at_ms"]
    assert meta_b["exec_count"] == meta_a["exec_count"]
    assert meta_b["bytes_out_total"] == meta_a["bytes_out_total"]

    checkpoint = _load_checkpoint(tmp_path, "meta-persist")
    assert "session_meta" in checkpoint
    assert checkpoint["session_meta"]["exec_count"] == 1


def test_exec_stdout_truncates_at_byte_cap(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EMERGE_EXEC_STDOUT_BYTES", "64")
    session = ExecSession(state_root=tmp_path, session_id="trunc-stdout")
    # Write ~2KB but only 64B should be captured.
    result = session.exec_code(
        "import sys; sys.stdout.write('x' * 2048)",
        metadata={"intent_signature": "m.read.x"},
    )

    assert result.get("isError") is not True
    assert "truncation" in result
    assert result["truncation"]["stdout_bytes"] > 0
    assert result["truncation"]["stdout_limit"] == 64

    text = result["content"][0]["text"]
    assert "stdout_truncated" in text
    # Captured stdout must not exceed the cap.
    captured = text.split("stdout:\n", 1)[1].split("\n\nstdout_truncated", 1)[0]
    assert len(captured.encode("utf-8")) <= 64


def test_exec_stderr_truncates_at_byte_cap(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EMERGE_EXEC_STDERR_BYTES", "32")
    session = ExecSession(state_root=tmp_path, session_id="trunc-stderr")
    result = session.exec_code(
        "import sys; sys.stderr.write('y' * 512)",
        metadata={"intent_signature": "m.read.x"},
    )

    assert "truncation" in result
    assert result["truncation"]["stderr_bytes"] > 0
    assert result["truncation"]["stderr_limit"] == 32


def test_exec_wal_records_truncation_bytes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EMERGE_EXEC_STDOUT_BYTES", "16")
    session = ExecSession(state_root=tmp_path, session_id="trunc-wal")
    session.exec_code(
        "import sys; sys.stdout.write('z' * 1024)",
        metadata={"intent_signature": "m.read.x"},
    )
    wal_path = tmp_path / "sessions" / "trunc-wal" / "wal.jsonl"
    lines = [json.loads(l) for l in wal_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert lines
    entry = lines[-1]
    assert entry["status"] == "success"
    assert entry["stdout_bytes"] <= 16
    assert entry["stdout_truncated_bytes"] > 0


def test_exec_error_increments_exec_count_and_persists(tmp_path: Path) -> None:
    session = ExecSession(state_root=tmp_path, session_id="meta-error")
    result = session.exec_code(
        "raise ValueError('boom')", metadata={"intent_signature": "m.read.x"}
    )
    assert result.get("isError") is True
    meta = session.session_meta()
    assert meta["exec_count"] == 1
    checkpoint = _load_checkpoint(tmp_path, "meta-error")
    assert checkpoint["session_meta"]["exec_count"] == 1


def test_daemon_evicts_idle_sessions(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EMERGE_SESSION_IDLE_TTL_S", "1")
    from scripts.emerge_daemon import EmergeDaemon

    daemon = EmergeDaemon(root=ROOT)
    daemon._state_root = tmp_path
    daemon._base_session_id = "ttl-base"

    session = daemon._get_session("default")
    assert "__default__" in daemon._sessions_by_profile

    # Rewind the cached session's last-active so it exceeds the TTL.
    session._meta["last_active_at_ms"] = int(time.time() * 1000) - 5_000

    evicted = daemon._evict_idle_sessions()
    assert evicted == ["__default__"]
    assert "__default__" not in daemon._sessions_by_profile


def test_daemon_rehydrates_evicted_session_from_disk(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EMERGE_SESSION_IDLE_TTL_S", "1")
    from scripts.emerge_daemon import EmergeDaemon

    daemon = EmergeDaemon(root=ROOT)
    daemon._state_root = tmp_path
    daemon._base_session_id = "ttl-rehydrate"

    first = daemon._get_session("default")
    first.exec_code("seed_value = 42", metadata={"intent_signature": "m.read.x"})
    first._meta["last_active_at_ms"] = int(time.time() * 1000) - 5_000

    # Second lookup should evict + build a new in-memory session, but WAL replay
    # must restore the `seed_value` binding from disk.
    second = daemon._get_session("default")
    assert second is not first
    # Force a second exec to demonstrate replay restored the global.
    result = second.exec_code(
        "print(seed_value)", metadata={"intent_signature": "m.read.x"}
    )
    assert "42" in result["content"][0]["text"]


def test_daemon_skips_eviction_when_ttl_disabled(tmp_path: Path, monkeypatch) -> None:
    # 0 or negative disables eviction entirely.
    monkeypatch.setenv("EMERGE_SESSION_IDLE_TTL_S", "0")
    from scripts.emerge_daemon import EmergeDaemon

    daemon = EmergeDaemon(root=ROOT)
    daemon._state_root = tmp_path
    daemon._base_session_id = "ttl-disabled"

    session = daemon._get_session("default")
    session._meta["last_active_at_ms"] = 1  # arbitrarily old
    assert daemon._evict_idle_sessions() == []
    assert "__default__" in daemon._sessions_by_profile


def test_bounded_buffer_reports_exact_truncation(tmp_path: Path) -> None:
    from scripts.exec_session import _BoundedBuffer

    buf = _BoundedBuffer(10)
    buf.write("abcdefghij")  # exactly 10 bytes
    buf.write("klmno")  # 5 bytes dropped
    assert buf.getvalue() == "abcdefghij"
    assert buf.truncated_bytes == 5
    assert buf.written_bytes == 10
