import json
import os
import socket
import subprocess
import threading
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.emerge_daemon import EmergeDaemon
from scripts import repl_admin
from scripts.remote_runner import RunnerExecutor, RunnerHTTPHandler, ThreadingHTTPServer


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


class _RunnerServer:
    def __init__(self, state_root: Path) -> None:
        self._state_root = state_root
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.url = ""

    def __enter__(self) -> "_RunnerServer":
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        host, port = sock.getsockname()
        sock.close()
        executor = RunnerExecutor(root=ROOT, state_root=self._state_root)
        handler_cls = type("TestRunnerHTTPHandler", (RunnerHTTPHandler,), {"executor": executor})
        self._server = ThreadingHTTPServer((host, port), handler_cls)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.url = f"http://{host}:{port}"
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        assert self._server is not None
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)


def test_repl_admin_status_and_clear(tmp_path: Path):
    env = os.environ.copy()
    env["EMERGE_STATE_ROOT"] = str(tmp_path)
    env["EMERGE_SESSION_ID"] = "admin-session"

    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path)
    os.environ["EMERGE_SESSION_ID"] = "admin-session"
    try:
        daemon = EmergeDaemon(root=ROOT)
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
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_repl_admin_policy_status_reports_pipeline_registry(tmp_path: Path):
    env = os.environ.copy()
    env["EMERGE_STATE_ROOT"] = str(tmp_path)
    env["EMERGE_SESSION_ID"] = "admin-session"

    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path)
    os.environ["EMERGE_SESSION_ID"] = "admin-session"
    try:
        daemon = EmergeDaemon(root=ROOT)
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
        assert "zwcad.add_wall" in keys
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_repl_admin_policy_status_pretty_output(tmp_path: Path):
    env = os.environ.copy()
    env["EMERGE_STATE_ROOT"] = str(tmp_path)
    env["EMERGE_SESSION_ID"] = "admin-session"

    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path)
    os.environ["EMERGE_SESSION_ID"] = "admin-session"
    try:
        daemon = EmergeDaemon(root=ROOT)
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
        assert "zwcad.add_wall" in pretty
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_repl_admin_default_root_is_home_emerge(tmp_path: Path):
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env.pop("EMERGE_STATE_ROOT", None)
    env["EMERGE_SESSION_ID"] = "default-check"
    out = _run_admin(["status"], env)
    assert out["state_root"] == str(tmp_path / ".emerge" / "repl")


def test_repl_admin_policy_status_handles_corrupt_registry(tmp_path: Path):
    env = os.environ.copy()
    env["EMERGE_STATE_ROOT"] = str(tmp_path)
    env["EMERGE_SESSION_ID"] = "corrupt"
    # registry lives at state_root level (not under session_dir)
    (tmp_path / "pipelines-registry.json").write_text("{bad json", encoding="utf-8")
    out = _run_admin(["policy-status"], env)
    assert out["registry_exists"] is True
    assert out["registry_corrupt"] is True
    assert out["pipeline_count"] == 0


def test_repl_admin_policy_status_includes_policy_execution_metrics(tmp_path: Path):
    env = os.environ.copy()
    env["EMERGE_STATE_ROOT"] = str(tmp_path)
    env["EMERGE_SESSION_ID"] = "policy-exec"

    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path)
    os.environ["EMERGE_SESSION_ID"] = "policy-exec"
    try:
        daemon = EmergeDaemon(root=ROOT)
        daemon.call_tool("icc_write", {"connector": "mock", "pipeline": "add-wall", "length": 0})
        daemon.call_tool(
            "icc_write", {"connector": "mock", "pipeline": "add-wall-rollback", "length": 800}
        )

        policy = _run_admin(["policy-status"], env)
        by_key = {item["key"]: item for item in policy["pipelines"]}
        stop_key = "mock.write.add-wall"
        rb_key = "mock.write.add-wall-rollback"
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
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_repl_admin_policy_status_includes_goal_source_from_hook_state(tmp_path: Path):
    env = os.environ.copy()
    env["EMERGE_STATE_ROOT"] = str(tmp_path / "repl")
    env["EMERGE_SESSION_ID"] = "goal-source"
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
    env["EMERGE_STATE_ROOT"] = str(tmp_path)
    env["EMERGE_SESSION_ID"] = "profiled"
    env["EMERGE_TARGET_PROFILE"] = "mycader-1.zwcad"
    # Isolate from global runner config so exec runs locally and writes local WAL
    empty_runner_cfg = tmp_path / "runner-map.json"
    env["EMERGE_RUNNER_CONFIG_PATH"] = str(empty_runner_cfg)

    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path)
    os.environ["EMERGE_SESSION_ID"] = "profiled"
    os.environ["EMERGE_RUNNER_CONFIG_PATH"] = str(empty_runner_cfg)
    try:
        daemon = EmergeDaemon(root=ROOT)
        daemon.call_tool("icc_exec", {"code": "x = 1", "target_profile": "mycader-1.zwcad"})
        status = _run_admin(["status"], env)
        assert status["wal_exists"] is True
        assert "__" in status["session_dir"]
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)
        os.environ.pop("EMERGE_RUNNER_CONFIG_PATH", None)


def test_repl_admin_runner_status_reports_unconfigured_and_reachable(tmp_path: Path):
    env = os.environ.copy()
    env.pop("EMERGE_RUNNER_URL", None)
    env["EMERGE_RUNNER_CONFIG_PATH"] = str(tmp_path / "runner-map.json")
    out = _run_admin(["runner-status"], env)
    assert out["runner_configured"] is False
    assert out["runner_reachable"] is False

    with _RunnerServer(tmp_path / "runner-state") as runner:
        env2 = os.environ.copy()
        env2["EMERGE_RUNNER_URL"] = runner.url
        env2["EMERGE_RUNNER_CONFIG_PATH"] = str(tmp_path / "runner-map.json")
        status = _run_admin(["runner-status"], env2)
        assert status["runner_configured"] is True
        assert status["runner_reachable"] is True
        assert status["endpoint_count"] >= 1
        assert status["endpoints"][0]["health"]["status"] == "ready"
        pretty = _run_admin_raw(["runner-status", "--pretty"], env2)
        assert "Runner configured: True" in pretty
        assert "Runner reachable: True" in pretty


def test_repl_admin_runner_status_reports_multiple_endpoints(tmp_path: Path):
    with _RunnerServer(tmp_path / "r1") as r1, _RunnerServer(tmp_path / "r2") as r2:
        env = os.environ.copy()
        env["EMERGE_RUNNER_CONFIG_PATH"] = str(tmp_path / "runner-map.json")
        env["EMERGE_RUNNER_MAP"] = json.dumps(
            {
                "mycader-1.zwcad": r1.url,
                "mycader-2.zwcad": r2.url,
            }
        )
        out = _run_admin(["runner-status"], env)
        assert out["runner_configured"] is True
        assert out["endpoint_count"] == 2
        assert out["runner_reachable"] is True


def test_repl_admin_runner_config_set_and_status(tmp_path: Path):
    cfg_path = tmp_path / "runner-map.json"
    env = os.environ.copy()
    env["EMERGE_RUNNER_CONFIG_PATH"] = str(cfg_path)
    set_out = _run_admin(
        [
            "runner-config-set",
            "--runner-key",
            "mycader-1.zwcad",
            "--runner-url",
            "http://127.0.0.1:8787",
        ],
        env,
    )
    assert set_out["updated"] is True
    status = _run_admin(["runner-config-status"], env)
    assert status["exists"] is True
    assert status["map"]["mycader-1.zwcad"] == "http://127.0.0.1:8787"


def test_runner_bootstrap_requires_target_profile():
    with pytest.raises(ValueError):
        repl_admin.cmd_runner_bootstrap(
            ssh_target="user@host",
            target_profile="",
            remote_plugin_root="~/.emerge/plugin",
            runner_host="0.0.0.0",
            runner_port=8787,
            runner_url="http://host:8787",
            python_bin="python3",
            deploy=False,
        )


def test_remote_root_expr_expands_home():
    assert repl_admin._remote_root_expr("~") == "$HOME"
    assert repl_admin._remote_root_expr("~/plugin") == "$HOME/plugin"


def test_runner_bootstrap_shell_commands_quote_remote_root(monkeypatch):
    """SSH shell strings must shlex-quote remote_root to prevent injection."""
    import shlex
    captured: list[list[str]] = []

    def fake_run_checked(command: list[str], *, timeout_s: int = 90) -> str:
        captured.append(command)
        return ""

    monkeypatch.setattr(repl_admin, "_run_checked", fake_run_checked)
    monkeypatch.setattr(
        repl_admin,
        "_probe_runner_health",
        lambda **kwargs: ({"ok": True, "status": "ready"}, ""),
    )
    monkeypatch.setattr(repl_admin, "cmd_runner_config_set", lambda **kwargs: {})

    evil_root = "/tmp/test && echo INJECTED"
    try:
        repl_admin.cmd_runner_bootstrap(
            ssh_target="user@host",
            target_profile="test",
            remote_plugin_root=evil_root,
            runner_host="127.0.0.1",
            runner_port=8787,
            runner_url="http://host:8787",
            python_bin="python3",
            deploy=False,
        )
    except Exception:
        pass  # health or other failures are OK; we only care about command shape

    # All SSH shell strings containing remote_root must have it quoted
    for cmd in captured:
        if cmd and cmd[0] == "ssh":
            shell_str = cmd[-1]
            if evil_root in shell_str or shlex.quote(evil_root) in shell_str:
                # The raw unquoted evil string must NOT appear as a standalone token
                assert "echo INJECTED" not in shell_str or shlex.quote(evil_root) in shell_str


def test_runner_bootstrap_rejects_invalid_port():
    with pytest.raises(ValueError):
        repl_admin.cmd_runner_bootstrap(
            ssh_target="user@host",
            target_profile="mycader-1.zwcad",
            remote_plugin_root="~/.emerge/plugin",
            runner_host="0.0.0.0",
            runner_port=0,
            runner_url="http://host:8787",
            python_bin="python3",
            deploy=False,
        )


def test_runner_bootstrap_sets_route_and_reports_health(tmp_path: Path, monkeypatch):
    cfg_path = tmp_path / "runner-map.json"
    monkeypatch.setenv("EMERGE_RUNNER_CONFIG_PATH", str(cfg_path))

    def fake_run_checked(command: list[str]) -> str:
        if command and command[0] == "ssh" and "echo $!" in command[-1]:
            return "43210"
        return ""

    monkeypatch.setattr(repl_admin, "_run_checked", fake_run_checked)
    calls = {"n": 0}

    def fake_probe_runner_health(**kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            return {}, "runner unreachable"
        return {"ok": True, "service": "emerge-remote-runner", "status": "ready"}, ""

    monkeypatch.setattr(repl_admin, "_probe_runner_health", fake_probe_runner_health)

    out = repl_admin.cmd_runner_bootstrap(
        ssh_target="user@10.0.0.8",
        target_profile="mycader-1.zwcad",
        remote_plugin_root="~/.emerge/plugin",
        runner_host="0.0.0.0",
        runner_port=8787,
        runner_url="http://10.0.0.8:8787",
        python_bin="python3",
        deploy=False,
    )
    assert out["ok"] is True
    assert out["runner_pid"] == "43210"
    assert "runner_health_ok" in out["actions"]
    assert out["config"]["map"]["mycader-1.zwcad"] == "http://10.0.0.8:8787"
    assert out["reused_existing_runner"] is False


def test_runner_bootstrap_reuses_existing_healthy_runner(tmp_path: Path, monkeypatch):
    cfg_path = tmp_path / "runner-map.json"
    monkeypatch.setenv("EMERGE_RUNNER_CONFIG_PATH", str(cfg_path))

    def fake_run_checked(command: list[str], *, timeout_s: int = 90) -> str:
        if command and command[0] == "ssh" and "cat .claude-plugin/plugin.json" in command[-1]:
            return json.dumps({"name": "emerge", "version": repl_admin._local_plugin_version()})
        return ""

    monkeypatch.setattr(repl_admin, "_run_checked", fake_run_checked)
    monkeypatch.setattr(
        repl_admin,
        "_probe_runner_health",
        lambda **kwargs: ({"ok": True, "status": "ready"}, ""),
    )

    out = repl_admin.cmd_runner_bootstrap(
        ssh_target="user@10.0.0.8",
        target_profile="mycader-1.zwcad",
        remote_plugin_root="~/.emerge/plugin",
        runner_host="0.0.0.0",
        runner_port=8787,
        runner_url="http://10.0.0.8:8787",
        python_bin="python3",
        deploy=False,
    )
    assert out["ok"] is True
    assert out["reused_existing_runner"] is True
    assert "runner_already_healthy" in out["actions"]
    assert out["version_match"] is True


def test_runner_bootstrap_blocks_on_version_mismatch_with_running_runner(monkeypatch):
    def fake_run_checked(command: list[str], *, timeout_s: int = 90) -> str:
        if command and command[0] == "ssh" and "cat .claude-plugin/plugin.json" in command[-1]:
            return json.dumps({"name": "emerge", "version": "0.0.1"})
        return ""

    monkeypatch.setattr(repl_admin, "_run_checked", fake_run_checked)
    monkeypatch.setattr(
        repl_admin,
        "_probe_runner_health",
        lambda **kwargs: ({"ok": True, "status": "ready"}, ""),
    )
    with pytest.raises(RuntimeError, match="version mismatches"):
        repl_admin.cmd_runner_bootstrap(
            ssh_target="user@10.0.0.8",
            target_profile="mycader-1.zwcad",
            remote_plugin_root="~/.emerge/plugin",
            runner_host="0.0.0.0",
            runner_port=8787,
            runner_url="http://10.0.0.8:8787",
            python_bin="python3",
            deploy=False,
        )


# ---------------------------------------------------------------------------
# connector-export / connector-import
# ---------------------------------------------------------------------------

def test_connector_export_produces_zip(tmp_path):
    """Export a connector directory into a zip with manifest, files, and registry."""
    import zipfile

    connector_root = tmp_path / "connectors"
    connector_dir = connector_root / "mycon" / "pipelines" / "read"
    connector_dir.mkdir(parents=True)
    (connector_dir / "state.py").write_text("# state")
    (connector_dir / "state.yaml").write_text("pipeline: state")
    # __pycache__ should be excluded
    pycache = connector_dir / "__pycache__"
    pycache.mkdir()
    (pycache / "state.cpython-313.pyc").write_bytes(b"junk")

    state_root = tmp_path / "repl"
    state_root.mkdir()
    (state_root / "pipelines-registry.json").write_text(json.dumps({
        "pipelines": {
            "mycon.read.state": {"status": "explore", "rollout_pct": 0},
            "other.read.state": {"status": "stable", "rollout_pct": 100},
        }
    }))

    out_zip = tmp_path / "mycon-pkg.zip"
    result = repl_admin.cmd_connector_export(
        connector="mycon",
        out=str(out_zip),
        connector_root=connector_root,
        state_root=state_root,
    )

    assert result["ok"] is True
    assert out_zip.exists()

    with zipfile.ZipFile(out_zip, "r") as zf:
        names = zf.namelist()
        assert "manifest.json" in names
        assert "pipelines-registry.json" in names
        assert "connectors/mycon/pipelines/read/state.py" in names
        assert "connectors/mycon/pipelines/read/state.yaml" in names
        assert not any("__pycache__" in n for n in names)
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["name"] == "mycon"
        reg = json.loads(zf.read("pipelines-registry.json"))
        assert "mycon.read.state" in reg["pipelines"]
        assert "other.read.state" not in reg["pipelines"]


def test_connector_export_missing_connector_returns_error(tmp_path):
    """Export returns error dict when connector directory does not exist."""
    connector_root = tmp_path / "connectors"
    connector_root.mkdir()
    state_root = tmp_path / "repl"
    state_root.mkdir()

    result = repl_admin.cmd_connector_export(
        connector="nonexistent",
        out=str(tmp_path / "pkg.zip"),
        connector_root=connector_root,
        state_root=state_root,
    )
    assert result["ok"] is False
    assert "nonexistent" in result["error"]


def _make_pkg(tmp_path: Path, connector: str = "mycon") -> Path:
    """Helper: build a valid connector zip package."""
    src_root = tmp_path / "src_connectors"
    connector_dir = src_root / connector / "pipelines" / "read"
    connector_dir.mkdir(parents=True)
    (connector_dir / "state.py").write_text("# state")
    (connector_dir / "state.yaml").write_text("pipeline: state")

    state_root = tmp_path / "src_repl"
    state_root.mkdir(exist_ok=True)
    (state_root / "pipelines-registry.json").write_text(json.dumps({
        "pipelines": {f"{connector}.read.state": {"status": "explore", "rollout_pct": 0}}
    }))

    out_zip = tmp_path / f"{connector}-pkg.zip"
    repl_admin.cmd_connector_export(
        connector=connector,
        out=str(out_zip),
        connector_root=src_root,
        state_root=state_root,
    )
    return out_zip


def test_connector_import_extracts_files_and_merges_registry(tmp_path):
    """Import unpacks connector files and merges registry entries."""
    pkg = _make_pkg(tmp_path)

    dest_connector_root = tmp_path / "dest_connectors"
    dest_connector_root.mkdir()
    dest_state_root = tmp_path / "dest_repl"
    dest_state_root.mkdir()
    (dest_state_root / "pipelines-registry.json").write_text(json.dumps({"pipelines": {}}))

    result = repl_admin.cmd_connector_import(
        pkg=str(pkg),
        overwrite=False,
        connector_root=dest_connector_root,
        state_root=dest_state_root,
    )

    assert result["ok"] is True
    assert result["connector"] == "mycon"
    assert (dest_connector_root / "mycon" / "pipelines" / "read" / "state.py").exists()
    assert "mycon.read.state" in result["pipelines_merged"]
    assert result["pipelines_skipped"] == []

    reg = json.loads((dest_state_root / "pipelines-registry.json").read_text())
    assert "mycon.read.state" in reg["pipelines"]


def test_connector_import_conflict_no_overwrite_returns_error(tmp_path):
    """Import returns error when connector dir exists and --overwrite not set."""
    pkg = _make_pkg(tmp_path)

    dest_connector_root = tmp_path / "dest_connectors"
    existing = dest_connector_root / "mycon"
    existing.mkdir(parents=True)
    dest_state_root = tmp_path / "dest_repl"
    dest_state_root.mkdir()

    result = repl_admin.cmd_connector_import(
        pkg=str(pkg),
        overwrite=False,
        connector_root=dest_connector_root,
        state_root=dest_state_root,
    )

    assert result["ok"] is False
    assert "overwrite" in result["error"].lower() or "exists" in result["error"].lower()


def test_connector_import_overwrite_replaces_files_and_registry(tmp_path):
    """Import with overwrite=True replaces existing connector and registry entries."""
    pkg = _make_pkg(tmp_path)

    dest_connector_root = tmp_path / "dest_connectors"
    existing_file = dest_connector_root / "mycon" / "pipelines" / "read" / "state.py"
    existing_file.parent.mkdir(parents=True)
    existing_file.write_text("# old")

    dest_state_root = tmp_path / "dest_repl"
    dest_state_root.mkdir()
    (dest_state_root / "pipelines-registry.json").write_text(json.dumps({
        "pipelines": {"mycon.read.state": {"status": "stable", "rollout_pct": 100}}
    }))

    result = repl_admin.cmd_connector_import(
        pkg=str(pkg),
        overwrite=True,
        connector_root=dest_connector_root,
        state_root=dest_state_root,
    )

    assert result["ok"] is True
    assert existing_file.read_text() == "# state"
    reg = json.loads((dest_state_root / "pipelines-registry.json").read_text())
    assert reg["pipelines"]["mycon.read.state"]["status"] == "explore"


def test_cli_connector_export(tmp_path):
    """connector-export sub-command produces a zip via CLI."""
    connector_root = tmp_path / "connectors"
    connector_dir = connector_root / "mycon" / "pipelines" / "read"
    connector_dir.mkdir(parents=True)
    (connector_dir / "state.py").write_text("# state")
    (connector_dir / "state.yaml").write_text("pipeline: state")

    state_root = tmp_path / "repl"
    state_root.mkdir()
    (state_root / "pipelines-registry.json").write_text(json.dumps({"pipelines": {}}))

    out_zip = tmp_path / "mycon-pkg.zip"
    env = {
        **os.environ,
        "EMERGE_CONNECTOR_ROOT": str(connector_root),
        "EMERGE_STATE_ROOT": str(state_root),
    }
    result = _run_admin(
        ["connector-export", "--connector", "mycon", "--out", str(out_zip)],
        env=env,
    )
    assert result["ok"] is True
    assert out_zip.exists()


def test_cli_connector_import(tmp_path):
    """connector-import sub-command extracts files via CLI."""
    src_connector_root = tmp_path / "src_connectors"
    connector_dir = src_connector_root / "mycon" / "pipelines" / "read"
    connector_dir.mkdir(parents=True)
    (connector_dir / "state.py").write_text("# state")
    (connector_dir / "state.yaml").write_text("pipeline: state")
    src_state_root = tmp_path / "src_repl"
    src_state_root.mkdir()
    (src_state_root / "pipelines-registry.json").write_text(json.dumps({
        "pipelines": {"mycon.read.state": {"status": "explore", "rollout_pct": 0}}
    }))
    pkg = tmp_path / "mycon-pkg.zip"
    repl_admin.cmd_connector_export(
        connector="mycon",
        out=str(pkg),
        connector_root=src_connector_root,
        state_root=src_state_root,
    )

    dest_connector_root = tmp_path / "dest_connectors"
    dest_connector_root.mkdir()
    dest_state_root = tmp_path / "dest_repl"
    dest_state_root.mkdir()
    (dest_state_root / "pipelines-registry.json").write_text(json.dumps({"pipelines": {}}))

    env = {
        **os.environ,
        "EMERGE_CONNECTOR_ROOT": str(dest_connector_root),
        "EMERGE_STATE_ROOT": str(dest_state_root),
    }
    result = _run_admin(
        ["connector-import", "--pkg", str(pkg)],
        env=env,
    )
    assert result["ok"] is True
    assert (dest_connector_root / "mycon" / "pipelines" / "read" / "state.py").exists()


def test_enrich_actions_injects_notes_content_for_notes_comment(tmp_path):
    """_enrich_actions enriches notes-comment with current_notes, notes_path, instruction."""
    connector_root = tmp_path / "connectors"
    notes_path = connector_root / "zwcad" / "NOTES.md"
    notes_path.parent.mkdir(parents=True)
    notes_path.write_text("# ZWCAD Notes\nCOM quirk: init order matters.", encoding="utf-8")

    actions = [{"type": "notes-comment", "connector": "zwcad", "comment": "fix the init order section"}]

    import os
    old = os.environ.get("EMERGE_CONNECTOR_ROOT")
    os.environ["EMERGE_CONNECTOR_ROOT"] = str(connector_root)
    try:
        result = repl_admin._enrich_actions(actions)
    finally:
        if old is None:
            os.environ.pop("EMERGE_CONNECTOR_ROOT", None)
        else:
            os.environ["EMERGE_CONNECTOR_ROOT"] = old

    assert len(result) == 1
    a = result[0]
    assert "current_notes" in a
    assert "COM quirk" in a["current_notes"]
    assert "notes_path" in a
    assert "instruction" in a
    assert "do NOT blindly append" in a["instruction"]


def test_enrich_actions_passes_through_non_notes_actions(tmp_path):
    """_enrich_actions leaves non-notes-comment actions unchanged."""
    actions = [
        {"type": "pipeline-promote", "key": "zwcad.read.state"},
        {"type": "pipeline-demote", "key": "zwcad.write.add-wall"},
    ]
    result = repl_admin._enrich_actions(actions)
    assert result == actions


def test_enrich_actions_handles_missing_notes_file(tmp_path):
    """_enrich_actions still enriches even when NOTES.md doesn't exist yet."""
    connector_root = tmp_path / "connectors"
    connector_root.mkdir()
    actions = [{"type": "notes-comment", "connector": "zwcad", "comment": "first note"}]

    import os
    old = os.environ.get("EMERGE_CONNECTOR_ROOT")
    os.environ["EMERGE_CONNECTOR_ROOT"] = str(connector_root)
    try:
        result = repl_admin._enrich_actions(actions)
    finally:
        if old is None:
            os.environ.pop("EMERGE_CONNECTOR_ROOT", None)
        else:
            os.environ["EMERGE_CONNECTOR_ROOT"] = old

    a = result[0]
    assert a["current_notes"] == ""
    assert "instruction" in a
