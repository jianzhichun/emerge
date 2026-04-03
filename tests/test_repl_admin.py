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


def test_repl_admin_policy_status_handles_corrupt_registry(tmp_path: Path):
    env = os.environ.copy()
    env["REPL_STATE_ROOT"] = str(tmp_path)
    env["REPL_SESSION_ID"] = "corrupt"
    session_dir = tmp_path / "corrupt"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "pipelines-registry.json").write_text("{bad json", encoding="utf-8")
    out = _run_admin(["policy-status"], env)
    assert out["registry_exists"] is True
    assert out["registry_corrupt"] is True
    assert out["pipeline_count"] == 0


def test_repl_admin_policy_status_includes_policy_execution_metrics(tmp_path: Path):
    env = os.environ.copy()
    env["REPL_STATE_ROOT"] = str(tmp_path)
    env["REPL_SESSION_ID"] = "policy-exec"

    os.environ["REPL_STATE_ROOT"] = str(tmp_path)
    os.environ["REPL_SESSION_ID"] = "policy-exec"
    try:
        daemon = ReplDaemon(root=ROOT)
        daemon.call_tool("icc_write", {"connector": "mock", "pipeline": "add-wall", "length": 0})
        daemon.call_tool(
            "icc_write", {"connector": "mock", "pipeline": "add-wall-rollback", "length": 800}
        )

        policy = _run_admin(["policy-status"], env)
        by_key = {item["key"]: item for item in policy["pipelines"]}
        stop_key = "pipeline::mock.write.add-wall"
        rb_key = "pipeline::mock.write.add-wall-rollback"
        assert by_key[stop_key]["policy_enforced_count"] >= 1
        assert by_key[stop_key]["stop_triggered_count"] >= 1
        assert by_key[stop_key]["last_policy_action"] == "stop"
        assert by_key[rb_key]["policy_enforced_count"] >= 1
        assert by_key[rb_key]["rollback_executed_count"] >= 1
        assert by_key[rb_key]["last_policy_action"] == "rollback"

        pretty = _run_admin_raw(["policy-status", "--pretty"], env)
        assert "policy_enforced_count" in pretty
        assert "rollback_executed_count" in pretty
    finally:
        os.environ.pop("REPL_STATE_ROOT", None)
        os.environ.pop("REPL_SESSION_ID", None)


def test_repl_admin_policy_status_includes_goal_source_from_hook_state(tmp_path: Path):
    env = os.environ.copy()
    env["REPL_STATE_ROOT"] = str(tmp_path / "repl")
    env["REPL_SESSION_ID"] = "goal-source"
    env["CLAUDE_PLUGIN_DATA"] = str(tmp_path / "hook-state")

    hook_dir = tmp_path / "hook-state"
    hook_dir.mkdir(parents=True, exist_ok=True)
    (hook_dir / "state.json").write_text(
        json.dumps({"goal": "reduce token noise", "goal_source": "hook_payload"}),
        encoding="utf-8",
    )

    out = _run_admin(["policy-status"], env)
    assert out["goal"] == "reduce token noise"
    assert out["goal_source"] == "hook_payload"

    pretty = _run_admin_raw(["policy-status", "--pretty"], env)
    assert "Goal: reduce token noise" in pretty
    assert "Goal source: hook_payload" in pretty


def test_repl_admin_status_supports_target_profile_session_dir(tmp_path: Path):
    env = os.environ.copy()
    env["REPL_STATE_ROOT"] = str(tmp_path)
    env["REPL_SESSION_ID"] = "profiled"
    env["REPL_TARGET_PROFILE"] = "mycader-1.zwcad"

    os.environ["REPL_STATE_ROOT"] = str(tmp_path)
    os.environ["REPL_SESSION_ID"] = "profiled"
    try:
        daemon = ReplDaemon(root=ROOT)
        daemon.call_tool("icc_exec", {"code": "x = 1", "target_profile": "mycader-1.zwcad"})
        status = _run_admin(["status"], env)
        assert status["wal_exists"] is True
        assert "__" in status["session_dir"]
    finally:
        os.environ.pop("REPL_STATE_ROOT", None)
        os.environ.pop("REPL_SESSION_ID", None)
