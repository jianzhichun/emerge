import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.emerge_sync import export_vertical, import_vertical
from scripts.emerge_sync import (
    _file_to_intent_sig,
    _load_candidate_timestamps,
    _load_spans_timestamps,
)
from scripts.hub_config import save_hub_config


def test_file_to_intent_sig_read():
    assert _file_to_intent_sig("cloud-server", Path("read/get_instances.py")) == "cloud-server.read.get_instances"


def test_file_to_intent_sig_write():
    assert _file_to_intent_sig("cloud-server", Path("write/create_vm.py")) == "cloud-server.write.create_vm"


def test_file_to_intent_sig_unknown_depth_returns_empty():
    assert _file_to_intent_sig("cloud-server", Path("get_instances.py")) == ""


def test_load_candidate_timestamps_returns_stable_only(tmp_path):
    candidates = {
        "candidates": {
            "cs.read.a": {"status": "stable", "last_ts_ms": 500},
            "cs.read.b": {"status": "explore", "last_ts_ms": 999},
        }
    }
    (tmp_path / "span-candidates.json").write_text(json.dumps(candidates), encoding="utf-8")
    ts = _load_candidate_timestamps(tmp_path)
    assert ts == {"cs.read.a": 500}


def test_load_candidate_timestamps_missing_file(tmp_path):
    assert _load_candidate_timestamps(tmp_path) == {}


def test_load_spans_timestamps_parses_spans_json(tmp_path):
    spans = {"spans": {"cs.read.a": {"last_ts_ms": 1234}, "cs.read.b": {"last_ts_ms": 5678}}}
    (tmp_path / "spans.json").write_text(json.dumps(spans), encoding="utf-8")
    ts = _load_spans_timestamps(tmp_path)
    assert ts == {"cs.read.a": 1234, "cs.read.b": 5678}


def test_load_spans_timestamps_missing_file(tmp_path):
    assert _load_spans_timestamps(tmp_path) == {}


@pytest.fixture()
def connector_home(tmp_path, monkeypatch):
    """Fake ~/.emerge/connectors and hub worktree for tests."""
    connectors = tmp_path / "connectors"
    worktree = tmp_path / "hub-worktree"
    worktree.mkdir()
    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(connectors))
    monkeypatch.setenv("EMERGE_HUB_HOME", str(tmp_path))
    return connectors, worktree


def _make_connector(connectors: Path, name: str) -> None:
    base = connectors / name
    (base / "pipelines" / "read").mkdir(parents=True)
    (base / "pipelines" / "read" / "fetch.py").write_text("# fetch", encoding="utf-8")
    (base / "pipelines" / "read" / "fetch.yaml").write_text("connector: test", encoding="utf-8")
    (base / "NOTES.md").write_text("# Notes", encoding="utf-8")
    candidates = {
        "candidates": {
            "test.read.fetch": {"intent_signature": "test.read.fetch", "status": "stable", "last_ts_ms": 1000}
        }
    }
    (base / "span-candidates.json").write_text(json.dumps(candidates), encoding="utf-8")


def test_export_copies_pipelines_and_notes(connector_home):
    connectors, worktree = connector_home
    _make_connector(connectors, "gmail")
    export_vertical("gmail", connectors_root=connectors, hub_worktree=worktree)
    assert (worktree / "connectors" / "gmail" / "pipelines" / "read" / "fetch.py").exists()
    assert (worktree / "connectors" / "gmail" / "NOTES.md").exists()


def test_export_generates_spans_json_from_stable_candidates(connector_home):
    connectors, worktree = connector_home
    _make_connector(connectors, "gmail")
    export_vertical("gmail", connectors_root=connectors, hub_worktree=worktree)
    spans_path = worktree / "connectors" / "gmail" / "spans.json"
    assert spans_path.exists()
    spans = json.loads(spans_path.read_text())
    assert "test.read.fetch" in spans["spans"]


def test_import_overwrites_local_pipelines(connector_home):
    connectors, worktree = connector_home
    hub_dir = worktree / "connectors" / "gmail" / "pipelines" / "read"
    hub_dir.mkdir(parents=True)
    (hub_dir / "fetch.py").write_text("# remote version", encoding="utf-8")
    (hub_dir / "fetch.yaml").write_text("connector: gmail", encoding="utf-8")
    (worktree / "connectors" / "gmail" / "NOTES.md").write_text("# Remote Notes", encoding="utf-8")
    import_vertical("gmail", connectors_root=connectors, hub_worktree=worktree)
    local_py = connectors / "gmail" / "pipelines" / "read" / "fetch.py"
    assert local_py.read_text(encoding="utf-8") == "# remote version"


def test_import_merges_spans_json_newer_wins(connector_home):
    connectors, worktree = connector_home
    local_dir = connectors / "gmail"
    local_dir.mkdir(parents=True)
    local_spans = {"spans": {"gmail.read.fetch": {"intent_signature": "gmail.read.fetch", "last_ts_ms": 100}}}
    (local_dir / "spans.json").write_text(json.dumps(local_spans), encoding="utf-8")
    hub_dir = worktree / "connectors" / "gmail"
    hub_dir.mkdir(parents=True)
    hub_spans = {
        "spans": {
            "gmail.read.fetch": {"intent_signature": "gmail.read.fetch", "last_ts_ms": 999},
            "gmail.read.send": {"intent_signature": "gmail.read.send", "last_ts_ms": 500},
        }
    }
    (hub_dir / "spans.json").write_text(json.dumps(hub_spans), encoding="utf-8")
    import_vertical("gmail", connectors_root=connectors, hub_worktree=worktree)
    merged = json.loads((local_dir / "spans.json").read_text())
    assert merged["spans"]["gmail.read.fetch"]["last_ts_ms"] == 999
    assert "gmail.read.send" in merged["spans"]


def test_export_spans_json_merges_remote_spans(connector_home):
    """Exporting B's spans must not erase A's spans already in the worktree."""
    connectors, worktree = connector_home

    # A's spans already live in the worktree
    hub_conn_dir = worktree / "connectors" / "cloud-server"
    hub_conn_dir.mkdir(parents=True)
    existing_spans = {
        "spans": {
            "cloud-server.read.list_vms": {
                "intent_signature": "cloud-server.read.list_vms",
                "status": "stable",
                "last_ts_ms": 1000,
            }
        }
    }
    (hub_conn_dir / "spans.json").write_text(json.dumps(existing_spans), encoding="utf-8")

    # B has a different stable pipeline
    base = connectors / "cloud-server"
    (base / "pipelines" / "read").mkdir(parents=True)
    (base / "pipelines" / "read" / "get_quota.py").write_text("# quota", encoding="utf-8")
    candidates = {
        "candidates": {
            "cloud-server.read.get_quota": {
                "intent_signature": "cloud-server.read.get_quota",
                "status": "stable",
                "last_ts_ms": 2000,
            }
        }
    }
    (base / "span-candidates.json").write_text(json.dumps(candidates), encoding="utf-8")

    export_vertical("cloud-server", connectors_root=connectors, hub_worktree=worktree)

    spans = json.loads((hub_conn_dir / "spans.json").read_text())["spans"]
    assert "cloud-server.read.list_vms" in spans, "A's span must be preserved"
    assert "cloud-server.read.get_quota" in spans, "B's span must be added"


def test_export_vertical_preserves_remote_only_pipeline(connector_home):
    """A's pipeline already in worktree must survive B exporting a different pipeline."""
    connectors, worktree = connector_home

    # A's pipeline already in worktree (with spans.json to provide remote timestamp)
    hub_conn = worktree / "connectors" / "cloud-server"
    (hub_conn / "pipelines" / "read").mkdir(parents=True)
    (hub_conn / "pipelines" / "read" / "list_vms.py").write_text("# A's list_vms", encoding="utf-8")
    a_spans = {
        "spans": {
            "cloud-server.read.list_vms": {"intent_signature": "cloud-server.read.list_vms", "status": "stable", "last_ts_ms": 1000}
        }
    }
    (hub_conn / "spans.json").write_text(json.dumps(a_spans), encoding="utf-8")

    # B has a different pipeline locally
    base = connectors / "cloud-server"
    (base / "pipelines" / "read").mkdir(parents=True)
    (base / "pipelines" / "read" / "get_quota.py").write_text("# B's get_quota", encoding="utf-8")
    b_candidates = {
        "candidates": {
            "cloud-server.read.get_quota": {"status": "stable", "last_ts_ms": 2000}
        }
    }
    (base / "span-candidates.json").write_text(json.dumps(b_candidates), encoding="utf-8")

    export_vertical("cloud-server", connectors_root=connectors, hub_worktree=worktree)

    # A's pipeline must survive
    assert (hub_conn / "pipelines" / "read" / "list_vms.py").read_text() == "# A's list_vms"
    # B's pipeline must be added
    assert (hub_conn / "pipelines" / "read" / "get_quota.py").exists()


def test_export_vertical_local_wins_when_newer(connector_home):
    """When local last_ts_ms > remote last_ts_ms for the same pipeline, local version overwrites."""
    connectors, worktree = connector_home

    # Remote (worktree) has older version
    hub_conn = worktree / "connectors" / "cloud-server"
    (hub_conn / "pipelines" / "read").mkdir(parents=True)
    (hub_conn / "pipelines" / "read" / "fetch.py").write_text("# old remote", encoding="utf-8")
    old_spans = {
        "spans": {
            "cloud-server.read.fetch": {"status": "stable", "last_ts_ms": 100}
        }
    }
    (hub_conn / "spans.json").write_text(json.dumps(old_spans), encoding="utf-8")

    # Local has newer version
    base = connectors / "cloud-server"
    (base / "pipelines" / "read").mkdir(parents=True)
    (base / "pipelines" / "read" / "fetch.py").write_text("# new local", encoding="utf-8")
    candidates = {
        "candidates": {
            "cloud-server.read.fetch": {"status": "stable", "last_ts_ms": 999}
        }
    }
    (base / "span-candidates.json").write_text(json.dumps(candidates), encoding="utf-8")

    export_vertical("cloud-server", connectors_root=connectors, hub_worktree=worktree)

    assert (hub_conn / "pipelines" / "read" / "fetch.py").read_text() == "# new local"


def test_export_vertical_remote_wins_when_newer(connector_home):
    """When remote last_ts_ms > local last_ts_ms, local must NOT overwrite the remote pipeline."""
    connectors, worktree = connector_home

    # Remote has a newer version
    hub_conn = worktree / "connectors" / "cloud-server"
    (hub_conn / "pipelines" / "read").mkdir(parents=True)
    (hub_conn / "pipelines" / "read" / "fetch.py").write_text("# newer remote", encoding="utf-8")
    new_spans = {
        "spans": {
            "cloud-server.read.fetch": {"status": "stable", "last_ts_ms": 9999}
        }
    }
    (hub_conn / "spans.json").write_text(json.dumps(new_spans), encoding="utf-8")

    # Local has older version
    base = connectors / "cloud-server"
    (base / "pipelines" / "read").mkdir(parents=True)
    (base / "pipelines" / "read" / "fetch.py").write_text("# stale local", encoding="utf-8")
    candidates = {
        "candidates": {
            "cloud-server.read.fetch": {"status": "stable", "last_ts_ms": 50}
        }
    }
    (base / "span-candidates.json").write_text(json.dumps(candidates), encoding="utf-8")

    export_vertical("cloud-server", connectors_root=connectors, hub_worktree=worktree)

    # Remote version must be untouched
    assert (hub_conn / "pipelines" / "read" / "fetch.py").read_text() == "# newer remote"


# ── Git operation tests ─────────────────────────────────────────────────────

@pytest.fixture()
def git_setup(tmp_path, monkeypatch):
    """Create a bare remote and local hub worktree pointing to it."""
    bare_remote = tmp_path / "remote.git"
    bare_remote.mkdir()
    subprocess.run(["git", "init", "--bare", str(bare_remote)], check=True, capture_output=True)

    worktree = tmp_path / "hub-worktree"
    monkeypatch.setenv("EMERGE_HUB_HOME", str(tmp_path))

    cfg = {
        "remote": str(bare_remote),
        "branch": "emerge-hub",
        "poll_interval_seconds": 300,
        "selected_verticals": ["gmail"],
        "author": "test <test@test.com>",
    }
    save_hub_config(cfg)
    return bare_remote, worktree, tmp_path


def _init_hub_worktree(worktree: Path, remote: str, branch: str = "emerge-hub") -> None:
    """Bootstrap hub worktree — clones existing branch or creates orphan if branch doesn't exist yet."""
    worktree.mkdir(parents=True, exist_ok=True)

    _git_env = {**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@test.com",
                "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@test.com"}

    def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(list(args), cwd=str(worktree), check=check,
                              capture_output=True, env=_git_env)

    _run("git", "init")
    _run("git", "config", "user.name", "test")
    _run("git", "config", "user.email", "test@test.com")
    _run("git", "remote", "add", "origin", remote)

    fetch = _run("git", "fetch", "origin", branch, check=False)
    if fetch.returncode == 0:
        _run("git", "checkout", "-b", branch, f"origin/{branch}")
    else:
        _run("git", "checkout", "--orphan", branch)
        _run("git", "commit", "--allow-empty", "-m", "chore: init emerge-hub")
        _run("git", "push", "-u", "origin", branch)


def test_git_fetch_and_detect_no_changes(git_setup):
    from scripts.emerge_sync import git_has_remote_changes
    bare_remote, worktree, hub_home = git_setup
    _init_hub_worktree(worktree, str(bare_remote))
    assert git_has_remote_changes(worktree, "emerge-hub") is False


def test_git_push_commits_and_updates_remote(git_setup, connector_home):
    from scripts.emerge_sync import git_push
    bare_remote, worktree, hub_home = git_setup
    connectors, _ = connector_home
    _make_connector(connectors, "gmail")
    _init_hub_worktree(worktree, str(bare_remote))

    hub_dir = worktree / "connectors" / "gmail"
    hub_dir.mkdir(parents=True)
    (hub_dir / "NOTES.md").write_text("# hub notes", encoding="utf-8")

    result = git_push(worktree, "emerge-hub", connector="gmail", author="test <test@test.com>")
    assert result["ok"] is True
    assert result.get("pushed") is True


# ── Push/pull flow tests ────────────────────────────────────────────────────

def test_push_flow_exports_and_pushes(git_setup, connector_home):
    from scripts.emerge_sync import push_flow
    bare_remote, worktree, hub_home = git_setup
    connectors, _ = connector_home
    _make_connector(connectors, "gmail")
    _init_hub_worktree(worktree, str(bare_remote))

    result = push_flow("gmail", connectors_root=connectors, hub_worktree=worktree)
    assert result["ok"] is True


def test_pull_flow_imports_remote_changes(git_setup, connector_home):
    from scripts.emerge_sync import pull_flow, push_flow
    bare_remote, worktree_a, hub_home = git_setup
    connectors_a, _ = connector_home

    # Initialize BOTH worktrees before machine A pushes so machine B is behind after the push
    connectors_b = hub_home / "connectors_b"
    worktree_b = hub_home / "worktree_b"
    _init_hub_worktree(worktree_a, str(bare_remote))
    _init_hub_worktree(worktree_b, str(bare_remote))

    # Machine A pushes new content — remote now has files that machine B doesn't
    _make_connector(connectors_a, "gmail")
    push_flow("gmail", connectors_root=connectors_a, hub_worktree=worktree_a)

    # Machine B pulls the new content
    result = pull_flow("gmail", connectors_root=connectors_b, hub_worktree=worktree_b)
    assert result["ok"] is True
    assert (connectors_b / "gmail" / "pipelines" / "read" / "fetch.py").exists()


def test_apply_pending_resolutions_theirs_writes_remote_version(git_setup, connector_home):
    """'theirs' resolution must actually overwrite the local file with the remote version."""
    from scripts.emerge_sync import _apply_pending_resolutions, push_flow
    from scripts.hub_config import load_pending_conflicts, save_pending_conflicts, new_conflict_id

    bare_remote, worktree, hub_home = git_setup
    connectors, _ = connector_home

    # Seed the remote with a known file via Machine A
    _init_hub_worktree(worktree, str(bare_remote))
    _make_connector(connectors, "gmail")
    push_flow("gmail", connectors_root=connectors, hub_worktree=worktree)

    # Simulate a conflict record where the user chose "theirs"
    conflict_file = "connectors/gmail/NOTES.md"
    data = {
        "conflicts": [
            {
                "conflict_id": new_conflict_id(),
                "connector": "gmail",
                "file": conflict_file,
                "status": "resolved",
                "resolution": "theirs",
            }
        ]
    }
    save_pending_conflicts(data)

    # Overwrite the local file so we can confirm it gets replaced
    (worktree / "connectors" / "gmail" / "NOTES.md").write_text("STALE LOCAL", encoding="utf-8")
    import subprocess
    subprocess.run(["git", "add", "-A"], cwd=str(worktree), capture_output=True)
    subprocess.run(["git", "commit", "-m", "stale"], cwd=str(worktree), capture_output=True,
                   env={**__import__("os").environ,
                        "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t"})

    applied = _apply_pending_resolutions(worktree)
    assert applied is True
    # The file must now contain the remote (pushed) content, not the stale local version
    content = (worktree / "connectors" / "gmail" / "NOTES.md").read_text(encoding="utf-8")
    assert content != "STALE LOCAL"
    assert "Notes" in content  # _make_connector writes "# Notes"

    # Conflict status must be updated to "applied"
    updated = load_pending_conflicts()
    assert updated["conflicts"][0]["status"] == "applied"


def test_apply_pending_resolutions_ours_marks_applied_without_git_change(git_setup, connector_home):
    """'ours' resolution is a no-op (file stays at HEAD) but must be marked applied."""
    from scripts.emerge_sync import _apply_pending_resolutions, push_flow
    from scripts.hub_config import load_pending_conflicts, save_pending_conflicts, new_conflict_id

    bare_remote, worktree, hub_home = git_setup
    connectors, _ = connector_home

    _init_hub_worktree(worktree, str(bare_remote))
    _make_connector(connectors, "gmail")
    push_flow("gmail", connectors_root=connectors, hub_worktree=worktree)

    original_content = (worktree / "connectors" / "gmail" / "NOTES.md").read_text(encoding="utf-8")

    data = {
        "conflicts": [
            {
                "conflict_id": new_conflict_id(),
                "connector": "gmail",
                "file": "connectors/gmail/NOTES.md",
                "status": "resolved",
                "resolution": "ours",
            }
        ]
    }
    save_pending_conflicts(data)

    applied = _apply_pending_resolutions(worktree)
    assert applied is True
    # File content is unchanged — we kept our version
    assert (worktree / "connectors" / "gmail" / "NOTES.md").read_text(encoding="utf-8") == original_content
    updated = load_pending_conflicts()
    assert updated["conflicts"][0]["status"] == "applied"


def test_run_stable_events_skips_pull_when_push_conflicts(git_setup, connector_home, monkeypatch):
    """When push_flow records a conflict for a connector, pull_flow for the same connector
    must be skipped in the same cycle to avoid duplicating conflict records."""
    from unittest.mock import patch, MagicMock
    from scripts.emerge_sync import _run_stable_events
    from scripts.hub_config import append_sync_event, load_pending_conflicts, save_hub_config

    bare_remote, worktree, hub_home = git_setup
    connectors, _ = connector_home
    monkeypatch.setenv("EMERGE_HUB_HOME", str(hub_home))

    save_hub_config({
        "remote": str(bare_remote),
        "branch": "emerge-hub",
        "selected_verticals": ["gmail"],
        "author": "test <test@test.com>",
    })

    # Enqueue both a stable and a pull_requested event for gmail
    append_sync_event({"event": "stable", "connector": "gmail", "pipeline": "fetch", "ts_ms": 1})
    append_sync_event({"event": "pull_requested", "connector": "gmail", "ts_ms": 1})

    pull_call_count = 0

    def fake_push_flow(connector, **kwargs):
        return {"ok": False, "conflict": True, "files": ["connectors/gmail/fetch.py"]}

    def fake_pull_flow(connector, **kwargs):
        nonlocal pull_call_count
        pull_call_count += 1
        return {"ok": True, "action": "up_to_date"}

    with patch("scripts.emerge_sync.push_flow", fake_push_flow), \
         patch("scripts.emerge_sync.pull_flow", fake_pull_flow), \
         patch("scripts.emerge_sync._apply_pending_resolutions", return_value=False):
        _run_stable_events()

    assert pull_call_count == 0, (
        "pull_flow must not be called when push_flow had a conflict for the same connector"
    )
