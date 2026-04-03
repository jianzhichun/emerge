import json
import os
import subprocess
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.repl_daemon import ReplDaemon


def _run_admin(args: list[str], env: dict[str, str]) -> dict:
    proc = subprocess.run(
        ["python3", str(ROOT / "scripts" / "repl_admin.py"), *args],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return json.loads(proc.stdout.strip())


def _run_admin_raw(args: list[str], env: dict[str, str]) -> str:
    proc = subprocess.run(
        ["python3", str(ROOT / "scripts" / "repl_admin.py"), *args],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return proc.stdout


def test_repl_admin_status_and_clear(tmp_path: Path):
    env = os.environ.copy()
    env["REPL_STATE_ROOT"] = str(tmp_path)
    env["REPL_SESSION_ID"] = "admin-session"

    os.environ["REPL_STATE_ROOT"] = str(tmp_path)
    os.environ["REPL_SESSION_ID"] = "admin-session"
    try:
        daemon = ReplDaemon(root=ROOT)
        daemon.call_tool("icc_exec", {"code": "x = 12\nprint(x)"})

        status_before = _run_admin(["status"], env)
        assert status_before["session_id"] == "admin-session"
        assert status_before["wal_entries"] >= 1
        assert status_before["checkpoint_exists"] is True

        clear_out = _run_admin(["clear"], env)
        assert clear_out["cleared"] is True

        status_after = _run_admin(["status"], env)
        assert status_after["wal_entries"] == 0
        assert status_after["checkpoint_exists"] is False
    finally:
        os.environ.pop("REPL_STATE_ROOT", None)
        os.environ.pop("REPL_SESSION_ID", None)


def test_repl_admin_policy_status_reports_pipeline_registry(tmp_path: Path):
    env = os.environ.copy()
    env["REPL_STATE_ROOT"] = str(tmp_path)
    env["REPL_SESSION_ID"] = "admin-session"

    os.environ["REPL_STATE_ROOT"] = str(tmp_path)
    os.environ["REPL_SESSION_ID"] = "admin-session"
    try:
        daemon = ReplDaemon(root=ROOT)
        for _ in range(20):
            daemon.call_tool(
                "icc_exec",
                {
                    "mode": "inline_code",
                    "code": "v = 1",
                    "target_profile": "mycader-1.zwcad",
                    "intent_signature": "zwcad.add_wall",
                    "script_ref": "connectors/cade/actions/zwcad_add_wall.py",
                    "verify_passed": True,
                },
            )

        policy = _run_admin(["policy-status"], env)
        assert policy["session_id"] == "admin-session"
        assert policy["pipeline_count"] >= 1
        assert policy["thresholds"]["promote_min_attempts"] == 20
        keys = [item["key"] for item in policy["pipelines"]]
        assert "mycader-1.zwcad::zwcad.add_wall::connectors/cade/actions/zwcad_add_wall.py" in keys
    finally:
        os.environ.pop("REPL_STATE_ROOT", None)
        os.environ.pop("REPL_SESSION_ID", None)


def test_repl_admin_policy_status_pretty_output(tmp_path: Path):
    env = os.environ.copy()
    env["REPL_STATE_ROOT"] = str(tmp_path)
    env["REPL_SESSION_ID"] = "admin-session"

    os.environ["REPL_STATE_ROOT"] = str(tmp_path)
    os.environ["REPL_SESSION_ID"] = "admin-session"
    try:
        daemon = ReplDaemon(root=ROOT)
        for _ in range(20):
            daemon.call_tool(
                "icc_exec",
                {
                    "mode": "inline_code",
                    "code": "v = 1",
                    "target_profile": "mycader-1.zwcad",
                    "intent_signature": "zwcad.add_wall",
                    "script_ref": "connectors/cade/actions/zwcad_add_wall.py",
                    "verify_passed": True,
                },
            )

        pretty = _run_admin_raw(["policy-status", "--pretty"], env)
        assert "Session:" in pretty
        assert "Thresholds:" in pretty
        assert "Pipelines:" in pretty
        assert "mycader-1.zwcad::zwcad.add_wall::connectors/cade/actions/zwcad_add_wall.py" in pretty
    finally:
        os.environ.pop("REPL_STATE_ROOT", None)
        os.environ.pop("REPL_SESSION_ID", None)


def test_repl_admin_default_root_is_home_emerge(tmp_path: Path):
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env.pop("REPL_STATE_ROOT", None)
    env["REPL_SESSION_ID"] = "default-check"
    out = _run_admin(["status"], env)
    assert out["state_root"] == str(tmp_path / ".emerge" / "repl")
