"""Tests for icc_crystallize tool."""
from __future__ import annotations
import json
import os
from pathlib import Path
import sys
import pytest

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
        # Seed WAL with a synthesizable exec
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


def test_runner_executor_crystallize_reads_local_wal(tmp_path):
    """RunnerExecutor._crystallize reads WAL from runner's own state_root."""
    os.environ["EMERGE_SESSION_ID"] = "runner-cryst-test"
    connector_root = tmp_path / "connectors"
    os.environ["EMERGE_CONNECTOR_ROOT"] = str(connector_root)
    try:
        from scripts.remote_runner import RunnerExecutor
        executor = RunnerExecutor(root=ROOT, state_root=tmp_path / "state")
        # Seed WAL via icc_exec so it lands in runner's own state_root
        executor.run("icc_exec", {
            "code": "__result = [{'val': 42}]",
            "intent_signature": "rc.read.vals",
            "no_replay": False,
        })
        result = executor.run("icc_crystallize", {
            "intent_signature": "rc.read.vals",
            "connector": "rc",
            "pipeline_name": "vals",
            "mode": "read",
        })
        assert result.get("isError") is not True, result
        py_path = Path(result["py_path"])
        assert py_path.exists()
        assert "def run_read" in py_path.read_text()
        assert "__result = [{'val': 42}]" in py_path.read_text()
    finally:
        os.environ.pop("EMERGE_SESSION_ID", None)
        os.environ.pop("EMERGE_CONNECTOR_ROOT", None)


def test_daemon_crystallize_routes_to_runner_client(tmp_path, monkeypatch):
    """When a runner client is available, icc_crystallize is forwarded to it."""
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_SESSION_ID"] = "route-cryst-test"
    os.environ["EMERGE_CONNECTOR_ROOT"] = str(tmp_path / "connectors")
    try:
        from scripts.emerge_daemon import EmergeDaemon

        calls: list[tuple[str, dict]] = []

        class _FakeClient:
            def call_tool(self, name, arguments):
                calls.append((name, arguments))
                return {"ok": True, "routed": True, "content": [{"type": "text", "text": "{}"}]}

        class _FakeRouter:
            def find_client(self, arguments):
                return _FakeClient()

        daemon = EmergeDaemon(root=ROOT)
        daemon._runner_router = _FakeRouter()

        result = daemon.call_tool("icc_crystallize", {
            "intent_signature": "rc.read.vals",
            "connector": "rc",
            "pipeline_name": "vals",
            "mode": "read",
            "target_profile": "gpu-worker",
        })

        assert result.get("routed") is True
        assert len(calls) == 1
        assert calls[0][0] == "icc_crystallize"
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
