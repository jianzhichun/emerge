from pathlib import Path
import os
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.emerge_daemon import EmergeDaemon


def test_icc_exec_persists_variables_across_calls():
    daemon = EmergeDaemon(root=ROOT)
    first = daemon.call_tool("icc_exec", {"code": "x = 41\nprint('set')"})
    assert first.get("isError") is not True
    second = daemon.call_tool("icc_exec", {"code": "print(x + 1)"})
    text = second["content"][0]["text"]
    assert "42" in text


def test_icc_exec_returns_explicit_error_payload():
    daemon = EmergeDaemon(root=ROOT)
    result = daemon.call_tool("icc_exec", {"code": "raise ValueError('boom')"})
    assert result["isError"] is True
    assert "ValueError" in result["content"][0]["text"]


def test_icc_exec_restores_state_after_daemon_restart(tmp_path: Path):
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path)
    os.environ["EMERGE_SESSION_ID"] = "session-a"
    try:
        daemon1 = EmergeDaemon(root=ROOT)
        first = daemon1.call_tool("icc_exec", {"code": "x = 99\nprint('saved')"})
        assert first.get("isError") is not True

        daemon2 = EmergeDaemon(root=ROOT)
        second = daemon2.call_tool("icc_exec", {"code": "print(x + 1)"})
        assert "100" in second["content"][0]["text"]
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_default_session_id_is_project_scoped_not_literal_default(tmp_path: Path):
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path)
    os.environ.pop("EMERGE_SESSION_ID", None)
    try:
        daemon = EmergeDaemon(root=ROOT)
        daemon.call_tool("icc_exec", {"code": "x = 1"})
        dirs = [p.name for p in tmp_path.iterdir() if p.is_dir()]
        assert dirs
        assert "default" not in dirs
        assert any(name.startswith("emerge-") for name in dirs)
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)


def test_wal_replay_tolerates_broken_entries(tmp_path: Path):
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path)
    os.environ["EMERGE_SESSION_ID"] = "recover"
    try:
        daemon1 = EmergeDaemon(root=ROOT)
        daemon1.call_tool("icc_exec", {"code": "x = 7"})
        session_dir = tmp_path / "recover"
        wal = session_dir / "wal.jsonl"
        wal.write_text(
            wal.read_text(encoding="utf-8")
            + '{"seq": 999, "status":"success","code":"raise RuntimeError(\\"bad replay\\")"}\n',
            encoding="utf-8",
        )

        daemon2 = EmergeDaemon(root=ROOT)
        out = daemon2.call_tool("icc_exec", {"code": "print(x)"})
        assert out.get("isError") is not True
        assert "7" in out["content"][0]["text"]
        recovery = session_dir / "recovery.json"
        assert recovery.exists()
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_wal_replay_tolerates_invalid_json_lines(tmp_path: Path):
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path)
    os.environ["EMERGE_SESSION_ID"] = "recover-json"
    try:
        daemon1 = EmergeDaemon(root=ROOT)
        daemon1.call_tool("icc_exec", {"code": "x = 8"})
        session_dir = tmp_path / "recover-json"
        wal = session_dir / "wal.jsonl"
        wal.write_text(wal.read_text(encoding="utf-8") + "{not valid json}\n", encoding="utf-8")

        daemon2 = EmergeDaemon(root=ROOT)
        out = daemon2.call_tool("icc_exec", {"code": "print(x)"})
        assert out.get("isError") is not True
        assert "8" in out["content"][0]["text"]
        recovery = session_dir / "recovery.json"
        assert recovery.exists()
        assert "invalid_wal_json" in recovery.read_text(encoding="utf-8")
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_explicit_session_id_is_contained_under_state_root(tmp_path: Path):
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_SESSION_ID"] = "../../etc/passwd"
    try:
        daemon = EmergeDaemon(root=ROOT)
        daemon.call_tool("icc_exec", {"code": "x = 1"})
        dirs = [p for p in (tmp_path / "state").iterdir() if p.is_dir()]
        assert len(dirs) == 1
        assert dirs[0].parent.resolve() == (tmp_path / "state").resolve()
        assert ".." not in dirs[0].name
        assert "/" not in dirs[0].name
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_explicit_dot_session_id_is_sanitized(tmp_path: Path):
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_SESSION_ID"] = "."
    try:
        daemon = EmergeDaemon(root=ROOT)
        daemon.call_tool("icc_exec", {"code": "x = 1"})
        dirs = [p for p in (tmp_path / "state").iterdir() if p.is_dir()]
        assert len(dirs) == 1
        assert dirs[0].name != "."
        assert dirs[0].resolve() != (tmp_path / "state").resolve()
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_corrupt_checkpoint_falls_back_to_wal_replay(tmp_path: Path):
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path)
    os.environ["EMERGE_SESSION_ID"] = "checkpoint-corrupt"
    try:
        daemon1 = EmergeDaemon(root=ROOT)
        daemon1.call_tool("icc_exec", {"code": "x = 21"})
        session_dir = tmp_path / "checkpoint-corrupt"
        checkpoint = session_dir / "checkpoint.json"
        checkpoint.write_text("{bad checkpoint", encoding="utf-8")

        daemon2 = EmergeDaemon(root=ROOT)
        out = daemon2.call_tool("icc_exec", {"code": "print(x)"})
        assert out.get("isError") is not True
        assert "21" in out["content"][0]["text"]
        recovery = session_dir / "recovery.json"
        assert recovery.exists()
        assert "invalid_checkpoint" in recovery.read_text(encoding="utf-8")
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_exec_success_not_reversed_by_policy_bookkeeping_failure(tmp_path: Path):
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path)
    os.environ["EMERGE_SESSION_ID"] = "bookkeeping"
    try:
        daemon = EmergeDaemon(root=ROOT)

        def _raise(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("registry broken")

        daemon._record_exec_event = _raise  # type: ignore[method-assign]
        out = daemon.call_tool("icc_exec", {"code": "print('ok')"})
        assert out.get("isError") is not True
        assert "ok" in out["content"][0]["text"]
        # Warning goes in a separate content item so content[0] stays parseable
        warning_texts = [item.get("text", "") for item in out["content"][1:]]
        assert any("policy bookkeeping failed: registry broken" in t for t in warning_texts)
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_icc_exec_structured_error_fields(tmp_path: Path):
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path)
    try:
        daemon = EmergeDaemon(root=ROOT)
        result = daemon.call_tool("icc_exec", {"code": "x = undefined_var"})
        assert result.get("isError") is True
        assert "error_class" in result
        assert result["error_class"] == "NameError"
        assert "error_summary" in result
        assert "undefined_var" in result["error_summary"]
        assert "failed_line" in result
        assert isinstance(result["failed_line"], int)
        assert result.get("recovery_suggestion") == "exec"
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
