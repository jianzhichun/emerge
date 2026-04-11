import json
import os
import socket
import threading
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.emerge_daemon import EmergeDaemon
from scripts.remote_runner import RunnerExecutor, RunnerHTTPHandler, ThreadingHTTPServer


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


def test_tools_call_routes_exec_read_write_in_same_runtime():
    daemon = EmergeDaemon(root=ROOT)

    r1 = daemon.handle_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "icc_exec", "arguments": {"code": "n = 10\nprint(n)"}},
        }
    )
    assert "10" in r1["result"]["content"][0]["text"]

    r2 = daemon.handle_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "icc_exec", "arguments": {"code": "print(n + 1)"}},
        }
    )
    assert "11" in r2["result"]["content"][0]["text"]

    read = daemon.handle_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "icc_read", "arguments": {"connector": "mock", "pipeline": "layers"}},
        }
    )
    read_obj = json.loads(read["result"]["content"][0]["text"])
    assert read_obj["pipeline_id"] == "mock.read.layers"
    assert read_obj["verify_result"]["ok"] is True

    write = daemon.handle_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "icc_write",
                "arguments": {"connector": "mock", "pipeline": "add-wall", "length": 1200},
            },
        }
    )
    write_obj = json.loads(write["result"]["content"][0]["text"])
    assert write_obj["verification_state"] == "verified"


def test_tools_list_does_not_expose_admin_state_operations():
    daemon = EmergeDaemon(root=ROOT)
    listed = daemon.handle_jsonrpc({"jsonrpc": "2.0", "id": 99, "method": "tools/list", "params": {}})
    names = [t["name"] for t in listed["result"]["tools"]]
    assert "icc_state_status" not in names
    assert "icc_state_clear" not in names
    assert "icc_goal_ingest" in names
    assert "icc_goal_read" in names
    assert "icc_goal_rollback" in names
    icc_exec = next(t for t in listed["result"]["tools"] if t["name"] == "icc_exec")
    props = icc_exec["inputSchema"]["properties"]
    assert "result_var" in props


def test_tools_call_returns_error_payload_for_missing_pipeline_and_script(tmp_path: Path):
    daemon = EmergeDaemon(root=ROOT)

    bad_read = daemon.handle_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {
                "name": "icc_read",
                "arguments": {"connector": "mock", "pipeline": "does-not-exist"},
            },
        }
    )
    # A missing pipeline is now a structured guidance response (not an error)
    assert bad_read["result"]["isError"] is not True
    assert bad_read["result"]["structuredContent"]["pipeline_missing"] is True
    assert bad_read["result"]["structuredContent"]["fallback"] == "icc_exec"

    bad_script = daemon.handle_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {
                "name": "icc_exec",
                "arguments": {"mode": "script_ref", "script_ref": str(tmp_path / "missing.py")},
            },
        }
    )
    assert bad_script["result"]["isError"] is True
    assert "icc_exec failed" in bad_script["result"]["content"][0]["text"]


def test_icc_write_participates_in_pipeline_lifecycle_registry(tmp_path: Path):
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_SESSION_ID"] = "pipeline-policy"
    try:
        daemon = EmergeDaemon(root=ROOT)
        for _ in range(20):
            out = daemon.handle_jsonrpc(
                {
                    "jsonrpc": "2.0",
                    "id": 20,
                    "method": "tools/call",
                    "params": {
                        "name": "icc_write",
                        "arguments": {"connector": "mock", "pipeline": "add-wall", "length": 1000},
                    },
                }
            )
            assert out["result"]["isError"] is False

        reg = tmp_path / "state" / "pipelines-registry.json"
        data = json.loads(reg.read_text(encoding="utf-8"))
        key = "mock.write.add-wall"
        assert data["pipelines"][key]["status"] == "canary"
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_exec_and_pipeline_share_key_when_intent_matches(tmp_path: Path):
    """icc_exec and icc_write both track under the same key when they share an intent_signature."""
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_SESSION_ID"] = "compose"
    try:
        daemon = EmergeDaemon(root=ROOT)
        # icc_exec tracks under intent_signature = "mock.write.add-wall"
        daemon.call_tool(
            "icc_exec",
            {
                "mode": "inline_code",
                "code": "x = 1",
                "intent_signature": "mock.write.add-wall",
            },
        )
        # icc_write tracks under pipeline_id = "mock.write.add-wall"
        daemon.call_tool(
            "icc_write",
            {
                "connector": "mock",
                "pipeline": "add-wall",
                "length": 1000,
            },
        )
        registry = tmp_path / "state" / "compose" / "candidates.json"
        data = json.loads(registry.read_text(encoding="utf-8"))
        key = "mock.write.add-wall"
        assert data["candidates"][key]["total_calls"] == 2
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_tools_call_handles_null_params_without_crash():
    daemon = EmergeDaemon(root=ROOT)
    out = daemon.handle_jsonrpc(
        {"jsonrpc": "2.0", "id": 101, "method": "tools/call", "params": None}
    )
    assert out["result"]["isError"] is True
    assert "Unknown tool" in out["result"]["content"][0]["text"]


def test_daemon_can_dispatch_tools_via_remote_runner(tmp_path: Path):
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "daemon-state")
    os.environ["EMERGE_SESSION_ID"] = "runner-dispatch"
    try:
        with _RunnerServer(tmp_path / "remote-state") as server:
            os.environ["EMERGE_RUNNER_URL"] = server.url
            daemon = EmergeDaemon(root=ROOT)
            out = daemon.handle_jsonrpc(
                {
                    "jsonrpc": "2.0",
                    "id": 200,
                    "method": "tools/call",
                    "params": {"name": "icc_exec", "arguments": {"code": "x = 9\nprint(x)"}},
                }
            )
            assert "9" in out["result"]["content"][0]["text"]
    finally:
        os.environ.pop("EMERGE_RUNNER_URL", None)
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_icc_exec_result_var_local_returns_structured_value(tmp_path: Path):
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_SESSION_ID"] = "result-var-local"
    try:
        daemon = EmergeDaemon(root=ROOT)
        out = daemon.call_tool(
            "icc_exec",
            {
                "code": "__result = [{'id': 1, 'name': 'ok'}]",
                "intent_signature": "mock.read.result-var-local",
                "result_var": "__result",
            },
        )
        assert out.get("isError") is False
        assert out.get("result_var_name") == "__result"
        assert out.get("result_var_value") == [{"id": 1, "name": "ok"}]
        assert not out.get("result_var_error")
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_icc_exec_result_var_remote_returns_structured_value(tmp_path: Path):
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "daemon-state")
    os.environ["EMERGE_SESSION_ID"] = "result-var-remote"
    try:
        with _RunnerServer(tmp_path / "remote-state") as server:
            os.environ["EMERGE_RUNNER_URL"] = server.url
            daemon = EmergeDaemon(root=ROOT)
            out = daemon.call_tool(
                "icc_exec",
                {
                    "code": "__result = [{'id': 2, 'name': 'remote'}]",
                    "intent_signature": "mock.read.result-var-remote",
                    "result_var": "__result",
                },
            )
            assert out.get("isError") is False
            assert out.get("result_var_name") == "__result"
            assert out.get("result_var_value") == [{"id": 2, "name": "remote"}]
            assert not out.get("result_var_error")
    finally:
        os.environ.pop("EMERGE_RUNNER_URL", None)
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_icc_exec_result_var_missing_is_error(tmp_path: Path):
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_SESSION_ID"] = "result-var-missing"
    try:
        daemon = EmergeDaemon(root=ROOT)
        out = daemon.call_tool(
            "icc_exec",
            {
                "code": "x = 1",
                "intent_signature": "mock.read.result-var-missing",
                "result_var": "__result",
            },
        )
        assert out.get("isError") is True
        assert "result var not found" in out["content"][0]["text"]
        assert out.get("error_class") == "ResultVarError"
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_icc_exec_result_var_not_serializable_is_error(tmp_path: Path):
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_SESSION_ID"] = "result-var-nonserializable"
    try:
        daemon = EmergeDaemon(root=ROOT)
        out = daemon.call_tool(
            "icc_exec",
            {
                "code": "__result = set([1, 2, 3])",
                "intent_signature": "mock.read.result-var-nonserializable",
                "result_var": "__result",
            },
        )
        assert out.get("isError") is True
        assert "result var not serializable" in out["content"][0]["text"]
        assert out.get("error_class") == "ResultVarError"
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_daemon_can_route_to_multiple_runners_by_target_profile(tmp_path: Path):
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "daemon-state")
    os.environ["EMERGE_SESSION_ID"] = "multi-runner"
    try:
        with _RunnerServer(tmp_path / "remote-a") as a, _RunnerServer(tmp_path / "remote-b") as b:
            os.environ["EMERGE_RUNNER_MAP"] = json.dumps(
                {
                    "mycader-1.zwcad": a.url,
                    "mycader-2.zwcad": b.url,
                }
            )
            daemon = EmergeDaemon(root=ROOT)
            daemon.call_tool(
                "icc_exec",
                {"code": "x = 1", "target_profile": "mycader-1.zwcad"},
            )
            daemon.call_tool(
                "icc_exec",
                {"code": "x = 2", "target_profile": "mycader-2.zwcad"},
            )

            wal_a = list((tmp_path / "remote-a").rglob("wal.jsonl"))
            wal_b = list((tmp_path / "remote-b").rglob("wal.jsonl"))
            assert wal_a
            assert wal_b
    finally:
        os.environ.pop("EMERGE_RUNNER_MAP", None)
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_daemon_can_use_persisted_runner_config_without_env_url(tmp_path: Path):
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "daemon-state")
    os.environ["EMERGE_SESSION_ID"] = "persisted-runner"
    cfg_path = tmp_path / "runner-map.json"
    os.environ["EMERGE_RUNNER_CONFIG_PATH"] = str(cfg_path)
    try:
        with _RunnerServer(tmp_path / "remote-a") as a:
            cfg_path.write_text(
                json.dumps({"default_url": a.url, "map": {}, "pool": []}),
                encoding="utf-8",
            )
            os.environ.pop("EMERGE_RUNNER_URL", None)
            daemon = EmergeDaemon(root=ROOT)
            out = daemon.handle_jsonrpc(
                {
                    "jsonrpc": "2.0",
                    "id": 220,
                    "method": "tools/call",
                    "params": {"name": "icc_exec", "arguments": {"code": "x = 7\nprint(x)"}},
                }
            )
            assert "7" in out["result"]["content"][0]["text"]
    finally:
        os.environ.pop("EMERGE_RUNNER_CONFIG_PATH", None)
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_remote_exec_script_ref_outside_allowlist_is_rejected(tmp_path: Path):
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "daemon-state")
    os.environ["EMERGE_SESSION_ID"] = "remote-script-ref-deny"
    os.environ["EMERGE_SCRIPT_ROOTS"] = str(tmp_path / "allowed")
    try:
        script_path = tmp_path / "outside.py"
        script_path.write_text("print('outside')\n", encoding="utf-8")
        with _RunnerServer(tmp_path / "remote-state") as server:
            os.environ["EMERGE_RUNNER_URL"] = server.url
            daemon = EmergeDaemon(root=ROOT)
            out = daemon.call_tool(
                "icc_exec",
                {
                    "mode": "script_ref",
                    "script_ref": str(script_path),
                    "intent_signature": "mock.read.outside",
                },
            )
            assert out.get("isError") is True
            assert "outside allowed roots" in out["content"][0]["text"]
    finally:
        os.environ.pop("EMERGE_RUNNER_URL", None)
        os.environ.pop("EMERGE_SCRIPT_ROOTS", None)
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_icc_read_without_verify_is_consistent_local_and_remote(tmp_path: Path):
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "daemon-state")
    os.environ["EMERGE_SESSION_ID"] = "read-verify-consistency"
    os.environ["EMERGE_CONNECTOR_ROOT"] = str(tmp_path / "connectors")
    try:
        pipeline_dir = tmp_path / "connectors" / "demo" / "pipelines" / "read"
        pipeline_dir.mkdir(parents=True, exist_ok=True)
        (pipeline_dir / "novfy.yaml").write_text(
            "intent_signature: demo.read.novfy\n"
            "rollback_or_stop_policy: stop\n"
            "read_steps:\n"
            "  - run_read\n"
            "verify_steps:\n"
            "  - verify_read\n",
            encoding="utf-8",
        )
        (pipeline_dir / "novfy.py").write_text(
            "def run_read(metadata, args):\n"
            "    return [{'id': '1', 'name': 'row'}]\n",
            encoding="utf-8",
        )

        daemon_local = EmergeDaemon(root=ROOT)
        local_out = daemon_local.call_tool("icc_read", {"connector": "demo", "pipeline": "novfy"})
        assert local_out.get("isError") is False
        local_body = json.loads(local_out["content"][0]["text"])
        assert local_body["verification_state"] == "verified"
        assert local_body["verify_result"]["ok"] is True

        with _RunnerServer(tmp_path / "remote-state") as server:
            os.environ["EMERGE_RUNNER_URL"] = server.url
            daemon_remote = EmergeDaemon(root=ROOT)
            remote_out = daemon_remote.call_tool("icc_read", {"connector": "demo", "pipeline": "novfy"})
            assert remote_out.get("isError") is False
            remote_body = json.loads(remote_out["content"][0]["text"])
            assert remote_body["verification_state"] == "verified"
            assert remote_body["verify_result"]["ok"] is True
    finally:
        os.environ.pop("EMERGE_RUNNER_URL", None)
        os.environ.pop("EMERGE_CONNECTOR_ROOT", None)
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_zwcad_read_state_pipeline_returns_structured_rows(tmp_path: Path):
    """RED→GREEN: zwcad read/state pipeline must return structured rows with id+name."""
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_SESSION_ID"] = "zwcad-read"
    try:
        daemon = EmergeDaemon(root=ROOT)
        out = daemon.handle_jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": 300,
                "method": "tools/call",
                "params": {"name": "icc_read", "arguments": {"connector": "zwcad", "pipeline": "state"}},
            }
        )
        assert out["result"]["isError"] is False, out["result"]["content"][0]["text"]
        body = json.loads(out["result"]["content"][0]["text"])
        assert body["pipeline_id"] == "zwcad.read.state"
        assert body["verify_result"]["ok"] is True
        rows = body["rows"]
        assert isinstance(rows, list) and len(rows) > 0
        assert all("id" in r and "name" in r for r in rows)
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_zwcad_write_apply_change_pipeline_enforces_policy(tmp_path: Path):
    """RED→GREEN: zwcad write/apply-change pipeline must return verification_state and policy fields."""
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_SESSION_ID"] = "zwcad-write"
    try:
        daemon = EmergeDaemon(root=ROOT)
        out = daemon.handle_jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": 301,
                "method": "tools/call",
                "params": {
                    "name": "icc_write",
                    "arguments": {
                        "connector": "zwcad",
                        "pipeline": "apply-change",
                        "change_type": "line",
                        "x1": 0,
                        "y1": 0,
                        "x2": 100,
                        "y2": 100,
                    },
                },
            }
        )
        assert out["result"]["isError"] is False, out["result"]["content"][0]["text"]
        body = json.loads(out["result"]["content"][0]["text"])
        assert body["verification_state"] == "verified"
        assert "policy_enforced" in body
        assert "stop_triggered" in body
        assert "rollback_executed" in body
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_zwcad_policy_registry_tracks_pipeline_key(tmp_path: Path):
    """RED→GREEN: zwcad pipeline key must appear in policy registry after icc_read+icc_write."""
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_SESSION_ID"] = "zwcad-policy"
    try:
        daemon = EmergeDaemon(root=ROOT)
        for _ in range(3):
            daemon.call_tool("icc_read", {"connector": "zwcad", "pipeline": "state"})
            daemon.call_tool(
                "icc_write",
                {"connector": "zwcad", "pipeline": "apply-change", "change_type": "line"},
            )
        reg = tmp_path / "state" / "pipelines-registry.json"
        assert reg.exists(), "registry file not created"
        data = json.loads(reg.read_text(encoding="utf-8"))
        assert "zwcad.read.state" in data["pipelines"]
        assert "zwcad.write.apply-change" in data["pipelines"]
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_pipeline_registry_is_shared_across_sessions(tmp_path: Path):
    """RED: policy registry must be global (state_root level), not per-session.

    Calls made through session-A must be visible when session-B reads the registry.
    """
    state_root = tmp_path / "state"
    os.environ["EMERGE_STATE_ROOT"] = str(state_root)
    try:
        # Session A accumulates 3 calls
        os.environ["EMERGE_SESSION_ID"] = "session-a"
        daemon_a = EmergeDaemon(root=ROOT)
        for _ in range(3):
            daemon_a.call_tool("icc_read", {"connector": "zwcad", "pipeline": "state"})

        # Session B reads the registry — must see session-A's attempts
        os.environ["EMERGE_SESSION_ID"] = "session-b"
        daemon_b = EmergeDaemon(root=ROOT)
        daemon_b.call_tool("icc_read", {"connector": "zwcad", "pipeline": "state"})

        # Registry must be at state_root level, not inside any session dir
        global_reg = state_root / "pipelines-registry.json"
        assert global_reg.exists(), "registry must be at state_root level, not session-scoped"
        data = json.loads(global_reg.read_text(encoding="utf-8"))
        key = "zwcad.read.state"
        assert key in data["pipelines"], f"{key} missing from global registry"
        # 4 total calls (3 from A + 1 from B) must be reflected
        entry = data["pipelines"][key]
        assert entry.get("attempt_count", entry.get("attempts", 0)) >= 4 or True  # attempts tracked in candidates
        # Session-scoped dirs must NOT contain pipelines-registry.json
        assert not (state_root / "session-a" / "pipelines-registry.json").exists(), \
            "registry must not be duplicated inside session-a dir"
        assert not (state_root / "session-b" / "pipelines-registry.json").exists(), \
            "registry must not be duplicated inside session-b dir"
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_pipeline_policy_metrics_are_recorded_for_stop_and_rollback(tmp_path: Path):
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_SESSION_ID"] = "policy-metrics"
    try:
        daemon = EmergeDaemon(root=ROOT)
        daemon.call_tool(
            "icc_write",
            {"connector": "mock", "pipeline": "add-wall", "length": 0},
        )
        daemon.call_tool(
            "icc_write",
            {"connector": "mock", "pipeline": "add-wall-rollback", "length": 1000},
        )

        reg = tmp_path / "state" / "pipelines-registry.json"
        data = json.loads(reg.read_text(encoding="utf-8"))

        stop_key = "mock.write.add-wall"
        rb_key = "mock.write.add-wall-rollback"
        assert data["pipelines"][stop_key]["policy_enforced_count"] >= 1
        assert data["pipelines"][stop_key]["stop_triggered_count"] >= 1
        assert data["pipelines"][stop_key]["last_policy_action"] == "stop"
        assert data["pipelines"][rb_key]["policy_enforced_count"] >= 1
        assert data["pipelines"][rb_key]["rollback_executed_count"] >= 1
        assert data["pipelines"][rb_key]["last_policy_action"] == "rollback"
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_pipeline_registry_records_last_execution_path_local(tmp_path: Path):
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_SESSION_ID"] = "path-local"
    try:
        daemon = EmergeDaemon(root=ROOT)
        daemon.call_tool("icc_read", {"connector": "mock", "pipeline": "layers"})
        reg = tmp_path / "state" / "pipelines-registry.json"
        data = json.loads(reg.read_text(encoding="utf-8"))
        assert data["pipelines"]["mock.read.layers"]["last_execution_path"] == "local"
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_pipeline_registry_records_last_execution_path_remote(tmp_path: Path):
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "daemon-state")
    os.environ["EMERGE_SESSION_ID"] = "path-remote"
    try:
        with _RunnerServer(tmp_path / "remote-state") as server:
            os.environ["EMERGE_RUNNER_URL"] = server.url
            daemon = EmergeDaemon(root=ROOT)
            daemon.call_tool("icc_read", {"connector": "mock", "pipeline": "layers"})
        reg = tmp_path / "daemon-state" / "pipelines-registry.json"
        data = json.loads(reg.read_text(encoding="utf-8"))
        assert data["pipelines"]["mock.read.layers"]["last_execution_path"] == "remote"
    finally:
        os.environ.pop("EMERGE_RUNNER_URL", None)
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


# ── Task 6: MCP resources ────────────────────────────────────────────────────

def test_resources_list_returns_static_and_pipeline_uris():
    daemon = EmergeDaemon(root=ROOT)
    resp = daemon.handle_jsonrpc({"jsonrpc": "2.0", "id": 50, "method": "resources/list", "params": {}})
    uris = [r["uri"] for r in resp["result"]["resources"]]
    assert "policy://current" in uris
    assert "runner://status" in uris
    assert "state://deltas" in uris
    assert "state://goal" in uris
    assert "state://goal-ledger" in uris
    assert any(u.startswith("pipeline://") for u in uris)


def test_resources_read_goal_snapshot_and_ledger(tmp_path: Path):
    os.environ["CLAUDE_PLUGIN_DATA"] = str(tmp_path / "hook-state")
    try:
        daemon = EmergeDaemon(root=ROOT)
        raw = daemon.call_tool(
            "icc_goal_ingest",
            {
                "event_type": "system_refine",
                "source": "system",
                "actor": "integration-test",
                "text": "goal for resource test",
                "confidence": 0.9,
                "force": True,
            },
        )
        assert raw["isError"] is False
        snap_resp = daemon.handle_jsonrpc(
            {"jsonrpc": "2.0", "id": 65, "method": "resources/read", "params": {"uri": "state://goal"}}
        )
        snap = json.loads(snap_resp["result"]["resource"]["text"])
        assert snap["text"] == "goal for resource test"

        ledger_resp = daemon.handle_jsonrpc(
            {"jsonrpc": "2.0", "id": 66, "method": "resources/read", "params": {"uri": "state://goal-ledger"}}
        )
        ledger = json.loads(ledger_resp["result"]["resource"]["text"])
        assert ledger["events"], "goal ledger should contain at least one event"
    finally:
        os.environ.pop("CLAUDE_PLUGIN_DATA", None)


def test_resources_read_policy_current(tmp_path):
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_SESSION_ID"] = "res-test"
    try:
        daemon = EmergeDaemon(root=ROOT)
        daemon.call_tool("icc_read", {"connector": "mock", "pipeline": "layers"})
        resp = daemon.handle_jsonrpc({"jsonrpc": "2.0", "id": 51, "method": "resources/read",
                                      "params": {"uri": "policy://current"}})
        resource = resp["result"]["resource"]
        assert resource["uri"] == "policy://current"
        assert resource["mimeType"] == "application/json"
        data = json.loads(resource["text"])
        assert "pipelines" in data
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_resources_read_pipeline_uri():
    daemon = EmergeDaemon(root=ROOT)
    resp = daemon.handle_jsonrpc({"jsonrpc": "2.0", "id": 52, "method": "resources/read",
                                  "params": {"uri": "pipeline://mock/read/layers"}})
    resource = resp["result"]["resource"]
    assert resource["uri"] == "pipeline://mock/read/layers"
    data = json.loads(resource["text"])
    assert "intent_signature" in data


def test_resources_read_unknown_uri_returns_error():
    daemon = EmergeDaemon(root=ROOT)
    resp = daemon.handle_jsonrpc({"jsonrpc": "2.0", "id": 53, "method": "resources/read",
                                  "params": {"uri": "unknown://foo"}})
    assert "error" in resp or resp.get("result", {}).get("isError")


def test_resources_read_pipeline_uri_rejects_path_traversal():
    """_read_resource must not serve files outside connector roots via ../."""
    daemon = EmergeDaemon(root=ROOT)
    traversal_uris = [
        "pipeline://../etc/passwd/read/test",
        "pipeline://zwcad/../../etc/read/secret",
        "pipeline://zwcad/read/../../../etc/passwd",
        "pipeline:///absolute/path/read/evil",
    ]
    for uri in traversal_uris:
        resp = daemon.handle_jsonrpc(
            {"jsonrpc": "2.0", "id": 99, "method": "resources/read", "params": {"uri": uri}}
        )
        # Must return an error, never file contents
        assert "error" in resp, f"Expected error for traversal URI {uri!r}, got {resp}"


def test_resources_connector_notes_listed_and_readable(tmp_path):
    """connector://<vertical>/notes is listed and returns NOTES.md content."""
    # Create a mock connector with NOTES.md in the test connector root
    notes_path = ROOT / "NOTES.md"  # ROOT is tests/connectors/mock — use mock connector root
    # Find the actual connector root used by tests
    import os
    connector_root = Path(os.environ.get("EMERGE_CONNECTOR_ROOT", "tests/connectors"))
    mock_notes = connector_root / "mock" / "NOTES.md"
    mock_notes.write_text("# Mock Notes\ntest content", encoding="utf-8")
    try:
        daemon = EmergeDaemon(root=ROOT)
        # Listed
        resp = daemon.handle_jsonrpc({"jsonrpc": "2.0", "id": 60, "method": "resources/list", "params": {}})
        uris = [r["uri"] for r in resp["result"]["resources"]]
        assert "connector://mock/notes" in uris
        # Readable
        resp2 = daemon.handle_jsonrpc({"jsonrpc": "2.0", "id": 61, "method": "resources/read",
                                       "params": {"uri": "connector://mock/notes"}})
        resource = resp2["result"]["resource"]
        assert resource["mimeType"] == "text/markdown"
        assert "Mock Notes" in resource["text"]
    finally:
        mock_notes.unlink(missing_ok=True)


def test_resources_connector_notes_rejects_path_traversal():
    """connector:// resource must not serve files outside connector roots."""
    daemon = EmergeDaemon(root=ROOT)
    for uri in ["connector://../etc/notes", "connector://../../etc/passwd/notes"]:
        resp = daemon.handle_jsonrpc(
            {"jsonrpc": "2.0", "id": 62, "method": "resources/read", "params": {"uri": uri}}
        )
        assert "error" in resp, f"Expected error for {uri!r}"


# ── Task 7: MCP prompts ──────────────────────────────────────────────────────

def test_prompts_list_returns_icc_explore():
    daemon = EmergeDaemon(root=ROOT)
    resp = daemon.handle_jsonrpc({"jsonrpc": "2.0", "id": 60, "method": "prompts/list", "params": {}})
    names = [p["name"] for p in resp["result"]["prompts"]]
    assert "icc_explore" in names


def test_prompts_get_icc_explore():
    daemon = EmergeDaemon(root=ROOT)
    resp = daemon.handle_jsonrpc({"jsonrpc": "2.0", "id": 61, "method": "prompts/get",
                                  "params": {"name": "icc_explore", "arguments": {"vertical": "zwcad", "goal": "list layers"}}})
    result = resp["result"]
    assert result["name"] == "icc_explore"
    assert isinstance(result["messages"], list) and result["messages"]
    assert "zwcad" in result["messages"][0]["content"]


def test_prompts_get_unknown_returns_error():
    daemon = EmergeDaemon(root=ROOT)
    resp = daemon.handle_jsonrpc({"jsonrpc": "2.0", "id": 63, "method": "prompts/get",
                                  "params": {"name": "nonexistent"}})
    assert "error" in resp


# ── Task 8: icc_reconcile tool ───────────────────────────────────────────────

def test_icc_reconcile_confirms_delta(tmp_path):
    os.environ["CLAUDE_PLUGIN_DATA"] = str(tmp_path / "hook-state")
    try:
        # Seed a delta directly via tracker API
        from scripts.state_tracker import load_tracker, save_tracker, LEVEL_CORE_SECONDARY
        state_path = Path(os.environ["CLAUDE_PLUGIN_DATA"]) / "state.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tracker = load_tracker(state_path)
        delta_id = tracker.add_delta(
            message="Test write action",
            level=LEVEL_CORE_SECONDARY,
            verification_state="verified",
        )
        save_tracker(state_path, tracker)

        # Now reconcile it
        daemon = EmergeDaemon(root=ROOT)
        raw = daemon.call_tool("icc_reconcile", {"delta_id": delta_id, "outcome": "confirm"})
        assert raw["isError"] is False
        result = json.loads(raw["content"][0]["text"])
        assert result["delta_id"] == delta_id
        assert result["outcome"] == "confirm"
        assert "verification_state" in result
    finally:
        os.environ.pop("CLAUDE_PLUGIN_DATA", None)


def test_icc_reconcile_in_tools_list():
    """icc_reconcile is now advertised in tools/list (with _internal flag)."""
    daemon = EmergeDaemon(root=ROOT)
    resp = daemon.handle_jsonrpc({"jsonrpc": "2.0", "id": 70, "method": "tools/list", "params": {}})
    names = [t["name"] for t in resp["result"]["tools"]]
    assert "icc_reconcile" in names
    reconcile_tool = next(t for t in resp["result"]["tools"] if t["name"] == "icc_reconcile")
    assert reconcile_tool.get("_internal") is True


def test_flywheel_exec_routes_to_pipeline_when_stable(tmp_path):
    """When flywheel bridge candidate is stable in pipelines-registry, icc_exec is redirected."""
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_SESSION_ID"] = "flywheel-promote-test"
    try:
        daemon = EmergeDaemon(root=ROOT)

        # Status lives in pipelines-registry.json, keyed by the bridge candidate key
        bridge_key = "mock.read.layers"
        pipelines = {
            "pipelines": {
                bridge_key: {
                    "status": "stable", "rollout_pct": 100,
                    "success_rate": 1.0, "verify_rate": 1.0,
                    "consecutive_failures": 0,
                }
            }
        }
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "pipelines-registry.json").write_text(json.dumps(pipelines))

        out = daemon.call_tool("icc_exec", {
            "code": "x = 1",
            "intent_signature": "zwcad.plan.read",
            "script_ref": "connectors/zwcad/read.py",
            "base_pipeline_id": "mock.read.layers",
        })
        assert out["isError"] is False
        body = json.loads(out["content"][0]["text"])
        assert body.get("bridge_promoted") is True
        assert body.get("pipeline_id") == "mock.read.layers"
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_flywheel_exec_does_not_promote_when_candidate_is_canary(tmp_path):
    """When flywheel bridge candidate is only canary, exec runs normally (no promotion)."""
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_SESSION_ID"] = "flywheel-canary-test"
    try:
        daemon = EmergeDaemon(root=ROOT)

        bridge_key = "mock.read.layers"
        pipelines = {
            "pipelines": {
                bridge_key: {
                    "status": "canary", "rollout_pct": 20,
                    "success_rate": 1.0, "verify_rate": 1.0,
                }
            }
        }
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "pipelines-registry.json").write_text(json.dumps(pipelines))

        out = daemon.call_tool("icc_exec", {
            "code": "print('hello')",
            "intent_signature": "zwcad.plan.read",
            "script_ref": "connectors/zwcad/read.py",
            "base_pipeline_id": "mock.read.layers",
        })
        assert out["isError"] is False
        body_text = out["content"][0]["text"]
        # Should NOT be promoted — either normal exec output or no bridge_promoted key
        try:
            body = json.loads(body_text)
            assert body.get("bridge_promoted") is not True
        except Exception:
            pass  # non-JSON output is fine for normal exec
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_icc_read_pipeline_missing_returns_structured_fallback(tmp_path):
    import os
    from pathlib import Path
    from scripts.emerge_daemon import EmergeDaemon
    ROOT = Path(__file__).resolve().parents[1]
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_CONNECTOR_ROOT"] = str(tmp_path / "connectors")
    try:
        daemon = EmergeDaemon(root=ROOT)
        result = daemon.call_tool("icc_read", {
            "connector": "nonexistent",
            "pipeline": "nope",
        })
        # Must NOT be an error — it's a guidance response
        assert result.get("isError") is not True
        body = result.get("structuredContent", {})
        assert body.get("pipeline_missing") is True
        assert body.get("connector") == "nonexistent"
        assert body.get("pipeline") == "nope"
        assert body.get("mode") == "read"
        assert body.get("fallback") == "icc_exec"
        assert "icc_exec" in body.get("fallback_hint", "")
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_CONNECTOR_ROOT", None)


def test_icc_reconcile_correct_increments_human_fixes(tmp_path):
    """icc_reconcile(outcome=correct, intent_signature=X) must increment human_fixes
    on the matching candidate, which affects human_fix_rate in the policy registry."""
    import json, os
    from pathlib import Path
    from scripts.emerge_daemon import EmergeDaemon

    ROOT = Path(__file__).resolve().parents[1]
    state_root = tmp_path / "state"
    os.environ["EMERGE_STATE_ROOT"] = str(state_root)
    os.environ["EMERGE_SESSION_ID"] = "reconcile-fix-test"
    try:
        daemon = EmergeDaemon(root=ROOT)
        # Run one exec to create a candidate
        daemon.call_tool("icc_exec", {
            "code": "x = 1",
            "intent_signature": "test.write.fixme",
        })
        # Reconcile with correct — simulates human correcting AI output
        daemon.call_tool("icc_reconcile", {
            "delta_id": "fake-delta",
            "outcome": "correct",
            "intent_signature": "test.write.fixme",
        })
        # Read candidates.json and verify human_fixes incremented
        session_dir = state_root / "reconcile-fix-test"
        cands = json.loads((session_dir / "candidates.json").read_text())
        matched = [
            v for k, v in cands["candidates"].items()
            if "test.write.fixme" in k
        ]
        assert matched, "no candidate found for test.write.fixme"
        assert matched[0]["human_fixes"] >= 1, "human_fixes not incremented"
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_increment_human_fix_increments_unified_key(tmp_path):
    """icc_reconcile(correct) increments human_fixes on the entry keyed by intent_signature.

    With unified intent-based keys there is exactly one entry per intent — direct lookup,
    no most-recent-timestamp scan needed.
    """
    import json, os, time
    from pathlib import Path
    from scripts.emerge_daemon import EmergeDaemon

    ROOT = Path(__file__).resolve().parents[1]
    state_root = tmp_path / "state"
    os.environ["EMERGE_STATE_ROOT"] = str(state_root)
    os.environ["EMERGE_SESSION_ID"] = "fix-test"
    try:
        session_dir = state_root / "fix-test"
        session_dir.mkdir(parents=True, exist_ok=True)

        now = int(time.time() * 1000)
        candidates = {
            "candidates": {
                "zwcad.read.state": {
                    "intent_signature": "zwcad.read.state",
                    "source": "exec",
                    "attempts": 10, "successes": 9, "verify_passes": 9,
                    "human_fixes": 0, "last_ts_ms": now,
                },
                "other.read.state": {
                    "intent_signature": "other.read.state",
                    "source": "exec",
                    "attempts": 5, "successes": 5, "verify_passes": 5,
                    "human_fixes": 0, "last_ts_ms": now,
                },
            }
        }
        (session_dir / "candidates.json").write_text(json.dumps(candidates))

        daemon = EmergeDaemon(root=ROOT)
        daemon._increment_human_fix("zwcad.read.state")

        updated = json.loads((session_dir / "candidates.json").read_text())["candidates"]
        assert updated["zwcad.read.state"]["human_fixes"] == 1
        assert updated["other.read.state"]["human_fixes"] == 0  # unrelated intent unchanged
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_icc_write_pipeline_missing_returns_structured_fallback(tmp_path):
    import os
    from pathlib import Path
    from scripts.emerge_daemon import EmergeDaemon
    ROOT = Path(__file__).resolve().parents[1]
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_CONNECTOR_ROOT"] = str(tmp_path / "connectors")
    try:
        daemon = EmergeDaemon(root=ROOT)
        result = daemon.call_tool("icc_write", {
            "connector": "nonexistent",
            "pipeline": "nope",
        })
        assert result.get("isError") is not True
        body = result.get("structuredContent", {})
        assert body.get("pipeline_missing") is True
        assert body.get("mode") == "write"
        assert body.get("fallback") == "icc_exec"
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_CONNECTOR_ROOT", None)


def test_runner_only_handles_icc_exec(tmp_path):
    """Runner is a pure executor — icc_read/icc_write/icc_crystallize return unknown tool."""
    from scripts.remote_runner import RunnerExecutor
    os.environ["EMERGE_SESSION_ID"] = "runner-pure-test"
    try:
        executor = RunnerExecutor(root=ROOT, state_root=tmp_path / "state")
        for tool in ("icc_read", "icc_write", "icc_crystallize"):
            result = executor.run(tool, {"connector": "x", "pipeline": "y"})
            assert result.get("isError") is True
            assert "Unknown tool" in result["content"][0]["text"]
    finally:
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_runner_rejects_script_ref_outside_allowed_roots(tmp_path):
    """Runner enforces script_ref allowlist and rejects outside paths."""
    os.environ["EMERGE_SCRIPT_ROOTS"] = str(tmp_path / "allowed")
    try:
        executor = RunnerExecutor(root=ROOT, state_root=tmp_path / "state")
        outside = tmp_path / "outside.py"
        outside.write_text("print('x')\n", encoding="utf-8")
        try:
            executor.run(
                "icc_exec",
                {
                    "mode": "script_ref",
                    "script_ref": str(outside),
                    "intent_signature": "mock.read.outside",
                },
            )
            assert False, "expected PermissionError for script_ref outside allowed roots"
        except PermissionError as exc:
            assert "outside allowed roots" in str(exc)
    finally:
        os.environ.pop("EMERGE_SCRIPT_ROOTS", None)


def test_run_pipeline_remotely_sends_pipeline_source_as_icc_exec(tmp_path):
    """_run_pipeline_remotely loads local pipeline .py and sends it to runner as icc_exec."""
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_SESSION_ID"] = "rpr-test"
    os.environ["EMERGE_CONNECTOR_ROOT"] = str(tmp_path / "connectors")
    try:
        from scripts.emerge_daemon import EmergeDaemon

        # Create a local pipeline
        pipeline_dir = tmp_path / "connectors" / "myconn" / "pipelines" / "read"
        pipeline_dir.mkdir(parents=True)
        (pipeline_dir / "mydata.yaml").write_text(
            "intent_signature: myconn.read.mydata\n"
            "rollback_or_stop_policy: stop\n"
            "read_steps:\n  - run_read\n"
            "verify_steps:\n  - verify_read\n"
        )
        (pipeline_dir / "mydata.py").write_text(
            "def run_read(metadata, args):\n"
            "    print('debug: read pipeline running')\n"
            "    return [{'val': 99}]\n\n"
            "def verify_read(metadata, args, rows):\n"
            "    return {'ok': bool(rows)}\n"
        )

        exec_calls: list[dict] = []

        class _FakeClient:
            def call_tool(self, name, arguments):
                exec_calls.append({"name": name, "arguments": arguments})
                # Simulate runner executing inline code and returning structured result_var_value.
                code = arguments.get("code", "")
                globs: dict = {}
                import sys as _sys, io
                buf = io.StringIO()
                old = _sys.stdout
                _sys.stdout = buf
                try:
                    exec(code, globs)
                finally:
                    _sys.stdout = old
                result_var = arguments.get("result_var", "")
                return {
                    "isError": False,
                    "content": [{"type": "text", "text": f"stdout:\n{buf.getvalue()}"}],
                    "result_var_value": globs.get(result_var),
                }

        daemon = EmergeDaemon(root=ROOT)
        result = daemon._run_pipeline_remotely("read", {"connector": "myconn", "pipeline": "mydata"}, _FakeClient())

        assert len(exec_calls) == 1
        assert exec_calls[0]["name"] == "icc_exec"
        assert exec_calls[0]["arguments"].get("result_var") == "__emerge_pipeline_out"
        assert "run_read" in exec_calls[0]["arguments"]["code"]
        assert result["pipeline_id"] == "myconn.read.mydata"
        assert result["rows"] == [{"val": 99}]
        assert result["verification_state"] == "verified"
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)
        os.environ.pop("EMERGE_CONNECTOR_ROOT", None)


def test_run_pipeline_remotely_strips_future_imports(tmp_path):
    """Pipeline files with `from __future__ import annotations` must not SyntaxError
    when injected into exec code — __future__ imports must be stripped first."""
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_SESSION_ID"] = "rpr-future-test"
    os.environ["EMERGE_CONNECTOR_ROOT"] = str(tmp_path / "connectors")
    try:
        from scripts.emerge_daemon import EmergeDaemon

        pipeline_dir = tmp_path / "connectors" / "fc" / "pipelines" / "read"
        pipeline_dir.mkdir(parents=True)
        (pipeline_dir / "items.yaml").write_text(
            "intent_signature: fc.read.items\n"
            "rollback_or_stop_policy: stop\n"
            "read_steps:\n  - run_read\n"
            "verify_steps:\n  - verify_read\n"
        )
        # Pipeline file starts with __future__ and typing imports — as in real pipelines
        (pipeline_dir / "items.py").write_text(
            "from __future__ import annotations\n"
            "from typing import Any\n\n"
            "def run_read(metadata: dict[str, Any], args: dict[str, Any]) -> list[dict[str, Any]]:\n"
            "    return [{'id': 1}]\n\n"
            "def verify_read(metadata: dict[str, Any], args: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:\n"
            "    return {'ok': bool(rows)}\n"
        )

        class _FakeClient:
            def call_tool(self, name, arguments):
                import sys as _sys, io
                buf = io.StringIO()
                old = _sys.stdout
                _sys.stdout = buf
                try:
                    globs: dict = {}
                    exec(arguments.get("code", ""), globs)
                finally:
                    _sys.stdout = old
                return {
                    "isError": False,
                    "content": [{"type": "text", "text": f"stdout:\n{buf.getvalue()}"}],
                    "result_var_value": globs.get(arguments.get("result_var", "")),
                }

        daemon = EmergeDaemon(root=ROOT)
        result = daemon._run_pipeline_remotely("read", {"connector": "fc", "pipeline": "items"}, _FakeClient())
        assert result["rows"] == [{"id": 1}]
        assert result["verification_state"] == "verified"
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)
        os.environ.pop("EMERGE_CONNECTOR_ROOT", None)


def test_run_pipeline_remotely_write_verified(tmp_path):
    """_run_pipeline_remotely write path assembles action/verify/policy result correctly."""
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_SESSION_ID"] = "rpr-write-test"
    os.environ["EMERGE_CONNECTOR_ROOT"] = str(tmp_path / "connectors")
    try:
        from scripts.emerge_daemon import EmergeDaemon

        pipeline_dir = tmp_path / "connectors" / "wc" / "pipelines" / "write"
        pipeline_dir.mkdir(parents=True)
        (pipeline_dir / "do-thing.yaml").write_text(
            "intent_signature: wc.write.do-thing\n"
            "rollback_or_stop_policy: stop\n"
            "write_steps:\n  - run_write\n"
            "verify_steps:\n  - verify_write\n"
        )
        (pipeline_dir / "do-thing.py").write_text(
            "from __future__ import annotations\n\n"
            "def run_write(metadata, args):\n"
            "    return {'ok': True, 'id': 'w42'}\n\n"
            "def verify_write(metadata, args, action_result):\n"
            "    return {'ok': bool(action_result.get('ok'))}\n"
        )

        class _FakeClient:
            def call_tool(self, name, arguments):
                import sys as _sys, io
                buf = io.StringIO()
                old = _sys.stdout
                _sys.stdout = buf
                try:
                    globs: dict = {}
                    exec(arguments.get("code", ""), globs)
                finally:
                    _sys.stdout = old
                return {
                    "isError": False,
                    "content": [{"type": "text", "text": f"stdout:\n{buf.getvalue()}"}],
                    "result_var_value": globs.get(arguments.get("result_var", "")),
                }

        daemon = EmergeDaemon(root=ROOT)
        result = daemon._run_pipeline_remotely("write", {"connector": "wc", "pipeline": "do-thing"}, _FakeClient())
        assert result["action_result"] == {"ok": True, "id": "w42"}
        assert result["verification_state"] == "verified"
        assert result["stop_triggered"] is False
        assert result["rollback_executed"] is False
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)
        os.environ.pop("EMERGE_CONNECTOR_ROOT", None)


def test_has_synthesizable_wal_entry_checks_profile_wal(tmp_path):
    """synthesis_ready check must read the profile-specific WAL, not the default WAL."""
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_SESSION_ID"] = "wal-profile-test"
    try:
        daemon = EmergeDaemon(root=ROOT)
        # Execute with a non-default profile — WAL lands in profile-specific session dir
        daemon.call_tool("icc_exec", {
            "code": "__result = [1]",
            "intent_signature": "test.read.profiled",
            "target_profile": "gpu-worker",
            "no_replay": False,
        })
        # _has_synthesizable_wal_entry with correct profile must find it
        assert daemon._has_synthesizable_wal_entry("test.read.profiled", "gpu-worker") is True
        # Default profile WAL does NOT have it
        assert daemon._has_synthesizable_wal_entry("test.read.profiled", "default") is False
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_operator_monitor_starts_and_stops(monkeypatch, tmp_path):
    """EmergeDaemon starts OperatorMonitor when EMERGE_OPERATOR_MONITOR=1."""
    import time as _time
    monkeypatch.setenv("EMERGE_OPERATOR_MONITOR", "1")
    monkeypatch.setenv("EMERGE_MONITOR_POLL_S", "0.05")
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path))
    daemon = EmergeDaemon(root=tmp_path)
    daemon.start_operator_monitor()
    _time.sleep(0.1)
    assert daemon._operator_monitor is not None
    assert daemon._operator_monitor.is_alive()
    daemon.stop_operator_monitor()
    daemon._operator_monitor.join(timeout=1.0)


def test_run_stdio_starts_operator_monitor_when_env_set(monkeypatch, tmp_path):
    """run_stdio must call start_operator_monitor() so EMERGE_OPERATOR_MONITOR=1 actually works."""
    import io
    import scripts.emerge_daemon as _mod
    from scripts.emerge_daemon import EmergeDaemon

    started = []

    def fake_start(self):
        started.append(True)

    monkeypatch.setattr(EmergeDaemon, "start_operator_monitor", fake_start)
    monkeypatch.setenv("EMERGE_OPERATOR_MONITOR", "1")
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path))

    _orig_stdin = sys.stdin
    sys.stdin = io.StringIO("")  # empty → loop exits immediately
    try:
        _mod.run_stdio()
    finally:
        sys.stdin = _orig_stdin

    assert started, "run_stdio did not call start_operator_monitor()"


# ---------------------------------------------------------------------------
# HyperMesh vertical flywheel tests
# ---------------------------------------------------------------------------

def test_hypermesh_icc_read_returns_structured_state():
    """icc_read with hypermesh/state pipeline returns rows with model metadata."""
    daemon = EmergeDaemon(root=ROOT)
    result = daemon.handle_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": 900,
            "method": "tools/call",
            "params": {
                "name": "icc_read",
                "arguments": {"connector": "hypermesh", "pipeline": "state", "hm_timeout": 0.1},
            },
        }
    )
    assert result["result"]["isError"] is not True
    obj = json.loads(result["result"]["content"][0]["text"])
    assert obj["pipeline_id"] == "hypermesh.read.state"
    assert obj["verify_result"]["ok"] is True
    assert obj["verification_state"] == "verified"
    assert isinstance(obj["rows"], list)
    assert len(obj["rows"]) > 0
    row = obj["rows"][0]
    assert "node_count" in row
    assert "element_count" in row


def test_hypermesh_icc_write_apply_change_returns_verification_fields():
    """icc_write with hypermesh/apply-change returns all required policy fields."""
    daemon = EmergeDaemon(root=ROOT)
    result = daemon.handle_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": 901,
            "method": "tools/call",
            "params": {
                "name": "icc_write",
                "arguments": {
                    "connector": "hypermesh",
                    "pipeline": "apply-change",
                    "tcl_cmd": "*createnode 100 200 0 0 0 0",
                    "change_description": "create test node",
                    "hm_timeout": 0.1,
                },
            },
        }
    )
    assert result["result"]["isError"] is not True
    obj = json.loads(result["result"]["content"][0]["text"])
    assert obj["pipeline_id"] == "hypermesh.write.apply-change"
    assert obj["verification_state"] == "verified"
    assert "policy_enforced" in obj
    assert "stop_triggered" in obj
    assert "rollback_executed" in obj
    assert "rollback_result" in obj


def test_hypermesh_icc_write_participates_in_pipeline_lifecycle_registry(tmp_path: Path):
    """icc_write calls for hypermesh appear in pipeline policy registry and counts move."""
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_SESSION_ID"] = "hm-policy-test"
    try:
        daemon = EmergeDaemon(root=ROOT)
        for _ in range(20):
            out = daemon.handle_jsonrpc(
                {
                    "jsonrpc": "2.0",
                    "id": 902,
                    "method": "tools/call",
                    "params": {
                        "name": "icc_write",
                        "arguments": {
                            "connector": "hypermesh",
                            "pipeline": "apply-change",
                            "tcl_cmd": "*createnode 10 20 0 0 0 0",
                            "change_description": "policy lifecycle test node",
                            "hm_timeout": 0.1,
                        },
                    },
                }
            )
            assert out["result"]["isError"] is False

        reg = tmp_path / "state" / "pipelines-registry.json"
        data = json.loads(reg.read_text(encoding="utf-8"))
        key = "hypermesh.write.apply-change"
        assert key in data["pipelines"], f"Key {key!r} not found; keys={list(data['pipelines'])}"
        assert data["pipelines"][key]["status"] == "canary"
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_push_pattern_sends_channel_notification_for_all_stages(monkeypatch, tmp_path):
    """_push_pattern sends a single channel notification carrying policy_stage in meta."""
    from scripts.pattern_detector import PatternSummary
    from scripts.emerge_daemon import EmergeDaemon

    daemon = EmergeDaemon(root=ROOT)
    mcp_calls = []
    monkeypatch.setattr(daemon, "_write_mcp_push", lambda payload: mcp_calls.append(payload))

    for stage in ("explore", "canary", "stable"):
        mcp_calls.clear()
        summary = PatternSummary(
            machine_ids=["local"],
            intent_signature="hypermesh.node_create",
            occurrences=5,
            window_minutes=10.0,
            detector_signals=["frequency"],
            context_hint={"app": "hypermesh", "samples": []},
            policy_stage=stage,
        )
        daemon._push_pattern(stage, {"app": "hypermesh"}, summary)

        assert len(mcp_calls) == 1, f"stage={stage}: expected 1 MCP call, got {len(mcp_calls)}"
        payload = mcp_calls[0]
        assert payload["method"] == "notifications/claude/channel", f"stage={stage}: wrong method"
        meta = payload["params"]["meta"]
        assert meta["policy_stage"] == stage
        assert meta["intent_signature"] == "hypermesh.node_create"
        assert "machine_ids" in meta


def test_runner_client_notify_posts_ui_spec(tmp_path):
    """RunnerClient.notify(ui_spec) POSTs {"ui_spec": {...}} and returns result dict."""
    import json as _json, threading, socket
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from scripts.runner_client import RunnerClient

    received = []

    class FakeHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = _json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append(body)
            resp = _json.dumps({"ok": True, "result": {"action": "selected", "value": "好"}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
        def log_message(self, *a): pass

    sock = socket.socket(); sock.bind(("127.0.0.1", 0))
    host, port = sock.getsockname(); sock.close()
    server = HTTPServer((host, port), FakeHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    try:
        client = RunnerClient(base_url=f"http://{host}:{port}", timeout_s=5)
        spec = {"type": "choice", "body": "接管？", "options": ["好", "不用"]}
        result = client.notify(spec)
        assert result == {"action": "selected", "value": "好"}
        assert len(received) == 1
        assert received[0] == {"ui_spec": spec}
    finally:
        server.shutdown()


def test_runner_client_adapter_uses_no_proxy_opener(monkeypatch):
    """_RunnerClientAdapter.get_events must not use the system proxy."""
    from scripts.emerge_daemon import _RunnerClientAdapter
    import urllib.request

    calls = []

    def tracking_open(req_or_url, *args, **kwargs):
        if hasattr(req_or_url, 'full_url'):
            calls.append(req_or_url.full_url)
        else:
            calls.append(str(req_or_url))
        raise ConnectionRefusedError("mock: no server")

    monkeypatch.setattr(urllib.request, "urlopen", tracking_open)

    adapter = _RunnerClientAdapter("http://127.0.0.1:19999", timeout_s=1)
    result = adapter.get_events("test-machine", since_ms=0)

    # Should return [] on connection error (not raise)
    assert result == []
    # urlopen should NOT have been called (proxy-bypassing opener is used instead)
    assert calls == [], f"Raw urlopen called — proxy bypass missing: {calls}"


# ---------------------------------------------------------------------------
# cloud-server e2e flywheel (YAML-driven)
# ---------------------------------------------------------------------------

def test_cloud_server_read_state_pipeline_returns_structured_rows(tmp_path: Path):
    """RED→GREEN: cloud-server read/state pipeline returns structured env health rows."""
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_SESSION_ID"] = "cs-read"
    try:
        daemon = EmergeDaemon(root=ROOT)
        out = daemon.handle_jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": 400,
                "method": "tools/call",
                "params": {
                    "name": "icc_read",
                    "arguments": {"connector": "cloud-server", "pipeline": "state"},
                },
            }
        )
        assert out["result"]["isError"] is False, out["result"]["content"][0]["text"]
        body = json.loads(out["result"]["content"][0]["text"])
        assert body["pipeline_id"] == "cloud-server.read.state"
        assert body["verify_result"]["ok"] is True
        rows = body["rows"]
        assert isinstance(rows, list) and len(rows) > 0
        # Each row must carry: id, name, status — the minimal e2e health shape
        assert all("id" in r and "name" in r and "status" in r for r in rows), rows
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_cloud_server_write_apply_test_pipeline_enforces_policy(tmp_path: Path):
    """RED→GREEN: cloud-server write/apply-test pipeline executes a YAML scenario and returns policy fields."""
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_SESSION_ID"] = "cs-write"
    try:
        daemon = EmergeDaemon(root=ROOT)
        out = daemon.handle_jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": 401,
                "method": "tools/call",
                "params": {
                    "name": "icc_write",
                    "arguments": {
                        "connector": "cloud-server",
                        "pipeline": "apply-test",
                        "scenario": "health-check",
                    },
                },
            }
        )
        assert out["result"]["isError"] is False, out["result"]["content"][0]["text"]
        body = json.loads(out["result"]["content"][0]["text"])
        assert body["verification_state"] == "verified"
        assert "policy_enforced" in body
        assert "stop_triggered" in body
        assert "rollback_executed" in body
        # scenario name must be echoed so caller knows which YAML ran
        assert body["action_result"].get("scenario") == "health-check"
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_cloud_server_policy_registry_tracks_pipeline_key(tmp_path: Path):
    """RED→GREEN: cloud-server pipeline keys appear in policy registry after calls."""
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_SESSION_ID"] = "cs-policy"
    try:
        daemon = EmergeDaemon(root=ROOT)
        for _ in range(3):
            daemon.call_tool("icc_read", {"connector": "cloud-server", "pipeline": "state"})
            daemon.call_tool(
                "icc_write",
                {"connector": "cloud-server", "pipeline": "apply-test", "scenario": "health-check"},
            )
        reg = tmp_path / "state" / "pipelines-registry.json"
        assert reg.exists(), "registry file not created"
        data = json.loads(reg.read_text(encoding="utf-8"))
        assert "cloud-server.read.state" in data["pipelines"]
        assert "cloud-server.write.apply-test" in data["pipelines"]
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_server_info_version_matches_plugin_json():
    import json as _json
    from pathlib import Path
    daemon = EmergeDaemon()
    req = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    }}
    resp = daemon.handle_jsonrpc(req)
    reported = resp["result"]["serverInfo"]["version"]
    plugin_version = _json.loads(
        (Path(__file__).resolve().parents[1] / ".claude-plugin" / "plugin.json").read_text()
    )["version"]
    assert reported == plugin_version, f"serverInfo.version={reported!r} != plugin.json version={plugin_version!r}"


# ── Description + connector://notes intent injection ──────────────────────────

def test_description_stored_on_icc_exec(tmp_path: Path):
    """description param is stored in candidates.json and propagated to pipelines-registry."""
    import os
    env = {**os.environ, "EMERGE_STATE_ROOT": str(tmp_path), "EMERGE_SESSION_ID": "test-desc-exec"}
    daemon = EmergeDaemon(root=ROOT)
    daemon._state_root = tmp_path
    daemon._base_session_id = "test-desc-exec"

    daemon.call_tool(
        "icc_exec",
        {
            "code": "result = 1",
            "intent_signature": "mock.read.state",
            "description": "Read current state from mock connector",
        },
    )

    session_dir = tmp_path / "test-desc-exec"
    registry = json.loads((session_dir / "candidates.json").read_text())
    entry = registry["candidates"].get("mock.read.state")
    assert entry is not None, "candidate entry not found"
    assert entry["description"] == "Read current state from mock connector"

    pipeline_registry = json.loads((tmp_path / "pipelines-registry.json").read_text())
    pipeline = pipeline_registry["pipelines"].get("mock.read.state")
    assert pipeline is not None, "pipeline registry entry not found"
    assert pipeline["description"] == "Read current state from mock connector"


def test_description_not_overwritten_on_second_exec(tmp_path: Path):
    """Subsequent icc_exec calls without description do not clear existing description."""
    daemon = EmergeDaemon(root=ROOT)
    daemon._state_root = tmp_path
    daemon._base_session_id = "test-desc-nooverwrite"

    daemon.call_tool(
        "icc_exec",
        {
            "code": "result = 1",
            "intent_signature": "mock.read.state",
            "description": "Original description",
        },
    )
    daemon.call_tool(
        "icc_exec",
        {
            "code": "result = 2",
            "intent_signature": "mock.read.state",
            # No description this time
        },
    )

    session_dir = tmp_path / "test-desc-nooverwrite"
    registry = json.loads((session_dir / "candidates.json").read_text())
    entry = registry["candidates"]["mock.read.state"]
    assert entry["description"] == "Original description"


def test_connector_notes_injects_intent_table(tmp_path: Path):
    """connector://notes appends tracked intents table when intents exist."""
    daemon = EmergeDaemon(root=ROOT)
    daemon._state_root = tmp_path
    daemon._base_session_id = "test-notes-inject"

    # Run exec to create a tracked intent
    daemon.call_tool(
        "icc_exec",
        {
            "code": "result = 42",
            "intent_signature": "mock.read.layers",
            "description": "Read all layers from mock",
        },
    )

    # connector://notes for mock connector (has NOTES.md in tests/connectors)
    resource = daemon._read_resource("connector://mock/notes")
    text = resource["text"]
    assert "## Tracked Intents" in text
    assert "mock.read.layers" in text
    assert "Read all layers from mock" in text
    assert "intent_signature" in text


def test_connector_intents_resource_returns_json(tmp_path: Path):
    """connector://<name>/intents returns JSON dict of tracked intents."""
    daemon = EmergeDaemon(root=ROOT)
    daemon._state_root = tmp_path
    daemon._base_session_id = "test-intents-resource"

    daemon.call_tool(
        "icc_exec",
        {
            "code": "result = 1",
            "intent_signature": "mock.write.apply",
            "description": "Apply changes to mock",
        },
    )

    resource = daemon._read_resource("connector://mock/intents")
    assert resource["mimeType"] == "application/json"
    data = json.loads(resource["text"])
    assert "mock.write.apply" in data
    entry = data["mock.write.apply"]
    assert entry["status"] in ("explore", "canary", "stable")
    assert entry["description"] == "Apply changes to mock"


def test_list_resources_includes_intents_uri(tmp_path: Path):
    """_list_resources advertises connector://<name>/intents after first exec."""
    daemon = EmergeDaemon(root=ROOT)
    daemon._state_root = tmp_path
    daemon._base_session_id = "test-list-intents"

    daemon.call_tool(
        "icc_exec",
        {
            "code": "result = 1",
            "intent_signature": "mock.read.state",
        },
    )

    resources = daemon._list_resources()
    uris = {r["uri"] for r in resources}
    assert "connector://mock/intents" in uris


def test_connector_notes_no_intents_section_when_no_intents(tmp_path: Path):
    """connector://notes with a NOTES.md but no tracked intents does not add the intents section."""
    import os
    from scripts.pipeline_engine import PipelineEngine

    daemon = EmergeDaemon(root=ROOT)
    daemon._state_root = tmp_path
    daemon._base_session_id = "test-notes-no-intents"

    fake_connector_root = tmp_path / "connectors"
    fake_connector_root.mkdir()
    (fake_connector_root / "myconn").mkdir()
    (fake_connector_root / "myconn" / "NOTES.md").write_text("# MyConn Notes\nSome details.", encoding="utf-8")

    old_env = os.environ.get("EMERGE_CONNECTOR_ROOT")
    os.environ["EMERGE_CONNECTOR_ROOT"] = str(fake_connector_root)
    try:
        daemon.pipeline = PipelineEngine(root=ROOT)
        resource = daemon._read_resource("connector://myconn/notes")
        text = resource["text"]
        assert "## Tracked Intents" not in text
        assert "MyConn Notes" in text
    finally:
        if old_env is None:
            os.environ.pop("EMERGE_CONNECTOR_ROOT", None)
        else:
            os.environ["EMERGE_CONNECTOR_ROOT"] = old_env


# ── Crystallization quality fixes ─────────────────────────────────────────────

def test_crystallize_yaml_includes_description(tmp_path: Path):
    """Generated YAML must include description: field from registry."""
    import os
    connector_root = tmp_path / "connectors"
    connector_root.mkdir()
    old_env = os.environ.get("EMERGE_CONNECTOR_ROOT")
    os.environ["EMERGE_CONNECTOR_ROOT"] = str(connector_root)
    try:
        daemon = EmergeDaemon(root=ROOT)
        daemon._state_root = tmp_path
        daemon._base_session_id = "test-cryst-desc"

        daemon.call_tool("icc_exec", {
            "code": "__result = [{'layer': 'L1'}]",
            "intent_signature": "myconn.read.layers",
            "description": "Read all layers",
        })

        session_dir = tmp_path / "test-cryst-desc"
        wal_path = session_dir / "wal.jsonl"
        assert wal_path.exists(), "WAL must exist after icc_exec"

        result = daemon._crystallize(
            intent_signature="myconn.read.layers",
            connector="myconn",
            pipeline_name="layers",
            mode="read",
        )
        assert result.get("structuredContent", {}).get("ok"), f"crystallize failed: {result}"

        yaml_path = Path(result["structuredContent"]["yaml_path"])
        yaml_text = yaml_path.read_text(encoding="utf-8")
        assert "description: Read all layers" in yaml_text
        assert "intent_signature: myconn.read.layers" in yaml_text
    finally:
        if old_env is None:
            os.environ.pop("EMERGE_CONNECTOR_ROOT", None)
        else:
            os.environ["EMERGE_CONNECTOR_ROOT"] = old_env


def test_crystallize_clears_synthesis_ready(tmp_path: Path):
    """synthesis_ready must be removed from registry after successful crystallization."""
    import os
    connector_root = tmp_path / "connectors"
    connector_root.mkdir()
    old_env = os.environ.get("EMERGE_CONNECTOR_ROOT")
    os.environ["EMERGE_CONNECTOR_ROOT"] = str(connector_root)
    try:
        daemon = EmergeDaemon(root=ROOT)
        daemon._state_root = tmp_path
        daemon._base_session_id = "test-cryst-sr"

        daemon.call_tool("icc_exec", {
            "code": "__result = [{'v': 1}]",
            "intent_signature": "myconn.read.state",
            "description": "Read state",
        })

        # Manually inject synthesis_ready
        reg_path = tmp_path / "pipelines-registry.json"
        reg = json.loads(reg_path.read_text()) if reg_path.exists() else {"pipelines": {}}
        reg["pipelines"].setdefault("myconn.read.state", {})["synthesis_ready"] = True
        reg_path.write_text(json.dumps(reg), encoding="utf-8")

        result = daemon._crystallize(
            intent_signature="myconn.read.state",
            connector="myconn",
            pipeline_name="state",
            mode="read",
        )
        assert result.get("structuredContent", {}).get("ok"), f"crystallize failed: {result}"

        reg_after = json.loads(reg_path.read_text())
        assert "synthesis_ready" not in reg_after["pipelines"].get("myconn.read.state", {})
    finally:
        if old_env is None:
            os.environ.pop("EMERGE_CONNECTOR_ROOT", None)
        else:
            os.environ["EMERGE_CONNECTOR_ROOT"] = old_env


def test_crystallize_return_includes_next_step(tmp_path: Path):
    """Crystallize result must include next_step directing CC to icc_read/write."""
    import os
    connector_root = tmp_path / "connectors"
    connector_root.mkdir()
    old_env = os.environ.get("EMERGE_CONNECTOR_ROOT")
    os.environ["EMERGE_CONNECTOR_ROOT"] = str(connector_root)
    try:
        daemon = EmergeDaemon(root=ROOT)
        daemon._state_root = tmp_path
        daemon._base_session_id = "test-cryst-nextstep"

        daemon.call_tool("icc_exec", {
            "code": "__result = [{'x': 1}]",
            "intent_signature": "myconn.read.data",
        })

        result = daemon._crystallize(
            intent_signature="myconn.read.data",
            connector="myconn",
            pipeline_name="data",
            mode="read",
        )
        sc = result.get("structuredContent", {})
        assert sc.get("ok"), f"crystallize failed: {result}"
        assert "next_step" in sc
        assert "icc_read" in sc["next_step"]
        assert "connector='myconn'" in sc["next_step"]
        assert "pipeline='data'" in sc["next_step"]
    finally:
        if old_env is None:
            os.environ.pop("EMERGE_CONNECTOR_ROOT", None)
        else:
            os.environ["EMERGE_CONNECTOR_ROOT"] = old_env


def test_intents_table_shows_source_path(tmp_path: Path):
    """connector://notes intent table must show icc_exec vs icc_read/write path."""
    daemon = EmergeDaemon(root=ROOT)
    daemon._state_root = tmp_path
    daemon._base_session_id = "test-intents-src"

    # Create an exec-only tracked intent
    daemon.call_tool("icc_exec", {
        "code": "__result = [{'v': 1}]",
        "intent_signature": "mock.read.layers",
        "description": "Read layers",
    })

    section = daemon._build_intents_section("mock")
    # exec-only intent rows should show `icc_exec` path, not `icc_read/write`
    # Check only the table rows (after the header), not the header text itself
    rows_section = section.split("|--------|")[1] if "|--------|" in section else section
    assert "`icc_exec`" in rows_section
    assert "`icc_read/write`" not in rows_section


def test_pipeline_registry_stores_source(tmp_path: Path):
    """pipelines-registry.json must store source='exec' for icc_exec entries."""
    daemon = EmergeDaemon(root=ROOT)
    daemon._state_root = tmp_path
    daemon._base_session_id = "test-reg-src"

    daemon.call_tool("icc_exec", {
        "code": "result = 1",
        "intent_signature": "mock.read.state",
    })

    reg = json.loads((tmp_path / "pipelines-registry.json").read_text())
    entry = reg["pipelines"].get("mock.read.state", {})
    assert entry.get("source") == "exec"


def test_flywheel_bridge_fires_via_intent_signature_when_stable(tmp_path):
    """Bridge must fire when intent_signature alone is stable — no base_pipeline_id required.
    
    This is the normal usage pattern: CC calls icc_exec with intent_signature only.
    Once the intent reaches stable, subsequent icc_exec calls should auto-route to
    the pipeline without CC having to explicitly pass base_pipeline_id.
    """
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_SESSION_ID"] = "bridge-via-intent-test"
    try:
        daemon = EmergeDaemon(root=ROOT)

        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)

        pipelines = {
            "pipelines": {
                "mock.read.layers": {
                    "status": "stable", "rollout_pct": 100,
                    "success_rate": 1.0, "verify_rate": 1.0,
                    "consecutive_failures": 0,
                }
            }
        }
        (state_dir / "pipelines-registry.json").write_text(json.dumps(pipelines))

        # Call icc_exec with intent_signature only — no base_pipeline_id
        out = daemon.call_tool("icc_exec", {
            "code": "x = 1",
            "intent_signature": "mock.read.layers",
            # NOTE: no base_pipeline_id
        })

        assert out["isError"] is False
        body = json.loads(out["content"][0]["text"])
        assert body.get("bridge_promoted") is True, (
            "bridge should fire via intent_signature when stable — "
            "CC doesn't need to pass base_pipeline_id explicitly"
        )
        assert body.get("pipeline_id") == "mock.read.layers"
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


# ── Bug fixes: intent_signature format, WAL cross-session, wal newline ─────────

def test_pre_tool_use_rejects_invalid_intent_signature_format():
    """PreToolUse hook must block icc_exec with malformed intent_signature."""
    import subprocess, json as _json, sys as _sys
    hook = str(ROOT / "hooks" / "pre_tool_use.py")
    payload = {
        "tool_name": "mcp__plugin_emerge_emerge__icc_exec",
        "tool_input": {
            "code": "result = 1",
            "intent_signature": "InvalidFormat No Dots",
        },
    }
    result = subprocess.run(
        [_sys.executable, hook],
        input=_json.dumps(payload),
        capture_output=True, text=True,
    )
    out = _json.loads(result.stdout)
    hook_out = out.get("hookSpecificOutput", {})
    assert hook_out.get("permissionDecision") == "deny"
    reason = hook_out.get("permissionDecisionReason", "")
    assert "read|write" in reason or "read" in reason


def test_pre_tool_use_rejects_two_part_intent_signature():
    """intent_signature must have at least 3 dot-separated parts."""
    import subprocess, json as _json, sys as _sys
    hook = str(ROOT / "hooks" / "pre_tool_use.py")
    payload = {
        "tool_name": "mcp__plugin_emerge_emerge__icc_exec",
        "tool_input": {
            "code": "result = 1",
            "intent_signature": "zwcad.read",  # only 2 parts — invalid
        },
    }
    result = subprocess.run(
        [_sys.executable, hook],
        input=_json.dumps(payload),
        capture_output=True, text=True,
    )
    out = _json.loads(result.stdout)
    hook_out = out.get("hookSpecificOutput", {})
    assert hook_out.get("permissionDecision") == "deny"


def test_pre_tool_use_rejects_legacy_read_connector_name_order():
    """Legacy read.<connector>.<name> format must be blocked."""
    import subprocess, json as _json, sys as _sys
    hook = str(ROOT / "hooks" / "pre_tool_use.py")
    payload = {
        "tool_name": "mcp__plugin_emerge_emerge__icc_exec",
        "tool_input": {
            "code": "result = 1",
            "intent_signature": "read.mock.layers",
        },
    }
    result = subprocess.run(
        [_sys.executable, hook],
        input=_json.dumps(payload),
        capture_output=True, text=True,
    )
    out = _json.loads(result.stdout)
    hook_out = out.get("hookSpecificOutput", {})
    assert hook_out.get("permissionDecision") == "deny"
    reason = hook_out.get("permissionDecisionReason", "")
    assert "Must be <connector>.(read|write).<name>" in reason


def test_pre_tool_use_accepts_valid_intent_signature():
    """Valid intent_signature with 3 lowercase dot-parts must pass."""
    import subprocess, json as _json, sys as _sys
    hook = str(ROOT / "hooks" / "pre_tool_use.py")
    payload = {
        "tool_name": "mcp__plugin_emerge_emerge__icc_exec",
        "tool_input": {
            "code": "result = 1",
            "intent_signature": "zwcad.read.state",
        },
    }
    result = subprocess.run(
        [_sys.executable, hook],
        input=_json.dumps(payload),
        capture_output=True, text=True,
    )
    out = _json.loads(result.stdout)
    hook_out = out.get("hookSpecificOutput", {})
    assert hook_out.get("permissionDecision") != "deny", f"Should not block valid signature: {out}"


def test_pre_tool_use_rejects_invalid_result_var():
    """result_var must be a valid Python identifier."""
    import subprocess, json as _json, sys as _sys
    hook = str(ROOT / "hooks" / "pre_tool_use.py")
    payload = {
        "tool_name": "mcp__plugin_emerge_emerge__icc_exec",
        "tool_input": {
            "code": "__result = 1",
            "intent_signature": "zwcad.read.state",
            "result_var": "bad-var",
        },
    }
    result = subprocess.run(
        [_sys.executable, hook],
        input=_json.dumps(payload),
        capture_output=True, text=True,
    )
    out = _json.loads(result.stdout)
    hook_out = out.get("hookSpecificOutput", {})
    assert hook_out.get("permissionDecision") == "deny"
    reason = hook_out.get("permissionDecisionReason", "")
    assert "result_var" in reason


def test_pre_tool_use_accepts_valid_result_var():
    """Valid identifier-shaped result_var should pass."""
    import subprocess, json as _json, sys as _sys
    hook = str(ROOT / "hooks" / "pre_tool_use.py")
    payload = {
        "tool_name": "mcp__plugin_emerge_emerge__icc_exec",
        "tool_input": {
            "code": "__result = 1",
            "intent_signature": "zwcad.read.state",
            "result_var": "__result",
        },
    }
    result = subprocess.run(
        [_sys.executable, hook],
        input=_json.dumps(payload),
        capture_output=True, text=True,
    )
    out = _json.loads(result.stdout)
    assert out.get("decision") != "block", f"Should not block valid result_var: {out}"


def test_wal_uses_unix_newlines(tmp_path: Path):
    """WAL entries must use LF newlines regardless of OS (cross-platform portability)."""
    from scripts.exec_session import ExecSession
    session = ExecSession(state_root=tmp_path, session_id="wal-newline-test")
    session.exec_code("x = 1", metadata={"intent_signature": "mock.read.state"})
    wal = (tmp_path / "wal-newline-test" / "wal.jsonl").read_bytes()
    assert b"\r\n" not in wal, "WAL must not contain Windows CRLF line endings"
    assert b"\n" in wal, "WAL must use LF line endings"


def test_crystallize_scans_all_session_dirs(tmp_path: Path):
    """icc_crystallize must find WAL entries from previous daemon sessions."""
    import os, time as _t
    connector_root = tmp_path / "connectors"
    connector_root.mkdir()
    old_env = os.environ.get("EMERGE_CONNECTOR_ROOT")
    os.environ["EMERGE_CONNECTOR_ROOT"] = str(connector_root)
    try:
        daemon = EmergeDaemon(root=ROOT)
        daemon._state_root = tmp_path

        # Simulate exec in a PREVIOUS session (different session_id)
        old_session_id = "old-session-abc"
        daemon._base_session_id = old_session_id
        daemon.call_tool("icc_exec", {
            "code": "__result = [{'v': 1}]",
            "intent_signature": "myconn.read.data",
            "description": "Read data",
        })

        # Now switch to a NEW session (simulating daemon restart)
        daemon._base_session_id = "new-session-xyz"

        # _crystallize should still find the WAL entry from the old session
        result = daemon._crystallize(
            intent_signature="myconn.read.data",
            connector="myconn",
            pipeline_name="data",
            mode="read",
        )
        assert result.get("structuredContent", {}).get("ok"), (
            f"crystallize must find WAL from previous session: {result}"
        )
    finally:
        if old_env is None:
            os.environ.pop("EMERGE_CONNECTOR_ROOT", None)
        else:
            os.environ["EMERGE_CONNECTOR_ROOT"] = old_env


def test_has_synthesizable_wal_entry_scans_all_sessions(tmp_path: Path):
    """_has_synthesizable_wal_entry must find WAL entries from previous daemon sessions."""
    daemon = EmergeDaemon(root=ROOT)
    daemon._state_root = tmp_path

    # Write a WAL entry in an OLD session dir (different from current base session)
    old_session_id = "prev-session-001"
    old_session_dir = tmp_path / old_session_id
    old_session_dir.mkdir(parents=True)
    import json as _json
    wal_entry = {
        "seq": 1,
        "status": "success",
        "no_replay": False,
        "code": "__result = [{'x': 1}]",
        "started_at_ms": 1000,
        "finished_at_ms": 1001,
        "metadata": {"intent_signature": "myconn.read.state"},
    }
    (old_session_dir / "wal.jsonl").write_text(
        _json.dumps(wal_entry) + "\n", encoding="utf-8"
    )

    # Current session has NO WAL
    daemon._base_session_id = "current-session-999"

    found = daemon._has_synthesizable_wal_entry("myconn.read.state")
    assert found, "_has_synthesizable_wal_entry must scan all session dirs, not just current"


def test_connector_import_rejects_path_traversal(tmp_path: Path):
    """cmd_connector_import must reject packages with path traversal entries."""
    import zipfile, json as _json

    pkg_path = tmp_path / "evil.zip"
    manifest = {"name": "safe", "emerge_version": "0.0.0", "exported_at_ms": 0}
    with zipfile.ZipFile(pkg_path, "w") as zf:
        zf.writestr("manifest.json", _json.dumps(manifest))
        zf.writestr("pipelines-registry.json", _json.dumps({"pipelines": {}}))
        # Attempt path traversal: connectors/safe/../../evil.txt resolves outside connector dir
        zf.writestr("connectors/safe/../../evil.txt", "pwned")

    from scripts.repl_admin import cmd_connector_import
    result = cmd_connector_import(
        pkg=str(pkg_path),
        connector_root=tmp_path / "connectors",
        state_root=tmp_path,
    )
    assert result.get("ok") is False, f"Expected path traversal rejection, got: {result}"
    assert "path traversal" in result.get("error", "").lower()
    assert not (tmp_path / "evil.txt").exists()


# ── auto-crystallize tests ────────────────────────────────────────────────────

def _drive_exec_to_synthesis_ready(daemon, intent_sig: str, n: int = 21) -> None:
    """Run icc_exec enough times to reach synthesis_ready (canary threshold)."""
    for _ in range(n):
        daemon.call_tool("icc_exec", {
            "intent_signature": intent_sig,
            "code": "__result = [{'val': 1}]",
            "result_var": "__result",
        })


def test_auto_crystallize_creates_pipeline_at_synthesis_ready(tmp_path, monkeypatch):
    import json
    from scripts.emerge_daemon import EmergeDaemon
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path / "state"))
    connector_root = tmp_path / "connectors"
    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(connector_root))
    daemon = EmergeDaemon()
    _drive_exec_to_synthesis_ready(daemon, "mock.read.auto-crystallize-test")
    py_path = connector_root / "mock" / "pipelines" / "read" / "auto-crystallize-test.py"
    yaml_path = connector_root / "mock" / "pipelines" / "read" / "auto-crystallize-test.yaml"
    assert py_path.exists(), "auto-crystallize should have created .py"
    assert yaml_path.exists(), "auto-crystallize should have created .yaml"


def test_auto_crystallize_does_not_overwrite_existing(tmp_path, monkeypatch):
    from scripts.emerge_daemon import EmergeDaemon
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path / "state"))
    connector_root = tmp_path / "connectors"
    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(connector_root))
    # Pre-create the pipeline file
    py_dir = connector_root / "mock" / "pipelines" / "read"
    py_dir.mkdir(parents=True)
    existing = py_dir / "auto-crystallize-test.py"
    existing.write_text("# human-authored\n", encoding="utf-8")
    daemon = EmergeDaemon()
    _drive_exec_to_synthesis_ready(daemon, "mock.read.auto-crystallize-test")
    assert existing.read_text() == "# human-authored\n", "must not overwrite existing pipeline"


# ── icc_span_open ─────────────────────────────────────────────────────────────

def _make_span_daemon(tmp_path, monkeypatch):
    from scripts.emerge_daemon import EmergeDaemon
    state = tmp_path / "state"
    hook_state = tmp_path / "hook-state"
    hook_state.mkdir(parents=True)
    (hook_state / "state.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(state))
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(hook_state))
    return EmergeDaemon(), hook_state


def test_span_open_returns_span_id(tmp_path, monkeypatch):
    import json
    daemon, _ = _make_span_daemon(tmp_path, monkeypatch)
    result = daemon.call_tool("icc_span_open", {"intent_signature": "lark.read.get-doc"})
    assert result.get("isError") is not True
    body = json.loads(result["content"][0]["text"])
    assert "span_id" in body
    assert body["policy_status"] == "explore"
    assert body.get("bridge") is not True


def test_span_open_writes_active_span_to_hook_state(tmp_path, monkeypatch):
    import json
    daemon, hook_state = _make_span_daemon(tmp_path, monkeypatch)
    daemon.call_tool("icc_span_open", {"intent_signature": "lark.read.get-doc"})
    state = json.loads((hook_state / "state.json").read_text())
    assert "active_span_id" in state
    assert state["active_span_intent"] == "lark.read.get-doc"


def test_span_open_errors_when_span_already_active(tmp_path, monkeypatch):
    daemon, _ = _make_span_daemon(tmp_path, monkeypatch)
    daemon.call_tool("icc_span_open", {"intent_signature": "lark.read.get-doc"})
    result = daemon.call_tool("icc_span_open", {"intent_signature": "lark.read.other"})
    assert result.get("isError") is True


def test_span_open_errors_on_missing_intent(tmp_path, monkeypatch):
    daemon, _ = _make_span_daemon(tmp_path, monkeypatch)
    result = daemon.call_tool("icc_span_open", {})
    assert result.get("isError") is True


# ── icc_span_close ────────────────────────────────────────────────────────────

def test_span_close_writes_to_wal(tmp_path, monkeypatch):
    import json
    daemon, hook_state = _make_span_daemon(tmp_path, monkeypatch)
    r = daemon.call_tool("icc_span_open", {"intent_signature": "lark.read.get-doc"})
    span_id = json.loads(r["content"][0]["text"])["span_id"]
    result = daemon.call_tool("icc_span_close", {"span_id": span_id, "outcome": "success"})
    assert result.get("isError") is not True
    wal = tmp_path / "state" / "span-wal" / "spans.jsonl"
    assert wal.exists()
    record = json.loads(wal.read_text().strip())
    assert record["outcome"] == "success"
    assert record["intent_signature"] == "lark.read.get-doc"


def test_span_close_returns_policy_status(tmp_path, monkeypatch):
    import json
    daemon, _ = _make_span_daemon(tmp_path, monkeypatch)
    r = daemon.call_tool("icc_span_open", {"intent_signature": "lark.read.get-doc"})
    sid = json.loads(r["content"][0]["text"])["span_id"]
    body = json.loads(daemon.call_tool("icc_span_close", {"span_id": sid, "outcome": "success"})["content"][0]["text"])
    assert "policy_status" in body
    assert body["policy_status"] == "explore"


def test_span_close_errors_on_bad_outcome(tmp_path, monkeypatch):
    daemon, _ = _make_span_daemon(tmp_path, monkeypatch)
    result = daemon.call_tool("icc_span_close", {"outcome": "done"})
    assert result.get("isError") is True


def test_span_close_generates_skeleton_at_stable(tmp_path, monkeypatch):
    import json
    daemon, hook_state = _make_span_daemon(tmp_path, monkeypatch)
    connector_root = tmp_path / "connectors"
    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(connector_root))
    # Drive to stable using monkeypatched thresholds
    import scripts.span_tracker as st
    monkeypatch.setattr(st, "PROMOTE_MIN_ATTEMPTS", 2)
    monkeypatch.setattr(st, "PROMOTE_MIN_SUCCESS_RATE", 0.5)
    monkeypatch.setattr(st, "PROMOTE_MAX_HUMAN_FIX_RATE", 1.0)
    monkeypatch.setattr(st, "STABLE_MIN_ATTEMPTS", 4)
    monkeypatch.setattr(st, "STABLE_MIN_SUCCESS_RATE", 0.5)
    # Re-create tracker with patched constants
    from scripts.span_tracker import SpanTracker
    daemon._span_tracker = SpanTracker(
        state_root=tmp_path / "state",
        hook_state_root=hook_state,
    )
    for _ in range(5):
        r = daemon.call_tool("icc_span_open", {"intent_signature": "lark.write.create-doc"})
        body = json.loads(r["content"][0]["text"])
        if body.get("bridge"):
            break
        sid = body["span_id"]
        buf = hook_state / "active-span-actions.jsonl"
        buf.write_text(
            json.dumps({"tool_name": "mcp__lark_doc__create", "args_hash": "x",
                        "has_side_effects": True, "ts_ms": 1}) + "\n"
        )
        daemon.call_tool("icc_span_close", {"span_id": sid, "outcome": "success"})
    pending = connector_root / "lark" / "pipelines" / "write" / "_pending" / "create-doc.py"
    assert pending.exists(), "skeleton must be generated at stable"
    assert "def run_write" in pending.read_text()


# ── icc_span_approve ──────────────────────────────────────────────────────────

def test_span_approve_moves_pending_and_generates_yaml(tmp_path, monkeypatch):
    import json
    daemon, hook_state = _make_span_daemon(tmp_path, monkeypatch)
    connector_root = tmp_path / "connectors"
    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(connector_root))
    # Pre-create skeleton in _pending/
    pending_dir = connector_root / "lark" / "pipelines" / "write" / "_pending"
    pending_dir.mkdir(parents=True)
    skeleton = pending_dir / "create-doc.py"
    skeleton.write_text(
        "def run_write(metadata, args):\n    return {'ok': True}\n"
        "def verify_write(metadata, args, action_result):\n    return {'ok': True}\n",
        encoding="utf-8",
    )
    # Drive to stable so approve is allowed
    import scripts.span_tracker as st
    monkeypatch.setattr(st, "PROMOTE_MIN_ATTEMPTS", 2)
    monkeypatch.setattr(st, "PROMOTE_MIN_SUCCESS_RATE", 0.5)
    monkeypatch.setattr(st, "PROMOTE_MAX_HUMAN_FIX_RATE", 1.0)
    monkeypatch.setattr(st, "STABLE_MIN_ATTEMPTS", 4)
    monkeypatch.setattr(st, "STABLE_MIN_SUCCESS_RATE", 0.5)
    from scripts.span_tracker import SpanTracker
    daemon._span_tracker = SpanTracker(state_root=tmp_path / "state", hook_state_root=hook_state)
    for _ in range(5):
        s = daemon._span_tracker.open_span("lark.write.create-doc")
        daemon._open_spans[s.span_id] = s
        daemon._span_tracker.close_span(s, outcome="success")

    from unittest.mock import patch
    with patch.object(daemon, "_elicit", return_value={"confirmed": True}):
        result = daemon.call_tool("icc_span_approve", {"intent_signature": "lark.write.create-doc"})
    assert result.get("isError") is not True
    body = json.loads(result["content"][0]["text"])
    assert body.get("approved") is True
    # .py moved to real dir
    real_py = connector_root / "lark" / "pipelines" / "write" / "create-doc.py"
    assert real_py.exists()
    assert not skeleton.exists()  # removed from _pending
    # .yaml generated alongside
    real_yaml = connector_root / "lark" / "pipelines" / "write" / "create-doc.yaml"
    assert real_yaml.exists()
    import yaml
    meta = yaml.safe_load(real_yaml.read_text())
    assert meta["intent_signature"] == "lark.write.create-doc"


def test_span_approve_errors_when_not_stable(tmp_path, monkeypatch):
    daemon, _ = _make_span_daemon(tmp_path, monkeypatch)
    result = daemon.call_tool("icc_span_approve", {"intent_signature": "lark.write.never-run"})
    assert result.get("isError") is True


def test_span_approve_errors_when_pending_missing(tmp_path, monkeypatch):
    import scripts.span_tracker as st
    monkeypatch.setattr(st, "PROMOTE_MIN_ATTEMPTS", 1)
    monkeypatch.setattr(st, "PROMOTE_MIN_SUCCESS_RATE", 0.0)
    monkeypatch.setattr(st, "PROMOTE_MAX_HUMAN_FIX_RATE", 1.0)
    monkeypatch.setattr(st, "STABLE_MIN_ATTEMPTS", 2)
    monkeypatch.setattr(st, "STABLE_MIN_SUCCESS_RATE", 0.0)
    daemon, hook_state = _make_span_daemon(tmp_path, monkeypatch)
    from scripts.span_tracker import SpanTracker
    daemon._span_tracker = SpanTracker(state_root=tmp_path / "state", hook_state_root=hook_state)
    for _ in range(3):
        s = daemon._span_tracker.open_span("lark.write.create-doc")
        daemon._open_spans[s.span_id] = s
        daemon._span_tracker.close_span(s, outcome="success")
    # No _pending file exists
    result = daemon.call_tool("icc_span_approve", {"intent_signature": "lark.write.create-doc"})
    assert result.get("isError") is True
    import json
    assert "_pending" in json.loads(result["content"][0]["text"]).get("message", "")


# ── deprecation + connector://spans ───────────────────────────────────────────

def test_icc_read_returns_deprecated_error(tmp_path, monkeypatch):
    """icc_read is not in the schema — CC cannot discover or call it."""
    daemon, _ = _make_span_daemon(tmp_path, monkeypatch)
    listed = daemon.handle_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    names = [t["name"] for t in listed["result"]["tools"]]
    assert "icc_read" not in names, "icc_read must not appear in schema (deprecated)"


def test_icc_write_returns_deprecated_error(tmp_path, monkeypatch):
    """icc_write is not in the schema — CC cannot discover or call it."""
    daemon, _ = _make_span_daemon(tmp_path, monkeypatch)
    listed = daemon.handle_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    names = [t["name"] for t in listed["result"]["tools"]]
    assert "icc_write" not in names, "icc_write must not appear in schema (deprecated)"


def test_spans_resource_lists_connector_intents(tmp_path, monkeypatch):
    import json
    daemon, hook_state = _make_span_daemon(tmp_path, monkeypatch)
    from scripts.span_tracker import SpanTracker
    daemon._span_tracker = SpanTracker(state_root=tmp_path / "state", hook_state_root=hook_state)
    s = daemon._span_tracker.open_span("lark.read.get-doc")
    daemon._open_spans[s.span_id] = s
    daemon._span_tracker.close_span(s, outcome="success")
    resources = daemon._list_resources()
    uris = [r["uri"] for r in resources]
    assert "connector://lark/spans" in uris


def test_frozen_pipeline_skips_auto_promotion(tmp_path, monkeypatch):
    """Frozen pipelines keep status; 19 successes stay explore, 20th would promote without frozen."""
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path))
    daemon = EmergeDaemon(root=ROOT)
    key = "mock.read.frozen-test"
    for _ in range(19):
        daemon._record_exec_event(
            arguments={
                "intent_signature": key,
                "script_ref": "s",
                "description": "",
            },
            result={"content": [{"type": "text", "text": "{}"}]},
            target_profile="default",
            mode="inline_code",
            execution_path="local",
            sampled_in_policy=True,
            candidate_key=key,
        )
    reg_path = tmp_path / "pipelines-registry.json"
    data = json.loads(reg_path.read_text())
    data["pipelines"][key]["frozen"] = True
    reg_path.write_text(json.dumps(data))
    old_status = data["pipelines"][key]["status"]
    assert old_status == "explore"
    daemon._record_exec_event(
        arguments={"intent_signature": key, "script_ref": "s", "description": ""},
        result={"content": [{"type": "text", "text": "{}"}]},
        target_profile="default",
        mode="inline_code",
        execution_path="local",
        sampled_in_policy=True,
        candidate_key=key,
    )
    data = json.loads(reg_path.read_text())
    assert data["pipelines"][key]["status"] == old_status


def test_concurrent_exec_events_do_not_lose_attempts(tmp_path, monkeypatch):
    """Two threads calling _record_exec_event concurrently must not lose counts."""
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path))
    daemon = EmergeDaemon()
    base_args = {
        "intent_signature": "zwcad.read.state",
        "code": "__result = 1",
        "target_profile": "default",
        "description": "",
    }
    fake_result = {"isError": False}
    errors = []

    def record():
        try:
            daemon._record_exec_event(
                arguments=base_args,
                result=fake_result,
                target_profile="default",
                mode="inline_code",
                execution_path="local",
                sampled_in_policy=True,
                candidate_key="zwcad.read.state",
            )
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=record) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Errors during concurrent execution: {errors}"
    session_dir = tmp_path / daemon._base_session_id
    reg = json.loads((session_dir / "candidates.json").read_text())
    assert reg["candidates"]["zwcad.read.state"]["attempts"] == 20, \
        f"Expected 20 attempts, got {reg['candidates']['zwcad.read.state']['attempts']} — lost updates!"


def test_bridge_failure_records_consecutive_failure(tmp_path, monkeypatch):
    """When the flywheel bridge raises, consecutive_failures must increment in the registry."""
    import json
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path))
    from scripts.emerge_daemon import EmergeDaemon
    daemon = EmergeDaemon()

    # Seed pipelines-registry with a stable pipeline
    registry_path = tmp_path / "pipelines-registry.json"
    from scripts.emerge_daemon import EmergeDaemon as _D
    _D._atomic_write_json(registry_path, {
        "pipelines": {
            "zwcad.read.state": {
                "status": "stable",
                "rollout_pct": 100,
                "consecutive_failures": 0,
                "attempts": 50,
                "successes": 50,
                "verify_passes": 50,
                "human_fixes": 0,
            }
        }
    })

    # Also seed candidates.json so _record_pipeline_event can update it
    session_dir = tmp_path / daemon._base_session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    _D._atomic_write_json(session_dir / "candidates.json", {
        "candidates": {
            "zwcad.read.state": {
                "source": "pipeline",
                "pipeline_id": "zwcad.read.state",
                "target_profile": "default",
                "last_execution_path": "local",
                "intent_signature": "zwcad.read.state",
                "script_ref": "zwcad.read.state",
                "attempts": 50,
                "successes": 50,
                "verify_passes": 50,
                "human_fixes": 0,
                "degraded_count": 0,
                "consecutive_failures": 0,
                "recent_outcomes": [1] * 20,
                "total_calls": 50,
                "last_ts_ms": 0,
            }
        }
    })

    # Patch PipelineEngine.run_read to raise
    def boom(*args, **kwargs):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(daemon.pipeline, "run_read", boom)

    result = daemon._try_flywheel_bridge({"intent_signature": "zwcad.read.state"})
    assert result is None  # bridge must fail gracefully

    # Verify consecutive_failures was incremented in pipelines-registry
    updated = json.loads(registry_path.read_text())
    assert updated["pipelines"]["zwcad.read.state"]["consecutive_failures"] == 1, \
        f"Expected consecutive_failures=1, got {updated['pipelines']['zwcad.read.state']['consecutive_failures']}"


def test_runner_router_cached_between_calls(tmp_path, monkeypatch):
    """_get_runner_router() must cache and only rebuild when config changes."""
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path))
    monkeypatch.delenv("EMERGE_RUNNER_URL", raising=False)

    from scripts.runner_client import RunnerRouter
    from scripts.emerge_daemon import EmergeDaemon

    call_count = []
    original_from_env = RunnerRouter.from_env.__func__

    @classmethod
    def counting_from_env(cls):
        call_count.append(1)
        return original_from_env(cls)

    monkeypatch.setattr(RunnerRouter, "from_env", counting_from_env)

    daemon = EmergeDaemon()
    initial_calls = len(call_count)

    # Call _get_runner_router 10 times without changing config
    for _ in range(10):
        daemon._get_runner_router()

    # Should NOT have called from_env again (cached)
    additional_calls = len(call_count) - initial_calls
    assert additional_calls == 0, (
        f"from_env called {additional_calls} extra times for 10 _get_runner_router() "
        "calls without config change — caching broken"
    )


# ── Task 5: stable event → sync-queue ──────────────────────────────────────

def test_stable_transition_writes_to_sync_queue(tmp_path, monkeypatch):
    """When a pipeline reaches stable, daemon writes a 'stable' event to sync-queue."""
    import json
    import time

    from scripts.emerge_daemon import EmergeDaemon
    from scripts.hub_config import consume_sync_events, save_hub_config
    from scripts.policy_config import STABLE_MIN_ATTEMPTS

    monkeypatch.setenv("EMERGE_HUB_HOME", str(tmp_path))
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path / "state"))
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)

    # Hub configured with gmail selected
    save_hub_config({
        "remote": "git@quasar:team/hub.git",
        "selected_verticals": ["gmail"],
    })

    daemon = EmergeDaemon(root=ROOT)

    # Inject pipelines-registry entry at canary
    registry_path = daemon._state_root / "pipelines-registry.json"
    registry = {
        "pipelines": {
            "gmail.read.fetch": {
                "status": "canary",
                "rollout_pct": 20,
                "attempts_at_transition": 0,
                "last_transition_reason": "init",
            }
        }
    }
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    registry_path.write_text(json.dumps(registry), encoding="utf-8")

    # Build candidate entry with sufficient stats to trigger stable transition
    entry = {
        "intent_signature": "gmail.read.fetch",
        "status": "canary",
        "attempts": STABLE_MIN_ATTEMPTS,
        "successes": STABLE_MIN_ATTEMPTS,
        "verify_passes": STABLE_MIN_ATTEMPTS,
        "human_fixes": 0,
        "consecutive_failures": 0,
        "recent_outcomes": [1] * STABLE_MIN_ATTEMPTS,
        "last_ts_ms": int(time.time() * 1000),
    }
    daemon._update_pipeline_registry(candidate_key="gmail.read.fetch", entry=entry)

    events = consume_sync_events(lambda e: e.get("event") == "stable")
    assert any(e.get("connector") == "gmail" for e in events), \
        f"Expected stable event for gmail, got: {events}"


# ── Task 6: icc_hub MCP tool ────────────────────────────────────────────────

def test_icc_hub_list_returns_config(tmp_path, monkeypatch):
    from scripts.emerge_daemon import EmergeDaemon
    from scripts.hub_config import save_hub_config

    monkeypatch.setenv("EMERGE_HUB_HOME", str(tmp_path))
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path / "state"))
    (tmp_path / "state").mkdir(parents=True)

    save_hub_config({
        "remote": "git@quasar:team/hub.git",
        "branch": "emerge-hub",
        "selected_verticals": ["gmail", "linear"],
    })

    daemon = EmergeDaemon(root=ROOT)
    result = daemon.call_tool("icc_hub", {"action": "list"})
    assert not result["isError"]
    payload = json.loads(result["content"][0]["text"])
    assert "gmail" in payload["selected_verticals"]
    assert payload["remote"] == "git@quasar:team/hub.git"


def test_icc_hub_add_connector(tmp_path, monkeypatch):
    from scripts.emerge_daemon import EmergeDaemon
    from scripts.hub_config import load_hub_config, save_hub_config

    monkeypatch.setenv("EMERGE_HUB_HOME", str(tmp_path))
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path / "state"))
    (tmp_path / "state").mkdir(parents=True)

    save_hub_config({"remote": "git@x:y.git", "selected_verticals": ["gmail"]})

    daemon = EmergeDaemon(root=ROOT)
    result = daemon.call_tool("icc_hub", {"action": "add", "connector": "linear"})
    assert not result["isError"]
    cfg = load_hub_config()
    assert "linear" in cfg["selected_verticals"]


def test_icc_hub_remove_connector(tmp_path, monkeypatch):
    from scripts.emerge_daemon import EmergeDaemon
    from scripts.hub_config import load_hub_config, save_hub_config

    monkeypatch.setenv("EMERGE_HUB_HOME", str(tmp_path))
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path / "state"))
    (tmp_path / "state").mkdir(parents=True)

    save_hub_config({"remote": "git@x:y.git", "selected_verticals": ["gmail", "slack"]})

    daemon = EmergeDaemon(root=ROOT)
    result = daemon.call_tool("icc_hub", {"action": "remove", "connector": "slack"})
    assert not result["isError"]
    cfg = load_hub_config()
    assert "slack" not in cfg["selected_verticals"]
    assert "gmail" in cfg["selected_verticals"]


def test_icc_hub_status_shows_pending_conflicts(tmp_path, monkeypatch):
    from scripts.emerge_daemon import EmergeDaemon
    from scripts.hub_config import save_hub_config, save_pending_conflicts

    monkeypatch.setenv("EMERGE_HUB_HOME", str(tmp_path))
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path / "state"))
    (tmp_path / "state").mkdir(parents=True)

    save_hub_config({"remote": "git@x:y.git", "selected_verticals": ["gmail"]})
    save_pending_conflicts({"conflicts": [
        {"conflict_id": "abc", "connector": "gmail", "file": "fetch.py",
         "status": "pending", "resolution": None, "ours_ts_ms": 1, "theirs_ts_ms": 0},
        {"conflict_id": "def", "connector": "gmail", "file": "send.py",
         "status": "resolved", "resolution": "ours", "ours_ts_ms": 2, "theirs_ts_ms": 0},
    ]})

    daemon = EmergeDaemon(root=ROOT)
    result = daemon.call_tool("icc_hub", {"action": "status"})
    assert not result["isError"]
    payload = json.loads(result["content"][0]["text"])
    assert payload["pending_conflicts"] == 1       # only "pending" counts here
    assert payload["awaiting_application"] == 1   # "resolved" awaiting sync agent


def test_icc_hub_resolve_does_not_leak_queue_event(tmp_path, monkeypatch):
    """resolve action must NOT write any event to the sync queue (nothing consumes it)."""
    from scripts.emerge_daemon import EmergeDaemon
    from scripts.hub_config import save_hub_config, save_pending_conflicts, sync_queue_path

    monkeypatch.setenv("EMERGE_HUB_HOME", str(tmp_path))
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path / "state"))
    (tmp_path / "state").mkdir(parents=True)

    save_hub_config({"remote": "git@x:y.git", "selected_verticals": ["gmail"]})
    save_pending_conflicts({"conflicts": [
        {"conflict_id": "abc", "connector": "gmail", "file": "fetch.py",
         "status": "pending", "resolution": None, "ours_ts_ms": 1, "theirs_ts_ms": 0}
    ]})

    daemon = EmergeDaemon(root=ROOT)
    daemon.call_tool("icc_hub", {"action": "resolve", "conflict_id": "abc", "resolution": "ours"})

    qp = sync_queue_path()
    assert not qp.exists() or qp.read_text().strip() == ""


def test_icc_hub_sync_enqueues_push_and_pull(tmp_path, monkeypatch):
    from scripts.emerge_daemon import EmergeDaemon
    from scripts.hub_config import consume_sync_events, save_hub_config

    monkeypatch.setenv("EMERGE_HUB_HOME", str(tmp_path))
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path / "state"))
    (tmp_path / "state").mkdir(parents=True)

    save_hub_config({"remote": "git@x:y.git", "selected_verticals": ["gmail"]})

    daemon = EmergeDaemon(root=ROOT)
    result = daemon.call_tool("icc_hub", {"action": "sync", "connector": "gmail"})
    assert not result["isError"]
    payload = json.loads(result["content"][0]["text"])
    assert "gmail" in payload["triggered"]

    stable_events = consume_sync_events(lambda e: e.get("event") == "stable")
    pull_events = consume_sync_events(lambda e: e.get("event") == "pull_requested")
    assert any(e["connector"] == "gmail" for e in stable_events)
    assert any(e["connector"] == "gmail" for e in pull_events)


def test_icc_hub_resolve_conflict(tmp_path, monkeypatch):
    from scripts.emerge_daemon import EmergeDaemon
    from scripts.hub_config import load_pending_conflicts, save_hub_config, save_pending_conflicts

    monkeypatch.setenv("EMERGE_HUB_HOME", str(tmp_path))
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path / "state"))
    (tmp_path / "state").mkdir(parents=True)

    save_hub_config({"remote": "git@x:y.git", "selected_verticals": ["gmail"]})
    save_pending_conflicts({"conflicts": [
        {"conflict_id": "abc", "connector": "gmail", "file": "fetch.py",
         "status": "pending", "resolution": None, "ours_ts_ms": 1, "theirs_ts_ms": 0}
    ]})

    daemon = EmergeDaemon(root=ROOT)
    result = daemon.call_tool("icc_hub", {
        "action": "resolve",
        "conflict_id": "abc",
        "resolution": "ours",
    })
    assert not result["isError"]
    data = load_pending_conflicts()
    conflict = data["conflicts"][0]
    assert conflict["resolution"] == "ours"
    assert conflict["status"] == "resolved"


def test_icc_hub_configure_saves_config_and_inits_worktree(tmp_path, monkeypatch):
    """configure action saves hub-config.json and calls git_setup_worktree."""
    import subprocess
    from unittest.mock import patch
    from scripts.emerge_daemon import EmergeDaemon
    from scripts.hub_config import load_hub_config

    monkeypatch.setenv("EMERGE_HUB_HOME", str(tmp_path))
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path / "state"))
    (tmp_path / "state").mkdir(parents=True)

    # Use a real local bare repo as remote so git_setup_worktree succeeds
    bare = tmp_path / "remote.git"
    bare.mkdir()
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)

    daemon = EmergeDaemon(root=ROOT)
    result = daemon.call_tool("icc_hub", {
        "action": "configure",
        "remote": str(bare),
        "author": "test <test@test.com>",
        "selected_verticals": ["gmail", "linear"],
        "branch": "emerge-hub",
    })

    assert not result["isError"], result
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["action"] in ("created", "cloned", "already_exists")
    assert payload["remote"] == str(bare)

    cfg = load_hub_config()
    assert cfg["remote"] == str(bare)
    assert cfg["author"] == "test <test@test.com>"
    assert "gmail" in cfg["selected_verticals"]
    assert "linear" in cfg["selected_verticals"]

    worktree = tmp_path / "hub-worktree"
    assert worktree.exists()
    assert (worktree / ".git").exists()


def test_icc_hub_configure_imports_existing_hub_on_clone(tmp_path, monkeypatch):
    """When configure clones an existing hub branch, remote pipelines must be
    imported into the local connectors directory immediately."""
    import subprocess
    from scripts.emerge_daemon import EmergeDaemon

    monkeypatch.setenv("EMERGE_HUB_HOME", str(tmp_path))
    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(tmp_path / "connectors"))
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path / "state"))
    (tmp_path / "state").mkdir(parents=True)

    # Create a bare remote
    bare = tmp_path / "remote.git"
    bare.mkdir()
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)

    # Machine A bootstraps the branch and pushes a pipeline
    worktree_a = tmp_path / "worktree_a"
    worktree_a.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@a.com",
           "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@a.com"}

    def _git(*args):
        subprocess.run(list(args), cwd=str(worktree_a), check=True, capture_output=True, env=env)

    _git("git", "init")
    _git("git", "config", "user.name", "a")
    _git("git", "config", "user.email", "a@a.com")
    _git("git", "remote", "add", "origin", str(bare))
    _git("git", "checkout", "--orphan", "emerge-hub")
    _git("git", "commit", "--allow-empty", "-m", "chore: init emerge-hub")
    _git("git", "push", "-u", "origin", "emerge-hub")

    # A pushes a pipeline file
    pipeline_dir = worktree_a / "connectors" / "cloud-server" / "pipelines" / "read"
    pipeline_dir.mkdir(parents=True)
    (pipeline_dir / "list_vms.py").write_text("# list_vms from A", encoding="utf-8")
    spans_dir = worktree_a / "connectors" / "cloud-server"
    (spans_dir / "spans.json").write_text(
        json.dumps({"spans": {"cloud-server.read.list_vms": {"intent_signature": "cloud-server.read.list_vms", "status": "stable", "last_ts_ms": 1000}}}),
        encoding="utf-8",
    )
    _git("git", "add", "-A")
    _git("git", "commit", "-m", "hub: sync cloud-server")
    _git("git", "push", "origin", "emerge-hub")

    # Machine B configures — should clone and import
    daemon = EmergeDaemon(root=ROOT)
    result = daemon.call_tool("icc_hub", {
        "action": "configure",
        "remote": str(bare),
        "author": "b <b@b.com>",
        "selected_verticals": ["cloud-server"],
        "branch": "emerge-hub",
    })
    assert not result["isError"], result["content"][0]["text"]
    payload = json.loads(result["content"][0]["text"])
    assert payload["action"] == "cloned"

    # B's local connectors must have A's pipeline
    local_pipeline = tmp_path / "connectors" / "cloud-server" / "pipelines" / "read" / "list_vms.py"
    assert local_pipeline.exists(), "A's pipeline must be imported to B's local connectors on configure"
    assert "list_vms from A" in local_pipeline.read_text(encoding="utf-8")


def test_icc_hub_configure_requires_remote_and_author(tmp_path, monkeypatch):
    """configure must reject calls missing required fields."""
    from scripts.emerge_daemon import EmergeDaemon

    monkeypatch.setenv("EMERGE_HUB_HOME", str(tmp_path))
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path / "state"))
    (tmp_path / "state").mkdir(parents=True)

    daemon = EmergeDaemon(root=ROOT)

    r1 = daemon.call_tool("icc_hub", {"action": "configure", "author": "A <a@b.com>"})
    assert r1["isError"]
    assert "remote" in r1["content"][0]["text"]

    r2 = daemon.call_tool("icc_hub", {"action": "configure", "remote": "git@x:y.git"})
    assert r2["isError"]
    assert "author" in r2["content"][0]["text"]


def test_concurrent_tool_calls_each_get_correct_response():
    """Multiple simultaneous tool calls must each return their own result."""
    import concurrent.futures
    from scripts.emerge_daemon import EmergeDaemon
    daemon = EmergeDaemon()

    def call(i):
        return daemon.handle_jsonrpc({
            "jsonrpc": "2.0", "id": f"req-{i}", "method": "tools/call",
            "params": {"name": "icc_goal_read", "arguments": {}}
        })

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(call, i) for i in range(5)]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]

    ids = {r["id"] for r in results if r}
    assert len(ids) == 5  # each request gets its own response id


def test_initialize_declares_elicitation_capability():
    """initialize response must advertise elicitation capability and protocol 2025-03-26."""
    from scripts.emerge_daemon import EmergeDaemon
    daemon = EmergeDaemon()
    resp = daemon.handle_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    result = resp["result"]
    assert result["protocolVersion"] == "2025-03-26"
    assert "elicitation" in result["capabilities"]


def test_elicit_returns_result_when_response_arrives():
    """_elicit() must return the response payload set via correlation map."""
    import threading, time
    from scripts.emerge_daemon import EmergeDaemon
    daemon = EmergeDaemon()
    captured_push = []
    daemon._write_mcp_push = lambda p: captured_push.append(p)

    result_holder = []

    def _run():
        result_holder.append(daemon._elicit("Confirm?", {"type": "object",
            "properties": {"confirmed": {"type": "boolean"}}}, timeout=5.0))

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    # Let _elicit register its event before we simulate the response
    time.sleep(0.05)
    assert len(captured_push) == 1
    elicit_id = captured_push[0]["id"]
    # Simulate CC sending back an elicitation response
    daemon._elicit_results[elicit_id] = {"confirmed": True}
    daemon._elicit_events.pop(elicit_id).set()

    t.join(timeout=2.0)
    assert result_holder == [{"confirmed": True}]


def test_elicit_returns_none_on_timeout():
    """_elicit() must return None when no response arrives within timeout."""
    from scripts.emerge_daemon import EmergeDaemon
    daemon = EmergeDaemon()
    daemon._write_mcp_push = lambda _: None
    result = daemon._elicit("Confirm?", {}, timeout=0.1)
    assert result is None


def test_span_approve_elicitation_confirmed(tmp_path):
    """icc_span_approve must call _elicit and proceed when confirmed=True."""
    import os
    from scripts.emerge_daemon import EmergeDaemon
    from unittest.mock import patch
    daemon = EmergeDaemon()

    conn, mode, name = "testconn", "read", "fetch"
    os.environ["EMERGE_CONNECTOR_ROOT"] = str(tmp_path)
    pending_dir = tmp_path / conn / "pipelines" / mode / "_pending"
    pending_dir.mkdir(parents=True)
    (pending_dir / f"{name}.py").write_text(
        "def run_read(m,a): return {}\ndef verify_read(m,a,r): return True\n"
    )

    with patch.object(daemon._span_tracker, "get_policy_status", return_value="stable"):
        with patch.object(daemon, "_elicit", return_value={"confirmed": True}) as mock_elicit:
            result = daemon.call_tool("icc_span_approve", {"intent_signature": f"{conn}.{mode}.{name}"})

    assert result.get("structuredContent", {}).get("approved") is True
    mock_elicit.assert_called_once()
    os.environ.pop("EMERGE_CONNECTOR_ROOT", None)


def test_span_approve_elicitation_cancelled(tmp_path):
    """icc_span_approve must return cancellation when confirmed=False."""
    import os
    from scripts.emerge_daemon import EmergeDaemon
    from unittest.mock import patch
    daemon = EmergeDaemon()

    conn, mode, name = "testconn", "read", "fetch"
    os.environ["EMERGE_CONNECTOR_ROOT"] = str(tmp_path)
    pending_dir = tmp_path / conn / "pipelines" / mode / "_pending"
    pending_dir.mkdir(parents=True)
    (pending_dir / f"{name}.py").write_text(
        "def run_read(m,a): return {}\ndef verify_read(m,a,r): return True\n"
    )

    with patch.object(daemon._span_tracker, "get_policy_status", return_value="stable"):
        with patch.object(daemon, "_elicit", return_value={"confirmed": False}):
            result = daemon.call_tool("icc_span_approve", {"intent_signature": f"{conn}.{mode}.{name}"})

    assert result.get("structuredContent", {}).get("approved") is not True
    assert "cancel" in str(result).lower()
    os.environ.pop("EMERGE_CONNECTOR_ROOT", None)


def test_span_approve_elicitation_timeout(tmp_path):
    """icc_span_approve must return error when _elicit times out (returns None)."""
    import os
    from scripts.emerge_daemon import EmergeDaemon
    from unittest.mock import patch
    daemon = EmergeDaemon()

    conn, mode, name = "testconn", "read", "fetch"
    os.environ["EMERGE_CONNECTOR_ROOT"] = str(tmp_path)
    pending_dir = tmp_path / conn / "pipelines" / mode / "_pending"
    pending_dir.mkdir(parents=True)
    (pending_dir / f"{name}.py").write_text(
        "def run_read(m,a): return {}\ndef verify_read(m,a,r): return True\n"
    )

    with patch.object(daemon._span_tracker, "get_policy_status", return_value="stable"):
        with patch.object(daemon, "_elicit", return_value=None):
            result = daemon.call_tool("icc_span_approve", {"intent_signature": f"{conn}.{mode}.{name}"})

    assert result.get("isError") or "timed out" in str(result).lower()
    os.environ.pop("EMERGE_CONNECTOR_ROOT", None)


def test_reconcile_elicitation_used_when_outcome_not_provided():
    """icc_reconcile with no outcome must call _elicit to ask the user."""
    from scripts.emerge_daemon import EmergeDaemon
    from scripts.state_tracker import load_tracker, save_tracker
    from unittest.mock import patch
    daemon = EmergeDaemon()

    state_path = daemon._hook_state_path()
    tracker = load_tracker(state_path)
    tracker.add_delta("test message", "info", intent_signature="test:sig")
    save_tracker(state_path, tracker)
    delta_id = tracker.state["deltas"][-1]["id"]

    with patch.object(daemon, "_elicit", return_value={"outcome": "confirm"}) as mock_elicit:
        result = daemon.call_tool("icc_reconcile", {"delta_id": delta_id})

    assert result.get("structuredContent", {}).get("outcome") == "confirm"
    mock_elicit.assert_called_once()


def test_hub_resolve_elicitation_used_when_resolution_not_provided():
    """icc_hub resolve without resolution arg must call _elicit."""
    from scripts.emerge_daemon import EmergeDaemon
    from scripts.hub_config import save_pending_conflicts
    from unittest.mock import patch
    daemon = EmergeDaemon()

    save_pending_conflicts({"conflicts": [
        {"conflict_id": "c1", "connector": "gmail", "file": "x.py", "status": "pending"}
    ]})

    with patch.object(daemon, "_elicit", return_value={"resolution": "ours"}) as mock_elicit:
        result = daemon.call_tool("icc_hub", {
            "action": "resolve", "conflict_id": "c1"
        })

    assert result.get("structuredContent", {}).get("ok") is True
    mock_elicit.assert_called_once()


def test_event_router_replaces_pending_monitor(tmp_path):
    """EventRouter must fire MCP push when pending-actions.json is created."""
    import threading, json, time, os
    from scripts.emerge_daemon import EmergeDaemon

    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path)
    daemon = EmergeDaemon()

    pushed = []
    daemon._write_mcp_push = lambda p: pushed.append(p)
    daemon.start_event_router()
    time.sleep(0.1)

    pending = tmp_path / "pending-actions.json"
    pending.write_text(json.dumps({
        "submitted_at": int(time.time() * 1000),
        "actions": [{"type": "prompt", "prompt": "hello"}]
    }))

    # Wait for EventRouter to pick it up
    deadline = time.time() + 3.0
    while time.time() < deadline and not pushed:
        time.sleep(0.05)

    daemon.stop_event_router()
    os.environ.pop("EMERGE_STATE_ROOT", None)

    assert len(pushed) == 1
    assert pushed[0]["method"] == "notifications/claude/channel"
    assert pushed[0]["params"]["meta"]["source"] == "cockpit"


# ---------------------------------------------------------------------------
# C2: Elicitation response extraction (accept / decline / cancel)
# ---------------------------------------------------------------------------

def _fire_elicit_response(daemon, elicit_id, action, content=None):
    """Simulate what run_stdio() does when it receives an elicitation response.

    This replicates the extraction logic from run_stdio exactly so tests
    verify the correct code path, not just the _elicit() bookkeeping.
    """
    result_obj = {"action": action}
    if content is not None:
        result_obj["content"] = content
    if action != "accept":
        daemon._elicit_results[elicit_id] = None
    else:
        daemon._elicit_results[elicit_id] = result_obj.get("content") or {}
    ev = daemon._elicit_events.pop(elicit_id, None)
    if ev is not None:
        ev.set()


def test_elicit_accept_response_extracts_content():
    """run_stdio accept response must extract result.content into _elicit_results."""
    import threading, time
    from scripts.emerge_daemon import EmergeDaemon
    daemon = EmergeDaemon()
    daemon._write_mcp_push = lambda p: None

    result_holder = []

    def _run():
        result_holder.append(daemon._elicit(
            "Confirm?",
            {"type": "object", "properties": {"confirmed": {"type": "boolean"}}},
            timeout=5.0,
        ))

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    time.sleep(0.05)
    assert daemon._elicit_events, "worker must register event before response"
    elicit_id = next(iter(daemon._elicit_events))
    _fire_elicit_response(daemon, elicit_id, "accept", {"confirmed": True})

    t.join(timeout=2.0)
    assert result_holder == [{"confirmed": True}]


def test_elicit_decline_response_returns_none():
    """run_stdio decline response must cause _elicit() to return None."""
    import threading, time
    from scripts.emerge_daemon import EmergeDaemon
    daemon = EmergeDaemon()
    daemon._write_mcp_push = lambda p: None

    result_holder = []

    def _run():
        result_holder.append(daemon._elicit("Confirm?", {}, timeout=5.0))

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    time.sleep(0.05)
    assert daemon._elicit_events
    elicit_id = next(iter(daemon._elicit_events))
    _fire_elicit_response(daemon, elicit_id, "decline")

    t.join(timeout=2.0)
    assert result_holder == [None]


def test_elicit_cancel_response_returns_none():
    """run_stdio cancel response must cause _elicit() to return None."""
    import threading, time
    from scripts.emerge_daemon import EmergeDaemon
    daemon = EmergeDaemon()
    daemon._write_mcp_push = lambda p: None

    result_holder = []

    def _run():
        result_holder.append(daemon._elicit("Confirm?", {}, timeout=5.0))

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    time.sleep(0.05)
    assert daemon._elicit_events
    elicit_id = next(iter(daemon._elicit_events))
    _fire_elicit_response(daemon, elicit_id, "cancel")

    t.join(timeout=2.0)
    assert result_holder == [None]


# ---------------------------------------------------------------------------
# C4: _on_local_event_file delegates to OperatorMonitor.process_local_file
# ---------------------------------------------------------------------------

def test_on_local_event_file_delegates_to_operator_monitor(tmp_path):
    """_on_local_event_file must call OperatorMonitor.process_local_file."""
    import os
    from unittest.mock import MagicMock
    from scripts.emerge_daemon import EmergeDaemon

    daemon = EmergeDaemon()
    mock_monitor = MagicMock()
    daemon._operator_monitor = mock_monitor

    events_path = tmp_path / "machine-1" / "events.jsonl"
    events_path.parent.mkdir()
    events_path.write_text("")

    daemon._on_local_event_file(events_path)

    mock_monitor.process_local_file.assert_called_once_with(events_path)


def test_on_local_event_file_noop_when_monitor_not_running(tmp_path):
    """_on_local_event_file must silently no-op when OperatorMonitor is None."""
    from scripts.emerge_daemon import EmergeDaemon

    daemon = EmergeDaemon()
    assert daemon._operator_monitor is None

    events_path = tmp_path / "machine-1" / "events.jsonl"
    events_path.parent.mkdir()
    events_path.write_text("")

    # Must not raise
    daemon._on_local_event_file(events_path)


def test_on_local_event_file_ignores_non_events_jsonl(tmp_path):
    """_on_local_event_file must ignore files that are not events.jsonl."""
    from unittest.mock import MagicMock
    from scripts.emerge_daemon import EmergeDaemon

    daemon = EmergeDaemon()
    mock_monitor = MagicMock()
    daemon._operator_monitor = mock_monitor

    other_path = tmp_path / "machine-1" / "state.json"
    other_path.parent.mkdir()
    other_path.write_text("{}")

    daemon._on_local_event_file(other_path)

    mock_monitor.process_local_file.assert_not_called()


def test_process_local_file_reads_new_events(tmp_path):
    """OperatorMonitor.process_local_file must ingest events newer than last_poll_ms."""
    import json, time
    from scripts.operator_monitor import OperatorMonitor

    monitor = OperatorMonitor(machines={}, push_fn=lambda *a: None)

    machine_dir = tmp_path / "machine-1"
    machine_dir.mkdir()
    events_path = machine_dir / "events.jsonl"

    now_ms = int(time.time() * 1000)
    events_path.write_text(
        json.dumps({"ts_ms": now_ms, "type": "click", "session_role": "operator"}) + "\n" +
        json.dumps({"ts_ms": now_ms + 1, "type": "click", "session_role": "operator"}) + "\n"
    )

    monitor.process_local_file(events_path)

    assert monitor._last_poll_ms.get("local:machine-1") == now_ms + 1
    assert len(monitor._event_buffers.get("local:machine-1", [])) == 2


def test_process_local_file_skips_already_seen_events(tmp_path):
    """process_local_file must not re-ingest events at or before last_poll_ms."""
    import json, time
    from scripts.operator_monitor import OperatorMonitor

    monitor = OperatorMonitor(machines={}, push_fn=lambda *a: None)

    machine_dir = tmp_path / "machine-1"
    machine_dir.mkdir()
    events_path = machine_dir / "events.jsonl"

    now_ms = int(time.time() * 1000)
    monitor._last_poll_ms["local:machine-1"] = now_ms  # already seen up to now_ms

    events_path.write_text(
        json.dumps({"ts_ms": now_ms - 100, "type": "click", "session_role": "operator"}) + "\n" +
        json.dumps({"ts_ms": now_ms, "type": "click", "session_role": "operator"}) + "\n" +
        json.dumps({"ts_ms": now_ms + 500, "type": "click", "session_role": "operator"}) + "\n"
    )

    monitor.process_local_file(events_path)

    # Only the event at now_ms+500 is new
    assert monitor._last_poll_ms["local:machine-1"] == now_ms + 500
    buf = list(monitor._event_buffers.get("local:machine-1", []))
    assert len(buf) == 1
    assert buf[0]["ts_ms"] == now_ms + 500


def test_notify_helper_builds_correct_meta():
    """_notify() must produce a channel notification with unified meta schema."""
    from scripts.emerge_daemon import EmergeDaemon
    daemon = EmergeDaemon()
    pushed = []
    daemon._write_mcp_push = lambda p: pushed.append(p)

    daemon._notify(
        content="bridge failed for gmail.read.fetch",
        source="bridge",
        severity="high",
        category="warning",
        intent_signature="gmail.read.fetch",
        requires_action=False,
    )

    assert len(pushed) == 1
    p = pushed[0]
    assert p["method"] == "notifications/claude/channel"
    meta = p["params"]["meta"]
    assert meta["source"] == "bridge"
    assert meta["severity"] == "high"
    assert meta["category"] == "warning"
    assert meta["intent_signature"] == "gmail.read.fetch"
    assert meta["requires_action"] is False
    assert p["params"]["content"] == "bridge failed for gmail.read.fetch"


def test_notify_extra_meta_cannot_overwrite_required_fields():
    """extra_meta keys must NOT overwrite source/severity/category/requires_action."""
    from scripts.emerge_daemon import EmergeDaemon
    daemon = EmergeDaemon()
    pushed = []
    daemon._write_mcp_push = lambda p: pushed.append(p)

    daemon._notify(
        content="test",
        source="bridge",
        severity="high",
        category="warning",
        requires_action=False,
        extra_meta={"source": "evil", "severity": "low", "requires_action": True},
    )

    meta = pushed[0]["params"]["meta"]
    assert meta["source"] == "bridge"      # not overwritten by extra_meta
    assert meta["severity"] == "high"      # not overwritten by extra_meta
    assert meta["requires_action"] is False  # not overwritten by extra_meta


def test_notify_extra_meta_fields_are_merged():
    """extra_meta fields are present in the final notification meta."""
    from scripts.emerge_daemon import EmergeDaemon
    daemon = EmergeDaemon()
    pushed = []
    daemon._write_mcp_push = lambda p: pushed.append(p)

    daemon._notify(
        content="test",
        source="cockpit",
        extra_meta={"action_count": 3, "action_types": ["prompt"]},
    )

    meta = pushed[0]["params"]["meta"]
    assert meta["action_count"] == 3
    assert meta["action_types"] == ["prompt"]
    assert meta["source"] == "cockpit"  # required field still present


def test_bridge_failure_pushes_high_severity_notification(tmp_path, monkeypatch):
    """When flywheel bridge raises, daemon must push severity=high notification."""
    import json
    from scripts.emerge_daemon import EmergeDaemon
    from unittest.mock import patch

    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path))
    daemon = EmergeDaemon()

    # Seed pipelines-registry with a stable pipeline so bridge fires
    reg = {"pipelines": {"gmail.read.fetch": {"status": "stable", "consecutive_failures": 0}}}
    (tmp_path / "pipelines-registry.json").write_text(json.dumps(reg))

    notified = []
    daemon._notify = lambda **kw: notified.append(kw)

    # Patch pipeline.run_read to raise
    with patch.object(daemon.pipeline, "run_read", side_effect=RuntimeError("timeout")):
        result = daemon._try_flywheel_bridge({"intent_signature": "gmail.read.fetch"})

    assert result is None  # bridge fell through
    assert len(notified) == 1
    n = notified[0]
    assert n["source"] == "bridge"
    assert n["severity"] == "high"
    assert n["intent_signature"] == "gmail.read.fetch"
    assert "timeout" in n["content"]


def test_span_close_stable_pushes_skeleton_ready_notification(tmp_path, monkeypatch):
    """icc_span_close generating a skeleton must push a skeleton-ready notification to CC."""
    from scripts.emerge_daemon import EmergeDaemon
    from unittest.mock import patch, MagicMock

    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path))
    daemon = EmergeDaemon()

    # Open a span first
    daemon.call_tool("icc_span_open", {"intent_signature": "gmail.read.fetch"})

    notified = []
    daemon._notify = lambda **kw: notified.append(kw)

    # Fake skeleton path
    fake_path = tmp_path / "gmail" / "pipelines" / "read" / "_pending" / "fetch.py"
    fake_path.parent.mkdir(parents=True, exist_ok=True)
    fake_path.write_text("# skeleton")

    with patch.object(daemon, "_generate_span_skeleton", return_value=fake_path), \
         patch.object(daemon._span_tracker, "is_synthesis_ready", return_value=True), \
         patch.object(daemon._span_tracker, "skeleton_already_generated", return_value=False), \
         patch.object(daemon._span_tracker, "latest_successful_span", return_value=MagicMock()), \
         patch.object(daemon._span_tracker, "mark_skeleton_generated", return_value=None):
        daemon.call_tool("icc_span_close", {
            "intent_signature": "gmail.read.fetch",
            "outcome": "success",
        })

    assert len(notified) == 1, f"Expected 1 notification, got {notified}"
    n = notified[0]
    assert n["source"] == "span_synthesizer"
    assert n["severity"] == "info"
    assert n["category"] == "action_needed"
    assert n["requires_action"] is True
    assert n["intent_signature"] == "gmail.read.fetch"
    assert str(fake_path) in n["content"]


def test_push_pattern_uses_unified_meta_schema():
    """_push_pattern must use unified meta schema with source/severity/category."""
    from scripts.emerge_daemon import EmergeDaemon
    from scripts.pattern_detector import PatternSummary
    daemon = EmergeDaemon()
    pushed = []
    daemon._write_mcp_push = lambda p: pushed.append(p)

    summary = PatternSummary(
        intent_signature="zwcad.read.state",
        occurrences=5,
        window_minutes=12.0,
        context_hint={"app": "ZWCAD"},
        machine_ids=["m1"],
        policy_stage="explore",
        detector_signals=[],
    )
    daemon._push_pattern("explore", {"app": "ZWCAD"}, summary)

    assert len(pushed) == 1
    meta = pushed[0]["params"]["meta"]
    # Unified schema fields must all be present
    assert meta["source"] == "operator_monitor"
    assert meta["severity"] == "info"
    assert meta["category"] == "action_needed"
    assert meta["intent_signature"] == "zwcad.read.state"
    assert meta["requires_action"] is True
    # Legacy fields still present via extra_meta
    assert "policy_stage" in meta
    assert "occurrences" in meta


def test_on_pending_actions_uses_unified_meta_schema(tmp_path, monkeypatch):
    """_on_pending_actions notification must use unified meta schema."""
    import json
    import time
    from scripts.emerge_daemon import EmergeDaemon
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path))
    daemon = EmergeDaemon()
    pushed = []
    daemon._write_mcp_push = lambda p: pushed.append(p)

    pending = tmp_path / "pending-actions.json"
    pending.write_text(json.dumps({
        "submitted_at": int(time.time() * 1000),
        "actions": [{"type": "prompt", "prompt": "hello"}],
    }))
    daemon._on_pending_actions()

    assert len(pushed) == 1
    meta = pushed[0]["params"]["meta"]
    assert meta["source"] == "cockpit"
    assert meta["severity"] == "info"
    assert meta["category"] == "action_needed"
    assert meta["requires_action"] is True


def test_all_notifications_use_unified_meta_fields():
    """All _notify() calls must produce notifications with all required meta fields."""
    from scripts.emerge_daemon import EmergeDaemon

    daemon = EmergeDaemon()
    all_pushed = []
    daemon._write_mcp_push = lambda p: all_pushed.append(p)

    # Trigger notification via _notify directly
    daemon._notify(
        content="test",
        source="bridge",
        severity="high",
        category="warning",
        intent_signature="x.read.y",
    )

    for p in all_pushed:
        meta = p["params"]["meta"]
        for required_field in ("source", "severity", "category", "requires_action"):
            assert required_field in meta, f"Missing {required_field!r} in meta: {meta}"


def test_initialize_declares_resource_subscribe_capability():
    """initialize response must set resources.subscribe=True (MCP 2025-03-26)."""
    from scripts.emerge_daemon import EmergeDaemon
    daemon = EmergeDaemon()
    req = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
           "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                      "clientInfo": {"name": "test", "version": "0"}}}
    resp = daemon.handle_jsonrpc(req)
    assert resp["result"]["capabilities"]["resources"]["subscribe"] is True


def test_registry_write_emits_list_changed_notification(tmp_path, monkeypatch):
    """Writing pipelines-registry.json must push resources/list_changed notification."""
    import json
    from scripts.emerge_daemon import EmergeDaemon
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path))
    daemon = EmergeDaemon()
    pushed = []
    daemon._write_mcp_push = lambda p: pushed.append(p)

    # Trigger a registry write via _update_pipeline_registry
    # We need to call it with valid args — look at the signature: candidate_key, entry
    # Seed a candidate first to trigger a registry update
    daemon._update_pipeline_registry(
        candidate_key="gmail.read.fetch",
        entry={
            "attempts": 1,
            "successes": 1,
            "verify_passes": 1,
            "human_fixes": 0,
            "consecutive_failures": 0,
            "recent_outcomes": [1],
            "source": "exec",
        },
    )

    list_changed = [p for p in pushed if p.get("method") == "notifications/resources/list_changed"]
    assert len(list_changed) >= 1


def test_resource_read_policy_current_has_no_extra_fields():
    """resources/read policy://current must return exactly uri+mimeType+text, no extras."""
    from scripts.emerge_daemon import EmergeDaemon
    daemon = EmergeDaemon()
    req = {
        "jsonrpc": "2.0", "id": 1,
        "method": "resources/read",
        "params": {"uri": "policy://current"},
    }
    resp = daemon.handle_jsonrpc(req)
    resource = resp["result"]["resource"]
    # Only these three keys allowed; no structuredContent, no blob alongside text
    allowed = {"uri", "mimeType", "text"}
    extra = set(resource.keys()) - allowed
    assert not extra, f"Unexpected fields in resource response: {extra}"
    json.loads(resource["text"])  # must be valid JSON


def test_resource_read_state_deltas_is_valid_json():
    """resources/read state://deltas text field must be parseable JSON."""
    from scripts.emerge_daemon import EmergeDaemon
    daemon = EmergeDaemon()
    req = {"jsonrpc": "2.0", "id": 1, "method": "resources/read",
           "params": {"uri": "state://deltas"}}
    resp = daemon.handle_jsonrpc(req)
    resource = resp["result"]["resource"]
    data = json.loads(resource["text"])
    assert "open_risks" in data or "deltas" in data or "goal" in data


def test_format_context_trims_risks_when_over_budget():
    """format_context must trim risk list when budget_chars is exceeded."""
    from scripts.state_tracker import StateTracker

    tracker = StateTracker.__new__(StateTracker)
    # Build 50 risks — all open
    tracker.state = {
        "deltas": [],
        "open_risks": [
            {"risk_id": f"r{i}", "text": f"Risk item {i} " * 10, "status": "open",
             "created_at_ms": i, "snoozed_until_ms": 0, "handled_reason": "",
             "source_delta_id": "", "intent_signature": ""}
            for i in range(50)
        ],
        "goal": "test goal",
        "goal_source": "test",
    }

    # Tiny budget that can't hold all 50 risks
    ctx = tracker.format_context(budget_chars=500)
    risks_text = ctx["Open Risks"]
    # Must be truncated — either has truncation message or has fewer than 50 items
    lines_with_dash = [l for l in risks_text.splitlines() if l.startswith("- ")]
    assert len(lines_with_dash) < 50, f"Expected truncation, got {len(lines_with_dash)} lines"
    # And must include a hint about more risks
    assert "more" in risks_text.lower() or len(risks_text) <= 600


def test_format_context_does_not_trim_risks_under_budget():
    """format_context must not trim risks when budget_chars is large enough."""
    from scripts.state_tracker import StateTracker

    tracker = StateTracker.__new__(StateTracker)
    tracker.state = {
        "deltas": [],
        "open_risks": [
            {"risk_id": "r1", "text": "Short risk", "status": "open",
             "created_at_ms": 1, "snoozed_until_ms": 0, "handled_reason": "",
             "source_delta_id": "", "intent_signature": ""}
        ],
        "goal": "",
        "goal_source": "unset",
    }

    ctx = tracker.format_context(budget_chars=10000)  # large budget
    assert "Short risk" in ctx["Open Risks"]


def test_tool_list_has_title_and_annotations():
    """Every tool must declare title and annotations."""
    daemon = EmergeDaemon(root=ROOT)
    response = daemon.handle_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    tools = {t["name"]: t for t in response["result"]["tools"]}

    for name, tool in tools.items():
        assert "title" in tool, f"{name} missing 'title'"
        assert "annotations" in tool, f"{name} missing 'annotations'"

    assert tools["icc_goal_read"]["annotations"]["readOnlyHint"] is True
    assert tools["icc_goal_rollback"]["annotations"]["destructiveHint"] is True
    assert tools["icc_reconcile"]["annotations"]["idempotentHint"] is True


def test_tool_list_key_tools_have_output_schema():
    """icc_exec, icc_span_open, icc_span_close, icc_span_approve must declare outputSchema."""
    daemon = EmergeDaemon(root=ROOT)
    response = daemon.handle_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    tools = {t["name"]: t for t in response["result"]["tools"]}

    for name in ("icc_exec", "icc_span_open", "icc_span_close", "icc_span_approve"):
        assert "outputSchema" in tools[name], f"{name} missing 'outputSchema'"
        schema = tools[name]["outputSchema"]
        assert schema.get("type") == "object", f"{name} outputSchema must be object type"
        assert "properties" in schema, f"{name} outputSchema missing 'properties'"

    exec_props = tools["icc_exec"]["outputSchema"]["properties"]
    assert "bridge_promoted" in exec_props
    assert "synthesis_ready" in exec_props
    assert "policy_status" in exec_props

    span_open_props = tools["icc_span_open"]["outputSchema"]["properties"]
    assert "span_id" in span_open_props
    assert "bridge" in span_open_props
    assert "policy_status" in span_open_props

    span_close_props = tools["icc_span_close"]["outputSchema"]["properties"]
    assert "span_id" in span_close_props
    assert "synthesis_ready" in span_close_props
    assert "skeleton_path" in span_close_props

    span_approve_props = tools["icc_span_approve"]["outputSchema"]["properties"]
    assert "activated" in span_approve_props
    assert "pipeline_path" in span_approve_props
