import json
import time
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_cmd_assets_returns_connectors_with_notes_and_scenarios(tmp_path: Path, monkeypatch):
    import os
    # Set up fake connector root
    connector_root = tmp_path / "connectors"
    (connector_root / "myfoo" / "scenarios").mkdir(parents=True)
    (connector_root / "myfoo" / "cockpit").mkdir()
    (connector_root / "myfoo" / "NOTES.md").write_text("## notes\nfoo info", encoding="utf-8")
    scenario_yaml = """
name: health-check
description: Basic liveness check
steps:
  - name: ping
    type: http_get
    base_url: "{{ env_url }}"
    path: /health
"""
    (connector_root / "myfoo" / "scenarios" / "health-check.yaml").write_text(scenario_yaml)
    (connector_root / "myfoo" / "cockpit" / "mycomp.html").write_text("<div>hello</div>")
    (connector_root / "myfoo" / "cockpit" / "mycomp.context.md").write_text("context info")

    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(connector_root))

    from scripts.repl_admin import cmd_assets
    result = cmd_assets()

    assert "myfoo" in result["connectors"]
    c = result["connectors"]["myfoo"]
    assert "## notes" in c["notes"]
    assert len(c["scenarios"]) == 1
    assert c["scenarios"][0]["name"] == "health-check"
    assert c["scenarios"][0]["step_count"] == 1
    assert len(c["components"]) == 1
    assert c["components"][0]["filename"] == "mycomp.html"
    assert c["components"][0]["context"] == "context info"


def test_cmd_assets_connector_without_notes_or_scenarios(tmp_path: Path, monkeypatch):
    connector_root = tmp_path / "connectors"
    (connector_root / "bare").mkdir(parents=True)
    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(connector_root))

    from scripts.repl_admin import cmd_assets
    result = cmd_assets()

    assert "bare" in result["connectors"]
    c = result["connectors"]["bare"]
    assert c["notes"] is None
    assert c["scenarios"] == []
    assert c["components"] == []


def test_cmd_submit_actions_writes_pending_file(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("EMERGE_REPL_ROOT", str(tmp_path))

    from scripts.repl_admin import cmd_submit_actions
    actions = [
        {"type": "pipeline-delete", "key": "pipeline::mock.read.does-not-exist"},
        {"type": "pipeline-set", "key": "pipeline::mock.read.layers", "fields": {"status": "canary"}},
    ]
    result = cmd_submit_actions(actions)

    assert result["ok"] is True
    assert result["action_count"] == 2
    pending = json.loads((tmp_path / "pending-actions.json").read_text())
    assert len(pending["actions"]) == 2
    assert pending["actions"][0]["type"] == "pipeline-delete"
    assert pending["submitted_at"] > 0


def test_cmd_submit_actions_atomic_write(tmp_path: Path, monkeypatch):
    """Verify tmp file is used (no partial writes)."""
    monkeypatch.setenv("EMERGE_REPL_ROOT", str(tmp_path))
    from scripts.repl_admin import cmd_submit_actions
    cmd_submit_actions([{"type": "pipeline-delete", "key": "pipeline::x"}])
    # tmp file should not exist after rename
    assert not (tmp_path / "pending-actions.json.tmp").exists()
    assert (tmp_path / "pending-actions.json").exists()


import urllib.request
import threading


def _start_test_server(tmp_path, monkeypatch):
    """Start cockpit server and return base URL."""
    monkeypatch.setenv("EMERGE_REPL_ROOT", str(tmp_path))
    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(tmp_path / "connectors"))
    (tmp_path / "connectors").mkdir(exist_ok=True)
    from scripts.repl_admin import cmd_serve
    result = cmd_serve(port=0, open_browser=False)
    assert result["ok"]
    return result["url"]


def test_serve_get_policy_returns_json(tmp_path, monkeypatch):
    url = _start_test_server(tmp_path, monkeypatch)
    with urllib.request.urlopen(f"{url}/api/policy") as resp:
        data = json.loads(resp.read())
    assert "pipelines" in data
    assert "thresholds" in data


def test_serve_get_assets_returns_connectors(tmp_path, monkeypatch):
    url = _start_test_server(tmp_path, monkeypatch)
    with urllib.request.urlopen(f"{url}/api/assets") as resp:
        data = json.loads(resp.read())
    assert "connectors" in data


def test_serve_post_submit_writes_pending(tmp_path, monkeypatch):
    url = _start_test_server(tmp_path, monkeypatch)
    actions = [{"type": "pipeline-delete", "key": "pipeline::x"}]
    body = json.dumps({"actions": actions}).encode()
    req = urllib.request.Request(
        f"{url}/api/submit", data=body,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    assert data["ok"] is True
    assert (tmp_path / "pending-actions.json").exists()


def test_serve_get_status_returns_ok(tmp_path, monkeypatch):
    url = _start_test_server(tmp_path, monkeypatch)
    with urllib.request.urlopen(f"{url}/api/status") as resp:
        data = json.loads(resp.read())
    assert data["ok"] is True


def test_serve_get_root_returns_html(tmp_path, monkeypatch):
    url = _start_test_server(tmp_path, monkeypatch)
    with urllib.request.urlopen(f"{url}/") as resp:
        body = resp.read().decode()
    assert "cockpit" in body.lower() or "<!DOCTYPE" in body or "<html" in body


def test_serve_component_path_traversal_rejected(tmp_path, monkeypatch):
    import urllib.error
    url = _start_test_server(tmp_path, monkeypatch)
    try:
        urllib.request.urlopen(f"{url}/api/components/../../../etc/passwd")
        assert False, "Should have raised"
    except urllib.error.HTTPError as e:
        assert e.code == 404


def test_cockpit_full_flow(tmp_path, monkeypatch):
    """Full flow: start server, check assets, submit actions, verify pending-actions.json written."""
    import urllib.request

    # Set up connector with NOTES and scenario
    connector_root = tmp_path / "connectors"
    (connector_root / "myconn" / "scenarios").mkdir(parents=True)
    (connector_root / "myconn" / "NOTES.md").write_text("# Notes\nsome info", encoding="utf-8")
    (connector_root / "myconn" / "scenarios" / "test.yaml").write_text(
        "name: test\ndescription: a test\nsteps:\n  - name: s1\n    type: http_get\n    base_url: '{{ env_url }}'\n    path: /health\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(connector_root))
    monkeypatch.setenv("EMERGE_REPL_ROOT", str(tmp_path))

    from scripts.repl_admin import cmd_serve
    r = cmd_serve(port=0, open_browser=False)
    base = r["url"]

    # Assets endpoint includes connector
    with urllib.request.urlopen(f"{base}/api/assets") as resp:
        assets = json.loads(resp.read())
    assert "myconn" in assets["connectors"]
    assert assets["connectors"]["myconn"]["notes"] is not None
    assert len(assets["connectors"]["myconn"]["scenarios"]) == 1

    # Submit actions
    actions = [
        {"type": "notes-comment", "connector": "myconn", "comment": "test comment"},
        {"type": "scenario-run", "connector": "myconn", "scenario": "test", "args": {"env_url": "http://localhost"}},
    ]
    body = json.dumps({"actions": actions}).encode()
    req = urllib.request.Request(
        f"{base}/api/submit", data=body,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
    assert result["ok"]

    # pending-actions.json should be written
    pending = json.loads((tmp_path / "pending-actions.json").read_text())
    assert len(pending["actions"]) == 2
    assert pending["actions"][0]["type"] == "notes-comment"
    assert pending["actions"][1]["type"] == "scenario-run"
