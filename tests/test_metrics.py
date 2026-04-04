from __future__ import annotations
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_local_jsonl_sink_appends_event(tmp_path):
    from scripts.metrics import LocalJSONLSink
    sink = LocalJSONLSink(path=tmp_path / "metrics.jsonl")
    sink.emit("pipeline.read", {"pipeline_id": "mock.read.layers", "ok": True})
    lines = (tmp_path / "metrics.jsonl").read_text().strip().split("\n")
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["event_type"] == "pipeline.read"
    assert event["pipeline_id"] == "mock.read.layers"
    assert "ts_ms" in event


def test_local_jsonl_sink_appends_multiple(tmp_path):
    from scripts.metrics import LocalJSONLSink
    sink = LocalJSONLSink(path=tmp_path / "m.jsonl")
    sink.emit("exec.call", {"target_profile": "default"})
    sink.emit("runner.retry", {"attempt": 1})
    lines = (tmp_path / "m.jsonl").read_text().strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[1])["event_type"] == "runner.retry"


def test_null_sink_does_not_write(tmp_path):
    from scripts.metrics import NullSink
    sink = NullSink()
    sink.emit("anything", {"x": 1})  # must not raise


def test_get_sink_returns_local_jsonl_by_default(tmp_path):
    from scripts.metrics import get_sink, LocalJSONLSink
    sink = get_sink({"metrics_sink": "local_jsonl"}, default_path=tmp_path / "m.jsonl")
    assert isinstance(sink, LocalJSONLSink)


def test_get_sink_returns_null_sink(tmp_path):
    from scripts.metrics import get_sink, NullSink
    sink = get_sink({"metrics_sink": "null"}, default_path=tmp_path / "m.jsonl")
    assert isinstance(sink, NullSink)


def test_daemon_emits_pipeline_read_metric(tmp_path):
    import os, sys, json
    from pathlib import Path
    ROOT = Path(__file__).resolve().parents[1]
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_SESSION_ID"] = "metric-test"
    os.environ["EMERGE_SETTINGS_PATH"] = str(tmp_path / "settings.json")
    metrics_path = tmp_path / "metrics.jsonl"
    (tmp_path / "settings.json").write_text('{"metrics_sink": "local_jsonl"}')

    try:
        from scripts.policy_config import _reset_settings_cache
        _reset_settings_cache()
        from scripts.emerge_daemon import EmergeDaemon as ReplDaemon
        daemon = ReplDaemon(root=ROOT)
        daemon._sink = __import__("scripts.metrics", fromlist=["LocalJSONLSink"]).LocalJSONLSink(path=metrics_path)
        daemon.call_tool("icc_read", {"connector": "mock", "pipeline": "layers"})
        assert metrics_path.exists()
        events = [json.loads(l) for l in metrics_path.read_text().strip().split("\n") if l]
        types = [e["event_type"] for e in events]
        assert "pipeline.read" in types
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)
        os.environ.pop("EMERGE_SETTINGS_PATH", None)
        from scripts.policy_config import _reset_settings_cache
        _reset_settings_cache()
