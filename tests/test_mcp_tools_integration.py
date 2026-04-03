import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.repl_daemon import ReplDaemon


def test_tools_call_routes_exec_read_write_in_same_runtime():
    daemon = ReplDaemon(root=ROOT)

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
    daemon = ReplDaemon(root=ROOT)
    listed = daemon.handle_jsonrpc({"jsonrpc": "2.0", "id": 99, "method": "tools/list", "params": {}})
    names = [t["name"] for t in listed["result"]["tools"]]
    assert "icc_state_status" not in names
    assert "icc_state_clear" not in names


def test_tools_call_returns_error_payload_for_missing_pipeline_and_script(tmp_path: Path):
    daemon = ReplDaemon(root=ROOT)

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
    assert bad_read["result"]["isError"] is True
    assert "icc_read failed" in bad_read["result"]["content"][0]["text"]

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
    os.environ["REPL_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["REPL_SESSION_ID"] = "pipeline-policy"
    try:
        daemon = ReplDaemon(root=ROOT)
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

        reg = tmp_path / "state" / "pipeline-policy" / "pipelines-registry.json"
        data = json.loads(reg.read_text(encoding="utf-8"))
        key = "pipeline::mock.write.add-wall"
        assert data["pipelines"][key]["status"] == "canary"
    finally:
        os.environ.pop("REPL_STATE_ROOT", None)
        os.environ.pop("REPL_SESSION_ID", None)


def test_l15_composed_key_can_be_shared_by_exec_and_pipeline(tmp_path: Path):
    os.environ["REPL_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["REPL_SESSION_ID"] = "compose"
    try:
        daemon = ReplDaemon(root=ROOT)
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
        key = "l15::mock.write.add-wall::zwcad.plan.wall::connectors/zwcad/actions/plan_wall.py"
        assert data["candidates"][key]["total_calls"] == 2
    finally:
        os.environ.pop("REPL_STATE_ROOT", None)
        os.environ.pop("REPL_SESSION_ID", None)


def test_tools_call_handles_null_params_without_crash():
    daemon = ReplDaemon(root=ROOT)
    out = daemon.handle_jsonrpc(
        {"jsonrpc": "2.0", "id": 101, "method": "tools/call", "params": None}
    )
    assert out["result"]["isError"] is True
    assert "Unknown tool" in out["result"]["content"][0]["text"]


def test_pipeline_policy_metrics_are_recorded_for_stop_and_rollback(tmp_path: Path):
    os.environ["REPL_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["REPL_SESSION_ID"] = "policy-metrics"
    try:
        daemon = ReplDaemon(root=ROOT)
        daemon.call_tool(
            "icc_write",
            {"connector": "mock", "pipeline": "add-wall", "length": 0},
        )
        daemon.call_tool(
            "icc_write",
            {"connector": "mock", "pipeline": "add-wall-rollback", "length": 1000},
        )

        reg = tmp_path / "state" / "policy-metrics" / "pipelines-registry.json"
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
        os.environ.pop("REPL_STATE_ROOT", None)
        os.environ.pop("REPL_SESSION_ID", None)
