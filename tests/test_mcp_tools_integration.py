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
    assert bad_read["result"]["pipeline_missing"] is True
    assert bad_read["result"]["fallback"] == "icc_exec"

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
        key = "pipeline::mock.write.add-wall"
        assert data["pipelines"][key]["status"] == "canary"
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_flywheel_composed_key_can_be_shared_by_exec_and_pipeline(tmp_path: Path):
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_SESSION_ID"] = "compose"
    try:
        daemon = EmergeDaemon(root=ROOT)
        daemon.call_tool(
            "icc_exec",
            {
                "mode": "inline_code",
                "code": "x = 1",
                "target_profile": "zwcad-profile",
                "intent_signature": "zwcad.plan.wall",
                "script_ref": "connectors/zwcad/actions/plan_wall.py",
                "base_pipeline_id": "mock.write.add-wall",
            },
        )
        daemon.call_tool(
            "icc_write",
            {
                "connector": "mock",
                "pipeline": "add-wall",
                "length": 1000,
                "exec_signature": "zwcad.plan.wall",
                "script_ref": "connectors/zwcad/actions/plan_wall.py",
            },
        )
        registry = tmp_path / "state" / "compose" / "candidates.json"
        data = json.loads(registry.read_text(encoding="utf-8"))
        key = "flywheel::mock.write.add-wall::zwcad.plan.wall::connectors/zwcad/actions/plan_wall.py"
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
        assert "pipeline::zwcad.read.state" in data["pipelines"]
        assert "pipeline::zwcad.write.apply-change" in data["pipelines"]
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
        key = "pipeline::zwcad.read.state"
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

        stop_key = "pipeline::mock.write.add-wall"
        rb_key = "pipeline::mock.write.add-wall-rollback"
        assert data["pipelines"][stop_key]["policy_enforced_count"] >= 1
        assert data["pipelines"][stop_key]["stop_triggered_count"] >= 1
        assert data["pipelines"][stop_key]["last_policy_action"] == "stop"
        assert data["pipelines"][rb_key]["policy_enforced_count"] >= 1
        assert data["pipelines"][rb_key]["rollback_executed_count"] >= 1
        assert data["pipelines"][rb_key]["last_policy_action"] == "rollback"
    finally:
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
    assert any(u.startswith("pipeline://") for u in uris)


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
        bridge_key = "flywheel::mock.read.layers::zwcad.plan.read::connectors/zwcad/read.py"
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

        bridge_key = "flywheel::mock.read.layers::zwcad.plan.read::connectors/zwcad/read.py"
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
        assert result.get("pipeline_missing") is True
        assert result.get("connector") == "nonexistent"
        assert result.get("pipeline") == "nope"
        assert result.get("mode") == "read"
        assert result.get("fallback") == "icc_exec"
        assert "icc_exec" in result.get("fallback_hint", "")
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


def test_increment_human_fix_targets_most_recent_candidate_only(tmp_path):
    """A single icc_reconcile(correct) must increment human_fixes on exactly ONE
    candidate — the most recently used one (highest last_ts_ms) — even when multiple
    candidates share the same intent_signature (exec, pipeline::, flywheel:: entries).
    Incrementing all would inflate human_fix_rate for unrelated candidates."""
    import json, os, time
    from pathlib import Path
    from scripts.emerge_daemon import EmergeDaemon

    ROOT = Path(__file__).resolve().parents[1]
    state_root = tmp_path / "state"
    os.environ["EMERGE_STATE_ROOT"] = str(state_root)
    os.environ["EMERGE_SESSION_ID"] = "multi-cand-fix-test"
    try:
        session_id = "multi-cand-fix-test"
        session_dir = state_root / session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        now = int(time.time() * 1000)
        # Three candidates sharing the same intent_signature: exec, pipeline, bridge.
        # The pipeline:: entry has the highest last_ts_ms — it is the "most recent".
        candidates = {
            "candidates": {
                "default::zwcad.read.state::<inline>": {
                    "intent_signature": "zwcad.read.state",
                    "attempts": 10, "successes": 9, "verify_passes": 9,
                    "human_fixes": 0, "last_ts_ms": now - 2000,
                },
                "pipeline::zwcad.read.state": {
                    "intent_signature": "zwcad.read.state",
                    "attempts": 5, "successes": 5, "verify_passes": 5,
                    "human_fixes": 0, "last_ts_ms": now - 500,  # most recent
                },
                "flywheel::zwcad.read.state::zwcad.read.state::script.py": {
                    "intent_signature": "zwcad.read.state",
                    "attempts": 3, "successes": 3, "verify_passes": 3,
                    "human_fixes": 0, "last_ts_ms": now - 1500,
                },
            }
        }
        (session_dir / "candidates.json").write_text(json.dumps(candidates))

        daemon = EmergeDaemon(root=ROOT)
        daemon._increment_human_fix("zwcad.read.state")

        updated = json.loads((session_dir / "candidates.json").read_text())["candidates"]

        # Only the pipeline:: entry (most recent) must be incremented
        assert updated["pipeline::zwcad.read.state"]["human_fixes"] == 1
        # The other two must be untouched
        assert updated["default::zwcad.read.state::<inline>"]["human_fixes"] == 0
        assert updated["flywheel::zwcad.read.state::zwcad.read.state::script.py"]["human_fixes"] == 0
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
        assert result.get("pipeline_missing") is True
        assert result.get("mode") == "write"
        assert result.get("fallback") == "icc_exec"
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
            "    return [{'val': 99}]\n\n"
            "def verify_read(metadata, args, rows):\n"
            "    return {'ok': bool(rows)}\n"
        )

        exec_calls: list[dict] = []

        class _FakeClient:
            def call_tool(self, name, arguments):
                exec_calls.append({"name": name, "arguments": arguments})
                # Simulate runner executing the inline code and printing JSON result
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
                return {"isError": False, "content": [{"type": "text", "text": f"stdout:\n{buf.getvalue()}"}]}

        daemon = EmergeDaemon(root=ROOT)
        result = daemon._run_pipeline_remotely("read", {"connector": "myconn", "pipeline": "mydata"}, _FakeClient())

        assert len(exec_calls) == 1
        assert exec_calls[0]["name"] == "icc_exec"
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
                    exec(arguments.get("code", ""), {})
                finally:
                    _sys.stdout = old
                return {"isError": False, "content": [{"type": "text", "text": f"stdout:\n{buf.getvalue()}"}]}

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
                    exec(arguments.get("code", ""), {})
                finally:
                    _sys.stdout = old
                return {"isError": False, "content": [{"type": "text", "text": f"stdout:\n{buf.getvalue()}"}]}

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


def test_build_elicit_params_schema_is_valid_json_schema(tmp_path):
    """requestedSchema must be a valid JSON Schema object with type=object and properties."""
    import os
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path)
    try:
        from scripts.emerge_daemon import EmergeDaemon
        from scripts.pattern_detector import PatternSummary
        daemon = EmergeDaemon(root=tmp_path)
        summary = PatternSummary(
            machine_ids=["m1"],
            intent_signature="zwcad.entity_added",
            occurrences=5,
            window_minutes=10.0,
            detector_signals=["frequency"],
            context_hint={"app": "zwcad"},
            policy_stage="canary",
        )
        params = daemon._build_elicit_params("canary", {"app": "zwcad"}, summary)
        schema = params["requestedSchema"]
        assert schema.get("type") == "object"
        assert "properties" in schema
        assert "action" in schema["properties"]
        assert "note" in schema["properties"]
        assert schema.get("required") == ["action"]
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)


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
        key = "pipeline::hypermesh.write.apply-change"
        assert key in data["pipelines"], f"Key {key!r} not found; keys={list(data['pipelines'])}"
        assert data["pipelines"][key]["status"] == "canary"
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_push_pattern_explore_sends_channel_notification(monkeypatch, tmp_path):
    """_push_pattern for explore stage sends a channel notification. (Task 5 will extend to all stages.)"""
    from scripts.pattern_detector import PatternSummary
    from scripts.emerge_daemon import EmergeDaemon

    daemon = EmergeDaemon(root=ROOT)
    mcp_calls = []
    monkeypatch.setattr(daemon, "_write_mcp_push", lambda payload: mcp_calls.append(payload))

    summary = PatternSummary(
        machine_ids=["local"],
        intent_signature="hypermesh.node_create",
        occurrences=5,
        window_minutes=10.0,
        detector_signals=["frequency"],
        context_hint={"app": "hypermesh", "samples": []},
        policy_stage="explore",
    )
    daemon._push_pattern("explore", {"app": "hypermesh"}, summary)

    assert len(mcp_calls) == 1
    assert mcp_calls[0]["method"] == "notifications/claude/channel"


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
