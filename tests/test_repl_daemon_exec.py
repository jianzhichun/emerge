from pathlib import Path
import os
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.repl_daemon import ReplDaemon


def test_icc_exec_persists_variables_across_calls():
    daemon = ReplDaemon(root=ROOT)
    first = daemon.call_tool("icc_exec", {"code": "x = 41\nprint('set')"})
    assert first.get("isError") is not True
    second = daemon.call_tool("icc_exec", {"code": "print(x + 1)"})
    text = second["content"][0]["text"]
    assert "42" in text


def test_icc_exec_returns_explicit_error_payload():
    daemon = ReplDaemon(root=ROOT)
    result = daemon.call_tool("icc_exec", {"code": "raise ValueError('boom')"})
    assert result["isError"] is True
    assert "ValueError" in result["content"][0]["text"]


def test_icc_exec_restores_state_after_daemon_restart(tmp_path: Path):
    os.environ["REPL_STATE_ROOT"] = str(tmp_path)
    os.environ["REPL_SESSION_ID"] = "session-a"
    try:
        daemon1 = ReplDaemon(root=ROOT)
        first = daemon1.call_tool("icc_exec", {"code": "x = 99\nprint('saved')"})
        assert first.get("isError") is not True

        daemon2 = ReplDaemon(root=ROOT)
        second = daemon2.call_tool("icc_exec", {"code": "print(x + 1)"})
        assert "100" in second["content"][0]["text"]
    finally:
        os.environ.pop("REPL_STATE_ROOT", None)
        os.environ.pop("REPL_SESSION_ID", None)


def test_default_session_id_is_project_scoped_not_literal_default(tmp_path: Path):
    os.environ["REPL_STATE_ROOT"] = str(tmp_path)
    os.environ.pop("REPL_SESSION_ID", None)
    try:
        daemon = ReplDaemon(root=ROOT)
        daemon.call_tool("icc_exec", {"code": "x = 1"})
        dirs = [p.name for p in tmp_path.iterdir() if p.is_dir()]
        assert dirs
        assert "default" not in dirs
        assert any(name.startswith("emerge-") for name in dirs)
    finally:
        os.environ.pop("REPL_STATE_ROOT", None)


def test_wal_replay_tolerates_broken_entries(tmp_path: Path):
    os.environ["REPL_STATE_ROOT"] = str(tmp_path)
    os.environ["REPL_SESSION_ID"] = "recover"
    try:
        daemon1 = ReplDaemon(root=ROOT)
        daemon1.call_tool("icc_exec", {"code": "x = 7"})
        session_dir = tmp_path / "recover"
        wal = session_dir / "wal.jsonl"
        wal.write_text(
            wal.read_text(encoding="utf-8")
            + '{"seq": 999, "status":"success","code":"raise RuntimeError(\\"bad replay\\")"}\n',
            encoding="utf-8",
        )

        daemon2 = ReplDaemon(root=ROOT)
        out = daemon2.call_tool("icc_exec", {"code": "print(x)"})
        assert out.get("isError") is not True
        assert "7" in out["content"][0]["text"]
        recovery = session_dir / "recovery.json"
        assert recovery.exists()
    finally:
        os.environ.pop("REPL_STATE_ROOT", None)
        os.environ.pop("REPL_SESSION_ID", None)
