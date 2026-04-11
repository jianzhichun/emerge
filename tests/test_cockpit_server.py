import json
import time
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_cmd_assets_returns_connectors_with_notes_and_components(tmp_path: Path, monkeypatch):
    import os
    connector_root = tmp_path / "connectors"
    (connector_root / "myfoo" / "cockpit").mkdir(parents=True)
    (connector_root / "myfoo" / "NOTES.md").write_text("## notes\nfoo info", encoding="utf-8")
    (connector_root / "myfoo" / "cockpit" / "mycomp.html").write_text("<div>hello</div>")
    (connector_root / "myfoo" / "cockpit" / "mycomp.context.md").write_text("context info")

    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(connector_root))

    from scripts.repl_admin import cmd_assets
    result = cmd_assets()

    assert "myfoo" in result["connectors"]
    c = result["connectors"]["myfoo"]
    assert "## notes" in c["notes"]
    assert "scenarios" not in c
    assert len(c["components"]) == 1
    assert c["components"][0]["filename"] == "mycomp.html"
    assert c["components"][0]["context"] == "context info"


def test_cmd_assets_connector_without_notes_or_components(tmp_path: Path, monkeypatch):
    connector_root = tmp_path / "connectors"
    (connector_root / "bare").mkdir(parents=True)
    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(connector_root))

    from scripts.repl_admin import cmd_assets
    result = cmd_assets()

    assert "bare" in result["connectors"]
    c = result["connectors"]["bare"]
    assert c["notes"] is None
    assert "scenarios" not in c
    assert c["components"] == []


def test_cmd_submit_actions_writes_pending_file(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("EMERGE_REPL_ROOT", str(tmp_path))

    from scripts.repl_admin import cmd_submit_actions
    actions = [
        {"type": "pipeline-delete", "key": "mock.read.does-not-exist"},
        {"type": "pipeline-set", "key": "mock.read.layers", "fields": {"status": "canary"}},
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
    cmd_submit_actions([{"type": "pipeline-delete", "key": "x"}])
    # tmp file should not exist after rename
    assert not (tmp_path / "pending-actions.json.tmp").exists()
    assert (tmp_path / "pending-actions.json").exists()


import urllib.request
import threading


def _start_test_server(tmp_path, monkeypatch):
    """Start cockpit server and return base URL."""
    # Set BOTH EMERGE_REPL_ROOT and EMERGE_STATE_ROOT so _cockpit_pid_path()
    # (which uses _resolve_state_root → EMERGE_STATE_ROOT) also points to tmp_path,
    # preventing cmd_serve from reusing a real running cockpit server.
    monkeypatch.setenv("EMERGE_REPL_ROOT", str(tmp_path))
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path))
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


def test_serve_inject_component_merged_into_assets_and_servable(tmp_path, monkeypatch):
    """POST /api/inject-component must surface in /api/assets and /api/components/... (regression)."""
    import scripts.repl_admin as ra

    ra._COCKPIT_INJECTED_HTML.clear()
    connector_root = tmp_path / "connectors"
    (connector_root / "acme").mkdir(parents=True)
    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(connector_root))
    monkeypatch.setenv("EMERGE_REPL_ROOT", str(tmp_path))
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path))
    from scripts.repl_admin import cmd_serve

    url = cmd_serve(port=0, open_browser=False)["url"]
    inj = json.dumps({"connector": "acme", "html": "<div id=\"inj\">ok</div>"}).encode()
    req = urllib.request.Request(
        f"{url}/api/inject-component",
        data=inj,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        assert json.loads(resp.read())["ok"] is True

    with urllib.request.urlopen(f"{url}/api/assets") as resp:
        assets = json.loads(resp.read())
    names = [c["filename"] for c in assets["connectors"]["acme"]["components"]]
    assert "injected-runtime-0.html" in names

    with urllib.request.urlopen(f"{url}/api/components/acme/injected-runtime-0.html") as resp:
        body = resp.read()
    assert b"inj" in body and b"ok" in body


def test_serve_post_submit_writes_pending(tmp_path, monkeypatch):
    url = _start_test_server(tmp_path, monkeypatch)
    actions = [{"type": "pipeline-delete", "key": "x"}]
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
    assert "cc_listening" not in data, "cc_listening removed — use server_online"
    assert data["server_online"] is True
    assert isinstance(data["pending"], bool)


def test_serve_status_and_submit_prefer_repl_root(tmp_path, monkeypatch):
    """Cockpit handshake files should consistently use EMERGE_REPL_ROOT."""
    repl_root = tmp_path / "repl-root"
    state_root = tmp_path / "state-root"
    monkeypatch.setenv("EMERGE_REPL_ROOT", str(repl_root))
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(state_root))
    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(tmp_path / "connectors"))
    (tmp_path / "connectors").mkdir(exist_ok=True)

    from scripts.repl_admin import cmd_serve
    base = cmd_serve(port=0, open_browser=False)["url"]

    actions = [{"type": "pipeline-delete", "key": "x"}]
    body = json.dumps({"actions": actions}).encode()
    req = urllib.request.Request(
        f"{base}/api/submit", data=body,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req) as resp:
        submit = json.loads(resp.read())
    assert submit["ok"] is True
    assert (repl_root / "pending-actions.json").exists()
    assert not (state_root / "pending-actions.json").exists()

    with urllib.request.urlopen(f"{base}/api/status") as resp:
        status = json.loads(resp.read())
    assert status["ok"] is True
    assert status["pending"] is True


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

    connector_root = tmp_path / "connectors"
    (connector_root / "myconn").mkdir(parents=True)
    (connector_root / "myconn" / "NOTES.md").write_text("# Notes\nsome info", encoding="utf-8")
    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(connector_root))
    monkeypatch.setenv("EMERGE_REPL_ROOT", str(tmp_path))
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path))

    from scripts.repl_admin import cmd_serve
    r = cmd_serve(port=0, open_browser=False)
    base = r["url"]

    with urllib.request.urlopen(f"{base}/api/assets") as resp:
        assets = json.loads(resp.read())
    assert "myconn" in assets["connectors"]
    assert assets["connectors"]["myconn"]["notes"] is not None
    assert "scenarios" not in assets["connectors"]["myconn"]

    actions = [
        {"type": "notes-comment", "connector": "myconn", "comment": "test comment"},
        {
            "type": "tool-call",
            "intent_signature": "myconn.write.apply-test",
            "call": {
                "tool": "icc_write",
                "arguments": {
                    "connector": "myconn",
                    "pipeline": "apply-test",
                    "scenario": "test",
                    "env_url": "http://localhost",
                },
            },
            "auto": {"mode": "assist", "crystallize_when_synthesis_ready": True},
            "flywheel": {"status": "explore", "synthesis_ready": False},
        },
    ]
    body = json.dumps({"actions": actions}).encode()
    req = urllib.request.Request(
        f"{base}/api/submit", data=body,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
    assert result["ok"]

    pending = json.loads((tmp_path / "pending-actions.json").read_text())
    assert len(pending["actions"]) == 2
    assert pending["actions"][0]["type"] == "notes-comment"
    assert pending["actions"][1]["type"] == "tool-call"


def test_session_reset_blocked_when_span_active(tmp_path, monkeypatch):
    """session/reset must refuse when active_span_id is present in state."""
    monkeypatch.setenv("EMERGE_REPL_ROOT", str(tmp_path))
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(tmp_path / "connectors"))
    (tmp_path / "connectors").mkdir(exist_ok=True)

    from scripts.policy_config import default_hook_state_root, pin_plugin_data_path_if_present
    from scripts.state_tracker import load_tracker, save_tracker

    pin_plugin_data_path_if_present()
    state_path = Path(default_hook_state_root()) / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tracker = load_tracker(state_path)
    tracker.state["active_span_id"] = "span-123"
    tracker.state["active_span_intent"] = "gmail.read.fetch"
    save_tracker(state_path, tracker)

    from scripts.repl_admin import cmd_control_plane_session_reset
    result = cmd_control_plane_session_reset(confirm="RESET")

    assert result["ok"] is False
    assert "active_span" in result.get("error", "").lower() or "span" in result.get("error", "").lower()
