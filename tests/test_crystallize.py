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
        assert result.get("ok") is True
        py_path = Path(result["py_path"])
        yaml_path = Path(result["yaml_path"])
        assert py_path.exists(), f"expected {py_path}"
        assert yaml_path.exists(), f"expected {yaml_path}"
        py_src = py_path.read_text()
        assert "def run_read" in py_src
        assert "def verify_read" in py_src
        assert "__result = [{'x': 1}]" in py_src
        import yaml
        meta = yaml.safe_load(yaml_path.read_text())
        assert meta["intent_signature"] == "myconn.read.mydata"
        assert meta.get("synthesized") is True
        assert "read_steps" in meta
        assert "verify_steps" in meta
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)
        os.environ.pop("EMERGE_CONNECTOR_ROOT", None)


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
        assert result.get("ok") is True
        py_src = Path(result["py_path"]).read_text()
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
        assert result.get("ok") is True, result
        assert "icc_crystallize" not in routed_calls
        assert Path(result["py_path"]).exists()
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)
        os.environ.pop("EMERGE_CONNECTOR_ROOT", None)


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
