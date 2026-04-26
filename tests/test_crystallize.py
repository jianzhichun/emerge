"""Tests for icc_crystallize tool."""
from __future__ import annotations
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_icc_crystallize_generates_pipeline_files(tmp_path):
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_SESSION_ID"] = "cryst-test"
    connector_root = tmp_path / "connectors"
    os.environ["EMERGE_CONNECTOR_ROOT"] = str(connector_root)
    try:
        from scripts.emerge_daemon import EmergeDaemon
        daemon = EmergeDaemon(root=ROOT)
        daemon.call_tool("icc_exec", {
            "code": "__result = [{'x': 1}]",
            "intent_signature": "myconn.read.mydata",
            "no_replay": False,
        })
        result = daemon.call_tool("icc_crystallize", {
            "intent_signature": "myconn.read.mydata",
            "connector": "myconn",
            "pipeline_name": "mydata",
            "mode": "read",
        })
        assert result.get("isError") is not True, result
        body = result.get("structuredContent", {})
        assert body.get("ok") is True
        py_path = Path(body["py_path"])
        yaml_path = Path(body["yaml_path"])
        assert py_path.exists(), f"expected {py_path}"
        assert yaml_path.exists(), f"expected {yaml_path}"
        py_src = py_path.read_text()
        assert "def run_read" in py_src
        assert "def verify_read" in py_src
        assert "__result = [{'x': 1}]" in py_src
        import yaml
        meta = yaml.safe_load(yaml_path.read_text())
        assert meta["intent_signature"] == "myconn.read.mydata"
        assert meta.get("synthesized") is True  # yaml meta field, not envelope
        assert "read_steps" in meta
        assert "verify_steps" in meta
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)
        os.environ.pop("EMERGE_CONNECTOR_ROOT", None)


def test_crystallizer_refuses_wal_missing_return_var(tmp_path):
    """Strict precondition: crystallize must refuse WAL that never sets
    __result (read) or __action (write). Emitting a band-aid fallback that
    always returns "ok" strands the intent at stable while LLM keeps paying
    full cost — the broken pipeline is invisible. Better to reject up front
    and mark the intent with a synthesis_skipped_reason so a human fixes it."""
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_SESSION_ID"] = "cryst-strict"
    connector_root = tmp_path / "connectors"
    os.environ["EMERGE_CONNECTOR_ROOT"] = str(connector_root)
    try:
        from scripts.emerge_daemon import EmergeDaemon
        daemon = EmergeDaemon(root=ROOT)
        daemon.call_tool("icc_exec", {
            "code": "x = 1 + 1",  # never sets __result
            "intent_signature": "strict.read.thing",
            "no_replay": False,
        })
        result = daemon.call_tool("icc_crystallize", {
            "intent_signature": "strict.read.thing",
            "connector": "strict",
            "pipeline_name": "thing",
            "mode": "read",
        })
        assert result.get("isError") is True
        assert "__result" in result["content"][0]["text"]

        py_path = connector_root / "strict" / "pipelines" / "read" / "thing.py"
        assert not py_path.exists(), "no pipeline file must be emitted"

        # Registry marks the intent so auto-crystallize does not keep retrying.
        from scripts.intent_registry import IntentRegistry
        reg = IntentRegistry.load(tmp_path / "state")
        if "strict.read.thing" in reg["intents"]:
            assert reg["intents"]["strict.read.thing"].get("synthesis_skipped_reason") == (
                "missing___result_assignment"
            )

        # Write mode: same contract for __action.
        daemon.call_tool("icc_exec", {
            "code": "y = 2 + 2",
            "intent_signature": "strict.write.thing",
            "no_replay": False,
        })
        result2 = daemon.call_tool("icc_crystallize", {
            "intent_signature": "strict.write.thing",
            "connector": "strict",
            "pipeline_name": "thing",
            "mode": "write",
        })
        assert result2.get("isError") is True
        assert "__action" in result2["content"][0]["text"]

        # But a well-formed WAL still crystallizes fine.
        daemon.call_tool("icc_exec", {
            "code": "__action = {'ok': True, 'n': 3}",
            "intent_signature": "strict.write.good",
            "no_replay": False,
        })
        result3 = daemon.call_tool("icc_crystallize", {
            "intent_signature": "strict.write.good",
            "connector": "strict",
            "pipeline_name": "good",
            "mode": "write",
        })
        assert result3.get("isError") is not True, result3
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)
        os.environ.pop("EMERGE_CONNECTOR_ROOT", None)


def test_auto_crystallize_enqueues_forward_synthesis_after_policy_save(tmp_path, monkeypatch):
    """Auto-crystallize now emits a lead-agent job instead of verbatim files."""
    from scripts.emerge_daemon import EmergeDaemon
    from scripts.intent_registry import IntentRegistry
    from scripts.policy_config import PROMOTE_MIN_ATTEMPTS

    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("EMERGE_SESSION_ID", "auto-clear")
    connector_root = tmp_path / "connectors"
    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(connector_root))

    daemon = EmergeDaemon(root=ROOT)
    for i in range(PROMOTE_MIN_ATTEMPTS):
        daemon.call_tool(
            "icc_exec",
            {
                "code": f"__result = [{{'i': {i}}}]",
                "intent_signature": "mock.read.auto-clear",
                "result_var": "__result",
            },
        )

    py_path = connector_root / "mock" / "pipelines" / "read" / "auto-clear.py"
    assert not py_path.exists()
    entry = IntentRegistry.get(tmp_path / "state", "mock.read.auto-clear")
    assert entry.get("stage") == "canary"
    assert entry.get("synthesis_ready") is True
    events_path = tmp_path / "state" / "events" / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert any(event.get("type") == "forward_synthesis_pending" for event in events)


def test_icc_crystallize_write_pipeline(tmp_path):
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_SESSION_ID"] = "cryst-write-test"
    connector_root = tmp_path / "connectors"
    os.environ["EMERGE_CONNECTOR_ROOT"] = str(connector_root)
    try:
        from scripts.emerge_daemon import EmergeDaemon
        daemon = EmergeDaemon(root=ROOT)
        daemon.call_tool("icc_exec", {
            "code": "__action = {'ok': True, 'id': 'w1'}",
            "intent_signature": "myconn.write.dowork",
            "no_replay": False,
        })
        result = daemon.call_tool("icc_crystallize", {
            "intent_signature": "myconn.write.dowork",
            "connector": "myconn",
            "pipeline_name": "dowork",
            "mode": "write",
        })
        body = result.get("structuredContent", {})
        assert body.get("ok") is True
        py_src = Path(body["py_path"]).read_text()
        assert "def run_write" in py_src
        assert "def verify_write" in py_src
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)
        os.environ.pop("EMERGE_CONNECTOR_ROOT", None)


def test_icc_crystallize_always_writes_locally_even_with_runner(tmp_path):
    """icc_crystallize ignores runner_router — pipeline files always land locally."""
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_SESSION_ID"] = "cryst-local-test"
    connector_root = tmp_path / "connectors"
    os.environ["EMERGE_CONNECTOR_ROOT"] = str(connector_root)
    try:
        from scripts.emerge_daemon import EmergeDaemon

        routed_calls: list[str] = []

        class _FakeClient:
            def call_tool(self, name, arguments):
                routed_calls.append(name)
                return {"ok": True, "content": [{"type": "text", "text": "{}"}]}

        class _FakeRouter:
            def find_client(self, arguments):
                return _FakeClient()

        daemon = EmergeDaemon(root=ROOT)

        # Seed WAL locally before installing fake router
        daemon.call_tool("icc_exec", {
            "code": "__result = [{'x': 1}]",
            "intent_signature": "rc.read.vals",
            "no_replay": False,
        })

        # Now install fake router — crystallize must still write locally
        daemon._runner_router = _FakeRouter()

        result = daemon.call_tool("icc_crystallize", {
            "intent_signature": "rc.read.vals",
            "connector": "rc",
            "pipeline_name": "vals",
            "mode": "read",
        })

        # Must succeed locally — runner not called for crystallize
        body = result.get("structuredContent", {})
        assert body.get("ok") is True, result
        assert "icc_crystallize" not in routed_calls
        assert Path(body["py_path"]).exists()
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)
        os.environ.pop("EMERGE_CONNECTOR_ROOT", None)


def test_generate_yaml_span_skeleton_creates_pending_yaml(tmp_path):
    """Multi-tool spans get a YAML skeleton, not a .py skeleton."""
    from scripts.crystallizer import PipelineCrystallizer

    cryst = PipelineCrystallizer(tmp_path)
    span = {
        "actions": [
            {
                "seq": 0,
                "tool_name": "mcp__plugin_emerge__icc_exec",
                "args_hash": "abc",
                "has_side_effects": False,
                "ts_ms": 1000,
                "args_snapshot": {"intent_signature": "mock.read.layers"},
                "result_summary": {"rows_count": 3, "row_keys": ["id", "name"]},
            },
            {
                "seq": 1,
                "tool_name": "mcp__plugin_emerge__icc_exec",
                "args_hash": "def",
                "has_side_effects": True,
                "ts_ms": 2000,
                "args_snapshot": {"intent_signature": "mock.write.add-wall"},
                "result_summary": {"ok": "true"},
            },
        ]
    }
    path = cryst.generate_span_skeleton(
        intent_signature="mock.write.multi-op",
        span=span,
        connector_root=tmp_path,
    )
    assert path is not None
    assert path.suffix == ".yaml", f"Expected .yaml, got {path.suffix}"
    assert path.parent.name == "_pending"

    import yaml as _yaml
    data = _yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data.get("intent_signature") == "mock.write.multi-op"
    assert "steps" in data


def test_generate_single_tool_span_skeleton_still_produces_py(tmp_path):
    """Single-tool spans keep the existing .py skeleton path."""
    from scripts.crystallizer import PipelineCrystallizer

    cryst = PipelineCrystallizer(tmp_path)
    span = {
        "actions": [
            {
                "seq": 0,
                "tool_name": "mcp__plugin_emerge__icc_exec",
                "args_hash": "abc",
                "has_side_effects": True,
                "ts_ms": 1000,
            }
        ]
    }
    path = cryst.generate_span_skeleton(
        intent_signature="mock.write.add-wall",
        span=span,
        connector_root=tmp_path,
    )
    assert path is not None
    assert path.suffix == ".py", f"Expected .py, got {path.suffix}"


def test_icc_crystallize_no_wal_entry_returns_error(tmp_path):
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_SESSION_ID"] = "cryst-empty"
    os.environ["EMERGE_CONNECTOR_ROOT"] = str(tmp_path / "connectors")
    try:
        from scripts.emerge_daemon import EmergeDaemon
        daemon = EmergeDaemon(root=ROOT)
        result = daemon.call_tool("icc_crystallize", {
            "intent_signature": "nothing.read.exists",
            "connector": "nothing",
            "pipeline_name": "exists",
            "mode": "read",
        })
        assert result.get("isError") is True
        assert "no synthesizable" in result["content"][0]["text"].lower()
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)
        os.environ.pop("EMERGE_CONNECTOR_ROOT", None)
