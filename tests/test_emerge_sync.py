import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.sync.asset_ops import export_vertical, import_vertical
from scripts.sync.asset_ops import (
    file_to_intent_sig as _file_to_intent_sig,
    load_candidate_timestamps as _load_candidate_timestamps,
    load_spans_timestamps as _load_spans_timestamps,
)
from scripts.hub_config import save_hub_config
from scripts.policy_config import STABLE_MIN_ATTEMPTS


def _intents_path(state_root: Path) -> Path:
    path = state_root / "registry" / "intents.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _stable_span_entry(intent_key: str, last_ts_ms: int = 1000) -> dict:
    """Build a span-candidates entry that meets stable promotion thresholds."""
    return {
        "intent_signature": intent_key,
        "attempts": STABLE_MIN_ATTEMPTS,
        "successes": STABLE_MIN_ATTEMPTS,
        "human_fixes": 0,
        "consecutive_failures": 0,
        "recent_outcomes": [1] * STABLE_MIN_ATTEMPTS,
        "last_ts_ms": last_ts_ms,
    }


def _explore_span_entry(intent_key: str, last_ts_ms: int = 999) -> dict:
    """Build a span-candidates entry that stays in explore."""
    return {
        "intent_signature": intent_key,
        "attempts": 2,
        "successes": 2,
        "human_fixes": 0,
        "consecutive_failures": 0,
        "recent_outcomes": [1, 1],
        "last_ts_ms": last_ts_ms,
    }


def test_file_to_intent_sig_read():
    assert _file_to_intent_sig("cloud-server", Path("read/get_instances.py")) == "cloud-server.read.get_instances"


def test_file_to_intent_sig_write():
    assert _file_to_intent_sig("cloud-server", Path("write/create_vm.py")) == "cloud-server.write.create_vm"


def test_file_to_intent_sig_unknown_depth_returns_empty():
    assert _file_to_intent_sig("cloud-server", Path("get_instances.py")) == ""


def test_load_candidate_timestamps_returns_stable_only(tmp_path, monkeypatch):
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path))
    candidates = {
        "intents": {
            "cs.read.a": _stable_span_entry("cs.read.a", last_ts_ms=500),
            "cs.read.b": _explore_span_entry("cs.read.b", last_ts_ms=999),
        }
    }
    _intents_path(tmp_path).write_text(json.dumps(candidates), encoding="utf-8")
    ts = _load_candidate_timestamps("cs")
    assert ts == {"cs.read.a": 500}


def test_load_candidate_timestamps_missing_file(tmp_path, monkeypatch):
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path))
    assert _load_candidate_timestamps("cs") == {}


def test_load_spans_timestamps_parses_spans_json(tmp_path):
    spans = {"spans": {"cs.read.a": {"last_ts_ms": 1234}, "cs.read.b": {"last_ts_ms": 5678}}}
    (tmp_path / "spans.json").write_text(json.dumps(spans), encoding="utf-8")
    ts = _load_spans_timestamps(tmp_path)
    assert ts == {"cs.read.a": 1234, "cs.read.b": 5678}


def test_load_spans_timestamps_missing_file(tmp_path):
    assert _load_spans_timestamps(tmp_path) == {}


@pytest.fixture()
def connector_home(tmp_path, monkeypatch):
    """Fake ~/.emerge/connectors, hub worktree, and state root for tests."""
    connectors = tmp_path / "connectors"
    worktree = tmp_path / "hub-worktree"
    state_root = tmp_path / "state"
    worktree.mkdir()
    state_root.mkdir()
    (state_root / "registry").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(connectors))
    monkeypatch.setenv("EMERGE_HUB_HOME", str(tmp_path))
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(state_root))
    return connectors, worktree, state_root


def _make_connector(connectors: Path, name: str, state_root: Path) -> None:
    base = connectors / name
    (base / "pipelines" / "read").mkdir(parents=True)
    (base / "pipelines" / "read" / "fetch.py").write_text("# fetch", encoding="utf-8")
    (base / "pipelines" / "read" / "fetch.yaml").write_text(f"connector: {name}", encoding="utf-8")
    (base / "NOTES.md").write_text("# Notes", encoding="utf-8")
    intent_key = f"{name}.read.fetch"
    candidates = {
        "intents": {
            intent_key: _stable_span_entry(intent_key, last_ts_ms=1000),
        }
    }
    _intents_path(state_root).write_text(json.dumps(candidates), encoding="utf-8")


def test_export_copies_pipelines_and_notes(connector_home):
    connectors, worktree, state_root = connector_home
    _make_connector(connectors, "gmail", state_root)
    export_vertical("gmail", connectors_root_path=connectors, hub_worktree=worktree)
    assert (worktree / "connectors" / "gmail" / "pipelines" / "read" / "fetch.py").exists()
    assert (worktree / "connectors" / "gmail" / "NOTES.md").exists()


def test_export_generates_spans_json_from_stable_candidates(connector_home):
    connectors, worktree, state_root = connector_home
    _make_connector(connectors, "gmail", state_root)
    export_vertical("gmail", connectors_root_path=connectors, hub_worktree=worktree)
    spans_path = worktree / "connectors" / "gmail" / "spans.json"
    assert spans_path.exists()
    spans = json.loads(spans_path.read_text())
    assert "gmail.read.fetch" in spans["spans"]


def test_import_overwrites_local_pipelines(connector_home):
    connectors, worktree, state_root = connector_home
    hub_dir = worktree / "connectors" / "gmail" / "pipelines" / "read"
    hub_dir.mkdir(parents=True)
    (hub_dir / "fetch.py").write_text("# remote version", encoding="utf-8")
    (hub_dir / "fetch.yaml").write_text("connector: gmail", encoding="utf-8")
    (worktree / "connectors" / "gmail" / "NOTES.md").write_text("# Remote Notes", encoding="utf-8")
    import_vertical("gmail", connectors_root_path=connectors, hub_worktree=worktree)
    local_py = connectors / "gmail" / "pipelines" / "read" / "fetch.py"
    assert local_py.read_text(encoding="utf-8") == "# remote version"


def test_import_merges_spans_json_newer_wins(connector_home):
    connectors, worktree, state_root = connector_home
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
    import_vertical("gmail", connectors_root_path=connectors, hub_worktree=worktree)
    merged = json.loads((local_dir / "spans.json").read_text())
    assert merged["spans"]["gmail.read.fetch"]["last_ts_ms"] == 999
    assert "gmail.read.send" in merged["spans"]


def test_export_spans_json_merges_remote_spans(connector_home):
    """Exporting B's spans must not erase A's spans already in the worktree."""
    connectors, worktree, state_root = connector_home

    # A's spans already live in the worktree
    hub_conn_dir = worktree / "connectors" / "cloud-server"
    hub_conn_dir.mkdir(parents=True)
    existing_spans = {
        "spans": {
            "cloud-server.read.list_vms": {
                "intent_signature": "cloud-server.read.list_vms",
                "stage": "stable",
                "last_ts_ms": 1000,
            }
        }
    }
    (hub_conn_dir / "spans.json").write_text(json.dumps(existing_spans), encoding="utf-8")

    base = connectors / "cloud-server"
    (base / "pipelines" / "read").mkdir(parents=True)
    (base / "pipelines" / "read" / "get_quota.py").write_text("# quota", encoding="utf-8")
    candidates = {
        "intents": {
            "cloud-server.read.get_quota": _stable_span_entry("cloud-server.read.get_quota", last_ts_ms=2000),
        }
    }
    _intents_path(state_root).write_text(json.dumps(candidates), encoding="utf-8")

    export_vertical("cloud-server", connectors_root_path=connectors, hub_worktree=worktree)

    spans = json.loads((hub_conn_dir / "spans.json").read_text())["spans"]
    assert "cloud-server.read.list_vms" in spans, "A's span must be preserved"
    assert "cloud-server.read.get_quota" in spans, "B's span must be added"


def test_export_vertical_preserves_remote_only_pipeline(connector_home):
    """A's pipeline already in worktree must survive B exporting a different pipeline."""
    connectors, worktree, state_root = connector_home

    # A's pipeline already in worktree (with spans.json to provide remote timestamp)
    hub_conn = worktree / "connectors" / "cloud-server"
    (hub_conn / "pipelines" / "read").mkdir(parents=True)
    (hub_conn / "pipelines" / "read" / "list_vms.py").write_text("# A's list_vms", encoding="utf-8")
    a_spans = {
        "spans": {
            "cloud-server.read.list_vms": {"intent_signature": "cloud-server.read.list_vms", "stage": "stable", "last_ts_ms": 1000}
        }
    }
    (hub_conn / "spans.json").write_text(json.dumps(a_spans), encoding="utf-8")

    # B has a different pipeline locally
    base = connectors / "cloud-server"
    (base / "pipelines" / "read").mkdir(parents=True)
    (base / "pipelines" / "read" / "get_quota.py").write_text("# B's get_quota", encoding="utf-8")
    b_candidates = {
        "intents": {
            "cloud-server.read.get_quota": _stable_span_entry("cloud-server.read.get_quota", last_ts_ms=2000),
        }
    }
    _intents_path(state_root).write_text(json.dumps(b_candidates), encoding="utf-8")

    export_vertical("cloud-server", connectors_root_path=connectors, hub_worktree=worktree)

    # A's pipeline must survive
    assert (hub_conn / "pipelines" / "read" / "list_vms.py").read_text() == "# A's list_vms"
    # B's pipeline must be added
    assert (hub_conn / "pipelines" / "read" / "get_quota.py").exists()


def test_export_vertical_local_wins_when_newer(connector_home):
    """When local last_ts_ms > remote last_ts_ms for the same pipeline, local version overwrites."""
    connectors, worktree, state_root = connector_home

    # Remote (worktree) has older version
    hub_conn = worktree / "connectors" / "cloud-server"
    (hub_conn / "pipelines" / "read").mkdir(parents=True)
    (hub_conn / "pipelines" / "read" / "fetch.py").write_text("# old remote", encoding="utf-8")
    old_spans = {
        "spans": {
            "cloud-server.read.fetch": {"stage": "stable", "last_ts_ms": 100}
        }
    }
    (hub_conn / "spans.json").write_text(json.dumps(old_spans), encoding="utf-8")

    base = connectors / "cloud-server"
    (base / "pipelines" / "read").mkdir(parents=True)
    (base / "pipelines" / "read" / "fetch.py").write_text("# new local", encoding="utf-8")
    candidates = {
        "intents": {
            "cloud-server.read.fetch": _stable_span_entry("cloud-server.read.fetch", last_ts_ms=999),
        }
    }
    _intents_path(state_root).write_text(json.dumps(candidates), encoding="utf-8")

    export_vertical("cloud-server", connectors_root_path=connectors, hub_worktree=worktree)

    assert (hub_conn / "pipelines" / "read" / "fetch.py").read_text() == "# new local"


def test_export_vertical_remote_wins_when_newer(connector_home):
    """When remote last_ts_ms > local last_ts_ms, local must NOT overwrite the remote pipeline."""
    connectors, worktree, state_root = connector_home

    # Remote has a newer version
    hub_conn = worktree / "connectors" / "cloud-server"
    (hub_conn / "pipelines" / "read").mkdir(parents=True)
    (hub_conn / "pipelines" / "read" / "fetch.py").write_text("# newer remote", encoding="utf-8")
    new_spans = {
        "spans": {
            "cloud-server.read.fetch": {"stage": "stable", "last_ts_ms": 9999}
        }
    }
    (hub_conn / "spans.json").write_text(json.dumps(new_spans), encoding="utf-8")

    base = connectors / "cloud-server"
    (base / "pipelines" / "read").mkdir(parents=True)
    (base / "pipelines" / "read" / "fetch.py").write_text("# stale local", encoding="utf-8")
    candidates = {
        "intents": {
            "cloud-server.read.fetch": _stable_span_entry("cloud-server.read.fetch", last_ts_ms=50),
        }
    }
    _intents_path(state_root).write_text(json.dumps(candidates), encoding="utf-8")

    export_vertical("cloud-server", connectors_root_path=connectors, hub_worktree=worktree)

    # Remote version must be untouched
    assert (hub_conn / "pipelines" / "read" / "fetch.py").read_text() == "# newer remote"


def test_export_vertical_skips_explore_state_pipeline(connector_home):
    """Pipelines without a stable candidate must NOT be exported to the hub."""
    connectors, worktree, state_root = connector_home

    base = connectors / "cloud-server"
    (base / "pipelines" / "read").mkdir(parents=True)
    (base / "pipelines" / "read" / "draft.py").write_text("# explore draft", encoding="utf-8")
    candidates = {
        "intents": {
            "cloud-server.read.draft": _explore_span_entry("cloud-server.read.draft", last_ts_ms=500),
        }
    }
    _intents_path(state_root).write_text(json.dumps(candidates), encoding="utf-8")

    export_vertical("cloud-server", connectors_root_path=connectors, hub_worktree=worktree)

    hub_conn = worktree / "connectors" / "cloud-server"
    assert not (hub_conn / "pipelines" / "read" / "draft.py").exists(), \
        "explore-state pipelines must not be exported"


def test_export_vertical_copies_yaml_companion(connector_home):
    """When a .py is exported, the sibling .yaml must follow."""
    connectors, worktree, state_root = connector_home

    base = connectors / "cloud-server"
    (base / "pipelines" / "read").mkdir(parents=True)
    (base / "pipelines" / "read" / "fetch.py").write_text("# fetch", encoding="utf-8")
    (base / "pipelines" / "read" / "fetch.yaml").write_text("connector: cs", encoding="utf-8")
    candidates = {
        "intents": {
            "cloud-server.read.fetch": _stable_span_entry("cloud-server.read.fetch", last_ts_ms=100),
        }
    }
    _intents_path(state_root).write_text(json.dumps(candidates), encoding="utf-8")

    export_vertical("cloud-server", connectors_root_path=connectors, hub_worktree=worktree)

    hub_conn = worktree / "connectors" / "cloud-server"
    assert (hub_conn / "pipelines" / "read" / "fetch.yaml").read_text() == "connector: cs"


def test_export_vertical_removes_stale_yaml_when_local_has_none(connector_home):
    """If local .py overwrites remote but local has no .yaml, the old remote .yaml is cleaned up."""
    connectors, worktree, state_root = connector_home

    # Remote worktree has an old .yaml
    hub_conn = worktree / "connectors" / "cloud-server"
    (hub_conn / "pipelines" / "read").mkdir(parents=True)
    (hub_conn / "pipelines" / "read" / "fetch.py").write_text("# old", encoding="utf-8")
    (hub_conn / "pipelines" / "read" / "fetch.yaml").write_text("old: yaml", encoding="utf-8")
    old_spans = {
        "spans": {"cloud-server.read.fetch": {"stage": "stable", "last_ts_ms": 10}}
    }
    (hub_conn / "spans.json").write_text(json.dumps(old_spans), encoding="utf-8")

    # Local has newer .py but NO .yaml
    base = connectors / "cloud-server"
    (base / "pipelines" / "read").mkdir(parents=True)
    (base / "pipelines" / "read" / "fetch.py").write_text("# new local", encoding="utf-8")
    candidates = {
        "intents": {
            "cloud-server.read.fetch": _stable_span_entry("cloud-server.read.fetch", last_ts_ms=999),
        }
    }
    _intents_path(state_root).write_text(json.dumps(candidates), encoding="utf-8")

    export_vertical("cloud-server", connectors_root_path=connectors, hub_worktree=worktree)

    assert (hub_conn / "pipelines" / "read" / "fetch.py").read_text() == "# new local"
    assert not (hub_conn / "pipelines" / "read" / "fetch.yaml").exists(), \
        "stale remote .yaml must be removed when local has no .yaml"


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
    from scripts.sync.git_ops import git_has_remote_changes
    bare_remote, worktree, hub_home = git_setup
    _init_hub_worktree(worktree, str(bare_remote))
    assert git_has_remote_changes(worktree, "emerge-hub") is False


def test_git_push_commits_and_updates_remote(git_setup, connector_home):
    from scripts.sync.git_ops import git_push
    bare_remote, worktree, hub_home = git_setup
    connectors, _, state_root = connector_home
    _make_connector(connectors, "gmail", state_root)
    _init_hub_worktree(worktree, str(bare_remote))

    hub_dir = worktree / "connectors" / "gmail"
    hub_dir.mkdir(parents=True)
    (hub_dir / "NOTES.md").write_text("# hub notes", encoding="utf-8")

    result = git_push(worktree, "emerge-hub", connector="gmail", author="test <test@test.com>")
    assert result["ok"] is True
    assert result.get("pushed") is True


# ── Push/pull flow tests ────────────────────────────────────────────────────

def test_push_flow_exports_and_pushes(git_setup, connector_home):
    from scripts.sync.sync_flow import push_flow
    bare_remote, worktree, hub_home = git_setup
    connectors, _, state_root = connector_home
    _make_connector(connectors, "gmail", state_root)
    _init_hub_worktree(worktree, str(bare_remote))

    result = push_flow("gmail", connectors_root_path=connectors, hub_worktree=worktree)
    assert result["ok"] is True


def test_pull_flow_imports_remote_changes(git_setup, connector_home):
    from scripts.sync.sync_flow import pull_flow, push_flow
    bare_remote, worktree_a, hub_home = git_setup
    connectors_a, _, state_root = connector_home

    connectors_b = hub_home / "connectors_b"
    worktree_b = hub_home / "worktree_b"
    _init_hub_worktree(worktree_a, str(bare_remote))
    _init_hub_worktree(worktree_b, str(bare_remote))

    _make_connector(connectors_a, "gmail", state_root)
    push_flow("gmail", connectors_root_path=connectors_a, hub_worktree=worktree_a)

    # Machine B pulls the new content
    result = pull_flow("gmail", connectors_root_path=connectors_b, hub_worktree=worktree_b)
    assert result["ok"] is True
    assert (connectors_b / "gmail" / "pipelines" / "read" / "fetch.py").exists()


def test_apply_pending_resolutions_theirs_writes_remote_version(git_setup, connector_home):
    """'theirs' resolution must actually overwrite the local file with the remote version."""
    from scripts.sync.git_ops import apply_pending_resolutions as _apply_pending_resolutions
    from scripts.sync.sync_flow import push_flow
    from scripts.hub_config import load_pending_conflicts, save_pending_conflicts, new_conflict_id

    bare_remote, worktree, hub_home = git_setup
    connectors, _, state_root = connector_home

    _init_hub_worktree(worktree, str(bare_remote))
    _make_connector(connectors, "gmail", state_root)
    push_flow("gmail", connectors_root_path=connectors, hub_worktree=worktree)

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
    from scripts.sync.git_ops import apply_pending_resolutions as _apply_pending_resolutions
    from scripts.sync.sync_flow import push_flow
    from scripts.hub_config import load_pending_conflicts, save_pending_conflicts, new_conflict_id

    bare_remote, worktree, hub_home = git_setup
    connectors, _, state_root = connector_home

    _init_hub_worktree(worktree, str(bare_remote))
    _make_connector(connectors, "gmail", state_root)
    push_flow("gmail", connectors_root_path=connectors, hub_worktree=worktree)

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
    from scripts.sync.sync_flow import _run_stable_events
    from scripts.hub_config import append_sync_event, load_pending_conflicts, save_hub_config

    bare_remote, worktree, hub_home = git_setup
    connectors, _, state_root = connector_home
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

    with patch("scripts.sync.sync_flow.push_flow", fake_push_flow), \
         patch("scripts.sync.sync_flow.pull_flow", fake_pull_flow), \
         patch("scripts.sync.sync_flow.apply_pending_resolutions", return_value=False):
        _run_stable_events()

    assert pull_call_count == 0, (
        "pull_flow must not be called when push_flow had a conflict for the same connector"
    )


def test_run_event_loop_fires_stable_events_on_queue_write(tmp_path, monkeypatch):
    """Writing to sync-queue.jsonl must trigger _run_stable_events immediately."""
    import threading
    import scripts.sync.sync_flow as _sync_flow

    fired = threading.Event()
    monkeypatch.setattr(_sync_flow, "_run_stable_events", lambda: fired.set())
    monkeypatch.setattr(_sync_flow, "_run_pull_cycle", lambda: None)

    queue = tmp_path / "sync-queue.jsonl"
    monkeypatch.setattr(_sync_flow, "sync_queue_path", lambda: queue)
    monkeypatch.setattr(_sync_flow, "load_hub_config",
                        lambda: {"poll_interval_seconds": 999})

    stop = threading.Event()
    t = threading.Thread(target=_sync_flow.run_event_loop, args=(stop,), daemon=True)
    t.start()

    queue.write_text('{"type":"stable"}\n')
    assert fired.wait(timeout=3.0), "stable event handler never fired"
    stop.set()
    t.join(timeout=2)


def test_run_event_loop_pull_cycle_fires_on_timer(tmp_path, monkeypatch):
    """_run_pull_cycle must fire after poll_interval_seconds."""
    import threading
    import scripts.sync.sync_flow as _sync_flow

    pulled = threading.Event()
    monkeypatch.setattr(_sync_flow, "_run_stable_events", lambda: None)
    monkeypatch.setattr(_sync_flow, "_run_pull_cycle", lambda: pulled.set())
    monkeypatch.setattr(_sync_flow, "sync_queue_path", lambda: tmp_path / "q.jsonl")
    monkeypatch.setattr(_sync_flow, "load_hub_config",
                        lambda: {"poll_interval_seconds": 1})  # 1s for test speed

    stop = threading.Event()
    t = threading.Thread(target=_sync_flow.run_event_loop, args=(stop,), daemon=True)
    t.start()

    assert pulled.wait(timeout=4.0), "pull cycle never fired"
    stop.set()
    t.join(timeout=2)


# ---------------------------------------------------------------------------
# Cross-machine learn-forever signals (synthesis_skipped, bridge_broken)
# ---------------------------------------------------------------------------


def _canary_entry_with_skipped(intent_key: str, reason: str, last_ts_ms: int = 2000) -> dict:
    """A canary intent that crystallizer refused — the signal we want cross-machine."""
    return {
        "intent_signature": intent_key,
        "stage": "canary",
        "attempts": 3,
        "successes": 3,
        "synthesis_skipped_reason": reason,
        "last_ts_ms": last_ts_ms,
    }


def _stable_entry_with_bridge_demotion(intent_key: str, last_ts_ms: int = 3000) -> dict:
    """A stable intent that auto-demoted due to bridge_broken — other machines should be warned."""
    return {
        "intent_signature": intent_key,
        "stage": "canary",  # post-demotion
        "attempts": 8,
        "successes": 6,
        "last_ts_ms": last_ts_ms,
        "last_demotion": {
            "reason": "bridge_broken",
            "to_stage": "canary",
            "from_stage": "stable",
        },
    }


def test_export_includes_synthesis_skipped_from_canary(connector_home):
    """synthesis_skipped_reason on a canary intent must cross into hub spans.json."""
    connectors, worktree, state_root = connector_home
    (connectors / "gmail").mkdir(parents=True)
    candidates = {
        "intents": {
            "gmail.read.parse": _canary_entry_with_skipped(
                "gmail.read.parse", "wal_missing_result_assignment", last_ts_ms=2000
            ),
        }
    }
    _intents_path(state_root).write_text(json.dumps(candidates), encoding="utf-8")

    export_vertical("gmail", connectors_root_path=connectors, hub_worktree=worktree)

    spans = json.loads(
        (worktree / "connectors" / "gmail" / "spans.json").read_text(encoding="utf-8")
    )["spans"]
    assert "gmail.read.parse" in spans
    assert spans["gmail.read.parse"]["synthesis_skipped_reason"] == "wal_missing_result_assignment"
    assert spans["gmail.read.parse"]["stage"] == "canary"


def test_export_includes_bridge_broken_demotion(connector_home):
    """last_demotion.reason == 'bridge_broken' must cross-machine as a warning signal."""
    connectors, worktree, state_root = connector_home
    (connectors / "cad").mkdir(parents=True)
    candidates = {
        "intents": {
            "cad.write.insert-block": _stable_entry_with_bridge_demotion(
                "cad.write.insert-block", last_ts_ms=3000
            ),
        }
    }
    _intents_path(state_root).write_text(json.dumps(candidates), encoding="utf-8")

    export_vertical("cad", connectors_root_path=connectors, hub_worktree=worktree)

    spans = json.loads(
        (worktree / "connectors" / "cad" / "spans.json").read_text(encoding="utf-8")
    )["spans"]
    assert "cad.write.insert-block" in spans
    demo = spans["cad.write.insert-block"]["last_demotion"]
    assert demo["reason"] == "bridge_broken"
    assert demo["to_stage"] == "canary"


def test_export_excludes_unremarkable_explore(connector_home):
    """Explore entries with no skipped-reason and no bridge demotion must NOT leak into hub."""
    connectors, worktree, state_root = connector_home
    (connectors / "gmail").mkdir(parents=True)
    candidates = {
        "intents": {
            "gmail.read.noise": _explore_span_entry("gmail.read.noise", last_ts_ms=500),
        }
    }
    _intents_path(state_root).write_text(json.dumps(candidates), encoding="utf-8")

    export_vertical("gmail", connectors_root_path=connectors, hub_worktree=worktree)

    spans_path = worktree / "connectors" / "gmail" / "spans.json"
    if spans_path.exists():
        spans = json.loads(spans_path.read_text(encoding="utf-8"))["spans"]
        assert "gmail.read.noise" not in spans


def test_import_propagates_synthesis_skipped_to_local_registry(connector_home):
    """After import, a remote skipped_reason must land on the local IntentRegistry entry
    so the next session's reflection surfaces it — spans.json alone is invisible to reflection."""
    connectors, worktree, state_root = connector_home

    # Local already has the intent (older, no skipped reason)
    candidates = {
        "intents": {
            "gmail.read.parse": {
                "intent_signature": "gmail.read.parse",
                "stage": "canary",
                "attempts": 3,
                "successes": 3,
                "last_ts_ms": 1000,
            },
        }
    }
    _intents_path(state_root).write_text(json.dumps(candidates), encoding="utf-8")

    # Remote (hub) has newer entry with a skipped_reason
    hub_dir = worktree / "connectors" / "gmail"
    hub_dir.mkdir(parents=True)
    remote_spans = {
        "spans": {
            "gmail.read.parse": {
                "intent_signature": "gmail.read.parse",
                "stage": "canary",
                "last_ts_ms": 2000,
                "synthesis_skipped_reason": "wal_missing_result_assignment",
            }
        }
    }
    (hub_dir / "spans.json").write_text(json.dumps(remote_spans), encoding="utf-8")

    import_vertical("gmail", connectors_root_path=connectors, hub_worktree=worktree)

    local = json.loads(_intents_path(state_root).read_text(encoding="utf-8"))
    entry = local["intents"]["gmail.read.parse"]
    assert entry["synthesis_skipped_reason"] == "wal_missing_result_assignment"
    # Invariant: stage must NOT be overwritten from hub (single-writer lives in PolicyEngine)
    assert entry["stage"] == "canary"
    # Counters also untouched by hub import
    assert entry["attempts"] == 3


def test_import_does_not_create_intent_from_hub(connector_home):
    """Hub must never materialize a NEW local intent — only propagates diagnostics to existing ones.
    Rationale: a local machine that has never attempted the intent gets no value from the warning
    until it does attempt, and creating phantom entries pollutes IntentRegistry."""
    connectors, worktree, state_root = connector_home
    _intents_path(state_root).write_text(json.dumps({"intents": {}}), encoding="utf-8")

    hub_dir = worktree / "connectors" / "gmail"
    hub_dir.mkdir(parents=True)
    remote_spans = {
        "spans": {
            "gmail.read.unknown": {
                "intent_signature": "gmail.read.unknown",
                "stage": "stable",
                "last_ts_ms": 5000,
                "synthesis_skipped_reason": "wal_missing_result_assignment",
            }
        }
    }
    (hub_dir / "spans.json").write_text(json.dumps(remote_spans), encoding="utf-8")

    import_vertical("gmail", connectors_root_path=connectors, hub_worktree=worktree)

    local = json.loads(_intents_path(state_root).read_text(encoding="utf-8"))
    assert "gmail.read.unknown" not in local["intents"]


def test_import_skips_stale_remote(connector_home):
    """If local last_ts_ms >= remote, the remote diagnostic must NOT overwrite — local is fresher."""
    connectors, worktree, state_root = connector_home

    candidates = {
        "intents": {
            "gmail.read.parse": {
                "intent_signature": "gmail.read.parse",
                "stage": "canary",
                "attempts": 3,
                "successes": 3,
                "last_ts_ms": 5000,  # local is newer
                "synthesis_skipped_reason": "local_reason",
            },
        }
    }
    _intents_path(state_root).write_text(json.dumps(candidates), encoding="utf-8")

    hub_dir = worktree / "connectors" / "gmail"
    hub_dir.mkdir(parents=True)
    remote_spans = {
        "spans": {
            "gmail.read.parse": {
                "intent_signature": "gmail.read.parse",
                "stage": "canary",
                "last_ts_ms": 1000,
                "synthesis_skipped_reason": "remote_reason",
            }
        }
    }
    (hub_dir / "spans.json").write_text(json.dumps(remote_spans), encoding="utf-8")

    import_vertical("gmail", connectors_root_path=connectors, hub_worktree=worktree)

    local = json.loads(_intents_path(state_root).read_text(encoding="utf-8"))
    assert local["intents"]["gmail.read.parse"]["synthesis_skipped_reason"] == "local_reason"


def test_import_propagates_bridge_broken_demotion(connector_home):
    """Remote bridge_broken demotion must land on local entry with imported_from_hub marker."""
    connectors, worktree, state_root = connector_home

    candidates = {
        "intents": {
            "cad.write.insert-block": {
                "intent_signature": "cad.write.insert-block",
                "stage": "stable",
                "attempts": 10,
                "successes": 10,
                "last_ts_ms": 500,
            },
        }
    }
    _intents_path(state_root).write_text(json.dumps(candidates), encoding="utf-8")

    hub_dir = worktree / "connectors" / "cad"
    hub_dir.mkdir(parents=True)
    remote_spans = {
        "spans": {
            "cad.write.insert-block": {
                "intent_signature": "cad.write.insert-block",
                "stage": "canary",
                "last_ts_ms": 2000,
                "last_demotion": {"reason": "bridge_broken", "to_stage": "canary"},
            }
        }
    }
    (hub_dir / "spans.json").write_text(json.dumps(remote_spans), encoding="utf-8")

    import_vertical("cad", connectors_root_path=connectors, hub_worktree=worktree)

    local = json.loads(_intents_path(state_root).read_text(encoding="utf-8"))
    entry = local["intents"]["cad.write.insert-block"]
    assert entry["last_demotion"]["reason"] == "bridge_broken"
    assert entry["last_demotion"]["imported_from_hub"] is True
    # Local stage must NOT be rewritten by hub import
    assert entry["stage"] == "stable"
