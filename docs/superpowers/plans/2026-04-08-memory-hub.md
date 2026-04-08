# Memory Hub Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a bidirectional connector-asset sharing system backed by a self-hosted git repo's orphan branch, with event-driven push on stable promotion, background pull polling, and AI-assisted conflict resolution.

**Architecture:** `emerge_sync.py` is a standalone sync agent that polls `~/.emerge/sync-queue.jsonl` for stable events (written by the daemon) and runs a periodic pull timer. The daemon gains an `icc_hub` MCP tool for configuration management and conflict resolution. All connector assets (pipelines, NOTES.md, spans.json) are pushed/pulled to a single orphan branch (`emerge-hub`) in the team's self-hosted git repo.

**Tech Stack:** Python stdlib (`subprocess`, `shutil`, `threading`, `tempfile`), git CLI (via `subprocess`), `~/.emerge/hub-config.json` for config, `~/.emerge/sync-queue.jsonl` for IPC.

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `scripts/hub_config.py` | Create | Config load/save, sync-queue append/consume, pending-conflicts read/write |
| `scripts/emerge_sync.py` | Create | Standalone sync agent: export, import, git ops, poll loop, setup wizard |
| `scripts/emerge_daemon.py` | Modify | Add `icc_hub` tool (list/add/remove/sync/status/resolve/setup), write stable events to sync-queue |
| `tests/test_hub_config.py` | Create | Unit tests for hub_config helpers |
| `tests/test_emerge_sync.py` | Create | Integration tests for export/import logic |
| `README.md` | Modify | Add Memory Hub to component table |
| `CLAUDE.md` | Modify | Add hub architecture notes |

---

### Task 1: hub_config.py — config and queue helpers

**Files:**
- Create: `scripts/hub_config.py`
- Test: `tests/test_hub_config.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_hub_config.py
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.hub_config import (
    load_hub_config,
    save_hub_config,
    append_sync_event,
    consume_sync_events,
    load_pending_conflicts,
    save_pending_conflicts,
    is_configured,
    hub_config_path,
    sync_queue_path,
    pending_conflicts_path,
    hub_worktree_path,
)


@pytest.fixture()
def hub_home(tmp_path, monkeypatch):
    """Redirect all hub paths to tmp_path."""
    monkeypatch.setenv("EMERGE_HUB_HOME", str(tmp_path))
    return tmp_path


def test_load_hub_config_returns_empty_when_missing(hub_home):
    cfg = load_hub_config()
    assert cfg == {}


def test_save_and_load_hub_config_roundtrip(hub_home):
    cfg = {
        "remote": "git@quasar:team/hub.git",
        "branch": "emerge-hub",
        "poll_interval_seconds": 300,
        "selected_verticals": ["gmail", "linear"],
        "author": "alice <alice@team.com>",
    }
    save_hub_config(cfg)
    loaded = load_hub_config()
    assert loaded == cfg


def test_is_configured_false_when_missing(hub_home):
    assert is_configured() is False


def test_is_configured_true_when_remote_and_verticals_set(hub_home):
    save_hub_config({"remote": "git@x:y.git", "selected_verticals": ["gmail"]})
    assert is_configured() is True


def test_append_and_consume_sync_events(hub_home):
    append_sync_event({"event": "stable", "connector": "gmail", "pipeline": "fetch"})
    append_sync_event({"event": "reload", "connector": "gmail"})
    all_events = consume_sync_events(lambda e: e.get("event") == "stable")
    assert len(all_events) == 1
    assert all_events[0]["connector"] == "gmail"
    # Remaining event is still in queue
    remaining = consume_sync_events(lambda e: True)
    assert len(remaining) == 1
    assert remaining[0]["event"] == "reload"


def test_pending_conflicts_roundtrip(hub_home):
    data = {
        "conflicts": [
            {
                "conflict_id": "abc123",
                "connector": "gmail",
                "file": "pipelines/read/fetch.py",
                "ours_ts_ms": 1000,
                "theirs_ts_ms": 900,
                "status": "pending",
                "resolution": None,
            }
        ]
    }
    save_pending_conflicts(data)
    loaded = load_pending_conflicts()
    assert loaded["conflicts"][0]["conflict_id"] == "abc123"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_hub_config.py -q
```
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.hub_config'`

- [ ] **Step 3: Implement hub_config.py**

```python
# scripts/hub_config.py
from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable


def _hub_home() -> Path:
    """Base directory for all hub state files. Override with EMERGE_HUB_HOME for tests."""
    override = os.environ.get("EMERGE_HUB_HOME")
    if override:
        return Path(override)
    return Path.home() / ".emerge"


def hub_config_path() -> Path:
    return _hub_home() / "hub-config.json"


def hub_worktree_path() -> Path:
    return _hub_home() / "hub-worktree"


def sync_queue_path() -> Path:
    return _hub_home() / "sync-queue.jsonl"


def pending_conflicts_path() -> Path:
    return _hub_home() / "pending-conflicts.json"


# ── Config ─────────────────────────────────────────────────────────────────

def load_hub_config() -> dict[str, Any]:
    """Load hub-config.json. Returns {} if missing or corrupt."""
    p = hub_config_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_hub_config(config: dict[str, Any]) -> None:
    """Atomically write hub-config.json."""
    p = hub_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(p, config)


def is_configured() -> bool:
    cfg = load_hub_config()
    return bool(cfg.get("remote") and cfg.get("selected_verticals"))


# ── Sync queue ─────────────────────────────────────────────────────────────

def append_sync_event(event: dict[str, Any]) -> None:
    """Append a JSON event line to sync-queue.jsonl."""
    p = sync_queue_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False) + "\n"
    with open(p, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def consume_sync_events(predicate: Callable[[dict[str, Any]], bool]) -> list[dict[str, Any]]:
    """Atomically remove and return events matching predicate from sync-queue.jsonl."""
    p = sync_queue_path()
    if not p.exists():
        return []
    consumed: list[dict[str, Any]] = []
    kept: list[str] = []
    for raw in p.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except Exception:
            kept.append(raw)
            continue
        if predicate(event):
            consumed.append(event)
        else:
            kept.append(raw)
    # Rewrite queue atomically
    fd, tmp = tempfile.mkstemp(prefix=".sync-queue-", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for line in kept:
                f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return consumed


# ── Pending conflicts ──────────────────────────────────────────────────────

def load_pending_conflicts() -> dict[str, Any]:
    """Load pending-conflicts.json. Returns {"conflicts": []} if missing."""
    p = pending_conflicts_path()
    if not p.exists():
        return {"conflicts": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data.get("conflicts"), list):
            return {"conflicts": []}
        return data
    except Exception:
        return {"conflicts": []}


def save_pending_conflicts(data: dict[str, Any]) -> None:
    """Atomically write pending-conflicts.json."""
    p = pending_conflicts_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(p, data)


def new_conflict_id() -> str:
    return uuid.uuid4().hex[:8]


# ── Internal ───────────────────────────────────────────────────────────────

def _atomic_write_json(path: Path, data: Any) -> None:
    text = json.dumps(data, indent=2, ensure_ascii=False)
    fd, tmp = tempfile.mkstemp(prefix=".hub-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_hub_config.py -q
```
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/hub_config.py tests/test_hub_config.py
git commit -m "feat: add hub_config helpers for Memory Hub"
```

---

### Task 2: emerge_sync.py — export and import logic

**Files:**
- Create: `scripts/emerge_sync.py` (export/import functions only)
- Test: `tests/test_emerge_sync.py` (export/import tests)

- [ ] **Step 1: Write failing tests**

```python
# tests/test_emerge_sync.py
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.emerge_sync import export_vertical, import_vertical


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
    # span-candidates.json for spans.json export
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
    # Set up hub worktree with "remote" content
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
    # Local has an older entry
    local_dir = connectors / "gmail"
    local_dir.mkdir(parents=True)
    local_spans = {"spans": {"gmail.read.fetch": {"intent_signature": "gmail.read.fetch", "last_ts_ms": 100}}}
    (local_dir / "spans.json").write_text(json.dumps(local_spans), encoding="utf-8")
    # Hub has a newer entry for same key plus a new key
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
    assert merged["spans"]["gmail.read.fetch"]["last_ts_ms"] == 999  # hub wins (newer)
    assert "gmail.read.send" in merged["spans"]  # new entry added
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_emerge_sync.py -q
```
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.emerge_sync'`

- [ ] **Step 3: Implement export_vertical and import_vertical in emerge_sync.py**

```python
# scripts/emerge_sync.py
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.hub_config import (
    append_sync_event,
    consume_sync_events,
    hub_worktree_path,
    load_hub_config,
    load_pending_conflicts,
    new_conflict_id,
    pending_conflicts_path,
    save_hub_config,
    save_pending_conflicts,
    sync_queue_path,
)

logger = logging.getLogger(__name__)

_PIPELINE_EXTENSIONS = (".py", ".yaml")
_PRIVATE_DIRS = {"operator-events", "credentials"}


def _connectors_root() -> Path:
    override = os.environ.get("EMERGE_CONNECTOR_ROOT")
    if override:
        return Path(override)
    return Path.home() / ".emerge" / "connectors"


# ── Export ─────────────────────────────────────────────────────────────────

def export_vertical(
    connector: str,
    *,
    connectors_root: Path | None = None,
    hub_worktree: Path | None = None,
) -> None:
    """Copy connector assets from local ~/.emerge/connectors/<connector>/ into the hub worktree."""
    src = (connectors_root or _connectors_root()) / connector
    dst = (hub_worktree or hub_worktree_path()) / "connectors" / connector

    # Copy pipelines (read + write), skipping private dirs
    src_pipelines = src / "pipelines"
    dst_pipelines = dst / "pipelines"
    if src_pipelines.exists():
        if dst_pipelines.exists():
            shutil.rmtree(dst_pipelines)
        shutil.copytree(src_pipelines, dst_pipelines)

    # Copy NOTES.md
    notes_src = src / "NOTES.md"
    if notes_src.exists():
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copy2(notes_src, dst / "NOTES.md")

    # Generate spans.json from span-candidates.json (stable entries only)
    _export_spans_json(src, dst)


def _export_spans_json(src: Path, dst: Path) -> None:
    """Generate spans.json with stable candidates; strip private fields."""
    candidates_path = src / "span-candidates.json"
    if not candidates_path.exists():
        return
    try:
        raw = json.loads(candidates_path.read_text(encoding="utf-8"))
        candidates = raw.get("candidates", {})
    except Exception:
        return
    spans: dict[str, Any] = {}
    for key, entry in candidates.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("status") != "stable":
            continue
        spans[key] = {
            "intent_signature": entry.get("intent_signature", key),
            "status": "stable",
            "last_ts_ms": entry.get("last_ts_ms", 0),
        }
    dst.mkdir(parents=True, exist_ok=True)
    (dst / "spans.json").write_text(
        json.dumps({"spans": spans}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ── Import ─────────────────────────────────────────────────────────────────

def import_vertical(
    connector: str,
    *,
    connectors_root: Path | None = None,
    hub_worktree: Path | None = None,
) -> None:
    """Copy connector assets from hub worktree into ~/.emerge/connectors/<connector>/."""
    src = (hub_worktree or hub_worktree_path()) / "connectors" / connector
    dst = (connectors_root or _connectors_root()) / connector

    if not src.exists():
        return

    dst.mkdir(parents=True, exist_ok=True)

    # Overwrite pipelines directory
    src_pipelines = src / "pipelines"
    if src_pipelines.exists():
        dst_pipelines = dst / "pipelines"
        if dst_pipelines.exists():
            shutil.rmtree(dst_pipelines)
        shutil.copytree(src_pipelines, dst_pipelines)

    # Overwrite NOTES.md
    notes_src = src / "NOTES.md"
    if notes_src.exists():
        shutil.copy2(notes_src, dst / "NOTES.md")

    # Merge spans.json (remote wins on newer last_ts_ms)
    _import_spans_json(src, dst)


def _import_spans_json(src: Path, dst: Path) -> None:
    """Merge remote spans.json into local spans.json. Remote wins on newer last_ts_ms."""
    remote_path = src / "spans.json"
    if not remote_path.exists():
        return
    try:
        remote = json.loads(remote_path.read_text(encoding="utf-8"))
        remote_spans = remote.get("spans", {})
    except Exception:
        return

    local_path = dst / "spans.json"
    try:
        local = json.loads(local_path.read_text(encoding="utf-8")) if local_path.exists() else {}
        local_spans = local.get("spans", {})
    except Exception:
        local_spans = {}

    merged = dict(local_spans)
    for key, entry in remote_spans.items():
        if not isinstance(entry, dict):
            continue
        local_entry = merged.get(key)
        if local_entry is None or entry.get("last_ts_ms", 0) > local_entry.get("last_ts_ms", 0):
            merged[key] = entry

    _write_json(local_path, {"spans": merged})


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".hub-import-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2, ensure_ascii=False))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_emerge_sync.py -q
```
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/emerge_sync.py tests/test_emerge_sync.py
git commit -m "feat: add emerge_sync export/import logic"
```

---

### Task 3: emerge_sync.py — git operations

**Files:**
- Modify: `scripts/emerge_sync.py` (add git functions)
- Test: `tests/test_emerge_sync.py` (add git tests using real local git repos)

- [ ] **Step 1: Write failing tests**

Add to `tests/test_emerge_sync.py`:

```python
import subprocess


@pytest.fixture()
def git_setup(tmp_path, monkeypatch):
    """Create a bare remote and local hub worktree with orphan emerge-hub branch."""
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
    """Bootstrap hub worktree with orphan branch and push to remote."""
    from scripts.emerge_sync import _git
    worktree.mkdir(parents=True, exist_ok=True)
    _git(["init"], cwd=worktree)
    _git(["remote", "add", "origin", remote], cwd=worktree)
    _git(["checkout", "--orphan", branch], cwd=worktree)
    _git(["commit", "--allow-empty", "-m", "chore: init emerge-hub",
          "--author", "test <test@test.com>"], cwd=worktree)
    _git(["push", "-u", "origin", branch], cwd=worktree)


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

    # Write a file into worktree to simulate export
    hub_dir = worktree / "connectors" / "gmail"
    hub_dir.mkdir(parents=True)
    (hub_dir / "NOTES.md").write_text("# hub notes", encoding="utf-8")

    result = git_push(worktree, "emerge-hub", connector="gmail",
                      author="test <test@test.com>")
    assert result["ok"] is True
    assert result.get("pushed") is True
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_emerge_sync.py::test_git_fetch_and_detect_no_changes tests/test_emerge_sync.py::test_git_push_commits_and_updates_remote -q
```
Expected: FAIL with `ImportError: cannot import name '_git' from 'scripts.emerge_sync'`

- [ ] **Step 3: Add git operation functions to emerge_sync.py**

Append to `scripts/emerge_sync.py` (after `_write_json`):

```python
# ── Git operations ─────────────────────────────────────────────────────────

def _git(args: list[str], *, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a git command in cwd. Raises CalledProcessError on failure (when check=True)."""
    return subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
    )


def git_has_remote_changes(worktree: Path, branch: str) -> bool:
    """Return True if origin/<branch> is ahead of HEAD after fetching."""
    try:
        _git(["fetch", "origin", branch], cwd=worktree)
    except subprocess.CalledProcessError:
        return False
    result = _git(
        ["rev-list", "--count", f"HEAD..origin/{branch}"],
        cwd=worktree,
        check=False,
    )
    if result.returncode != 0:
        return False
    return int(result.stdout.strip() or "0") > 0


def git_merge_remote(
    worktree: Path,
    branch: str,
    *,
    author: str,
) -> dict[str, Any]:
    """Fetch + merge origin/<branch>. Returns {"ok": True} or {"conflict": True, "files": [...]}."""
    try:
        _git(["fetch", "origin", branch], cwd=worktree)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip()
        if "Permission denied" in stderr or "Authentication failed" in stderr:
            return {"ok": False, "error": f"auth_failed: {stderr}"}
        return {"ok": False, "error": f"fetch failed: {stderr}"}

    merge = _git(
        ["merge", f"origin/{branch}", "--no-edit"],
        cwd=worktree,
        check=False,
    )
    if merge.returncode == 0:
        return {"ok": True}

    # Conflict: collect conflicting file paths
    status = _git(["diff", "--name-only", "--diff-filter=U"], cwd=worktree, check=False)
    conflict_files = [f.strip() for f in status.stdout.splitlines() if f.strip()]
    _git(["merge", "--abort"], cwd=worktree, check=False)
    return {"conflict": True, "files": conflict_files}


def git_push(
    worktree: Path,
    branch: str,
    *,
    connector: str,
    author: str,
) -> dict[str, Any]:
    """Stage all changes, commit, and push. Returns {"ok": True, "pushed": bool}."""
    status = _git(["status", "--porcelain"], cwd=worktree, check=False)
    if not status.stdout.strip():
        return {"ok": True, "pushed": False, "reason": "nothing_to_commit"}

    _git(["add", "-A"], cwd=worktree)
    ts_ms = int(time.time() * 1000)
    _git(
        ["commit", "-m", f"hub: sync {connector} pipelines [ts={ts_ms}]",
         "--author", author],
        cwd=worktree,
    )

    push_result = _git(
        ["push", "origin", branch],
        cwd=worktree,
        check=False,
    )
    if push_result.returncode == 0:
        return {"ok": True, "pushed": True}

    # Non-fast-forward: fetch + rebase and retry once
    _git(["fetch", "origin", branch], cwd=worktree, check=False)
    rebase = _git(
        ["rebase", f"origin/{branch}"],
        cwd=worktree,
        check=False,
    )
    if rebase.returncode != 0:
        _git(["rebase", "--abort"], cwd=worktree, check=False)
        return {"ok": False, "error": "push rejected and rebase failed"}
    retry = _git(["push", "origin", branch], cwd=worktree, check=False)
    if retry.returncode == 0:
        return {"ok": True, "pushed": True}
    return {"ok": False, "error": push_result.stderr.strip()}


def git_setup_worktree(worktree: Path, remote: str, branch: str, author: str) -> dict[str, Any]:
    """Clone existing remote branch or create orphan branch and push."""
    if worktree.exists() and (worktree / ".git").exists():
        return {"ok": True, "action": "already_exists"}

    worktree.mkdir(parents=True, exist_ok=True)
    _git(["init"], cwd=worktree)
    # Configure git identity in worktree so merge commits have correct author
    import re as _re
    _m = _re.match(r'^(.+?)\s*<(.+?)>', author)
    if _m:
        _git(["config", "user.name", _m.group(1).strip()], cwd=worktree)
        _git(["config", "user.email", _m.group(2).strip()], cwd=worktree)
    _git(["remote", "add", "origin", remote], cwd=worktree)

    # Try fetching existing branch first
    fetch = _git(["fetch", "origin", branch], cwd=worktree, check=False)
    if fetch.returncode == 0:
        _git(["checkout", "-b", branch, f"origin/{branch}"], cwd=worktree)
        return {"ok": True, "action": "cloned"}

    # Branch doesn't exist: create orphan + push
    _git(["checkout", "--orphan", branch], cwd=worktree)
    _git(
        ["commit", "--allow-empty", "-m", "chore: init emerge-hub", "--author", author],
        cwd=worktree,
    )
    _git(["push", "-u", "origin", branch], cwd=worktree)
    return {"ok": True, "action": "created"}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_emerge_sync.py -q
```
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/emerge_sync.py tests/test_emerge_sync.py
git commit -m "feat: add git operations to emerge_sync"
```

---

### Task 4: emerge_sync.py — push/pull flows, conflict handling, poll loop, and CLI entry point

**Files:**
- Modify: `scripts/emerge_sync.py` (add push_flow, pull_flow, conflict handling, poll loop, `__main__`)

- [ ] **Step 1: Write failing test**

Add to `tests/test_emerge_sync.py`:

```python
def test_push_flow_exports_and_pushes(git_setup, connector_home, monkeypatch):
    from scripts.emerge_sync import push_flow
    bare_remote, worktree, hub_home = git_setup
    connectors, _ = connector_home
    _make_connector(connectors, "gmail")
    _init_hub_worktree(worktree, str(bare_remote))

    result = push_flow("gmail", connectors_root=connectors, hub_worktree=worktree)
    assert result["ok"] is True


def test_pull_flow_imports_remote_changes(git_setup, connector_home, monkeypatch):
    from scripts.emerge_sync import pull_flow, push_flow
    bare_remote, worktree_a, hub_home = git_setup
    connectors_a, _ = connector_home

    # Machine A pushes
    _make_connector(connectors_a, "gmail")
    _init_hub_worktree(worktree_a, str(bare_remote))
    push_flow("gmail", connectors_root=connectors_a, hub_worktree=worktree_a)

    # Machine B's connectors + separate worktree (initialize from same bare remote)
    connectors_b = hub_home / "connectors_b"
    worktree_b = hub_home / "worktree_b"
    _init_hub_worktree(worktree_b, str(bare_remote))
    result = pull_flow("gmail", connectors_root=connectors_b, hub_worktree=worktree_b)
    assert result["ok"] is True
    assert (connectors_b / "gmail" / "pipelines" / "read" / "fetch.py").exists()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_emerge_sync.py::test_push_flow_exports_and_pushes tests/test_emerge_sync.py::test_pull_flow_imports_remote_changes -q
```
Expected: FAIL with `ImportError: cannot import name 'push_flow' from 'scripts.emerge_sync'`

- [ ] **Step 3: Add push_flow, pull_flow, conflict handling, and poll loop to emerge_sync.py**

Append to `scripts/emerge_sync.py`:

```python
# ── Conflict detection ──────────────────────────────────────────────────────

def _build_conflict_entries(
    conflict_files: list[str],
    connector: str,
    pipelines_registry: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build structured conflict entries with metadata for AI analysis."""
    entries = []
    for file_path in conflict_files:
        cid = new_conflict_id()
        entry: dict[str, Any] = {
            "conflict_id": cid,
            "connector": connector,
            "file": file_path,
            "status": "pending",
            "resolution": None,
            "ours_ts_ms": int(time.time() * 1000),
            "theirs_ts_ms": 0,
        }
        # Annotate with quality data if available
        if pipelines_registry:
            mode = "write" if "/write/" in file_path else "read"
            pipeline_key = f"{connector}.{mode}.{Path(file_path).stem}"
            reg_entry = pipelines_registry.get("pipelines", {}).get(pipeline_key)
            if isinstance(reg_entry, dict):
                entry["ours_success_rate"] = reg_entry.get("success_rate", 0.0)
                entry["ours_attempts"] = reg_entry.get("attempts", 0)
        entries.append(entry)
    return entries


def record_conflicts(connector: str, conflict_files: list[str]) -> None:
    """Write conflict entries to pending-conflicts.json and enqueue notification event."""
    data = load_pending_conflicts()
    new_entries = _build_conflict_entries(conflict_files, connector)
    data["conflicts"].extend(new_entries)
    save_pending_conflicts(data)
    append_sync_event({
        "event": "conflicts_pending",
        "connector": connector,
        "count": len(new_entries),
        "ts_ms": int(time.time() * 1000),
    })
    logger.warning(
        "Hub sync: %d conflict(s) for connector '%s' — resolve via icc_hub(action='status')",
        len(new_entries), connector,
    )


# ── Push and pull flows ─────────────────────────────────────────────────────

def push_flow(
    connector: str,
    *,
    connectors_root: Path | None = None,
    hub_worktree: Path | None = None,
) -> dict[str, Any]:
    """Full push flow: merge remote → export local → git commit+push."""
    cfg = load_hub_config()
    worktree = hub_worktree or hub_worktree_path()
    conns_root = connectors_root or _connectors_root()
    author = cfg.get("author", "emerge-sync <emerge-sync@local>")
    branch = cfg.get("branch", "emerge-hub")

    # 1. Merge remote changes first (merge before push)
    merge_result = git_merge_remote(worktree, branch, author=author)
    if merge_result.get("conflict"):
        record_conflicts(connector, merge_result["files"])
        return {"ok": False, "conflict": True, "files": merge_result["files"]}
    if not merge_result.get("ok"):
        return {"ok": False, "error": merge_result.get("error", "merge failed")}

    # 2. Export local connector assets into hub worktree
    export_vertical(connector, connectors_root=conns_root, hub_worktree=worktree)

    # 3. Commit and push
    result = git_push(worktree, branch, connector=connector, author=author)
    if result.get("ok"):
        append_sync_event({
            "event": "consumed",
            "connector": connector,
            "ts_ms": int(time.time() * 1000),
        })
    return result


def pull_flow(
    connector: str,
    *,
    connectors_root: Path | None = None,
    hub_worktree: Path | None = None,
) -> dict[str, Any]:
    """Full pull flow: fetch → check for changes → merge → import."""
    cfg = load_hub_config()
    worktree = hub_worktree or hub_worktree_path()
    conns_root = connectors_root or _connectors_root()
    author = cfg.get("author", "emerge-sync <emerge-sync@local>")
    branch = cfg.get("branch", "emerge-hub")

    # 1. Check for remote changes
    if not git_has_remote_changes(worktree, branch):
        return {"ok": True, "action": "up_to_date"}

    # 2. Merge
    merge_result = git_merge_remote(worktree, branch, author=author)
    if merge_result.get("conflict"):
        record_conflicts(connector, merge_result["files"])
        return {"ok": False, "conflict": True, "files": merge_result["files"]}
    if not merge_result.get("ok"):
        return {"ok": False, "error": merge_result.get("error", "merge failed")}

    # 3. Import updated assets
    import_vertical(connector, connectors_root=conns_root, hub_worktree=worktree)

    # 4. Notify daemon to reload
    append_sync_event({
        "event": "reload",
        "connector": connector,
        "ts_ms": int(time.time() * 1000),
    })
    return {"ok": True, "action": "imported"}


# ── Resolution application ──────────────────────────────────────────────────

def _apply_pending_resolutions(worktree: Path) -> bool:
    """Apply any resolved conflicts in pending-conflicts.json via git checkout --ours/--theirs.

    Returns True if any resolutions were applied (caller should git commit).
    """
    data = load_pending_conflicts()
    resolved = [c for c in data.get("conflicts", []) if c.get("status") == "resolved" and c.get("resolution") in ("ours", "theirs")]
    if not resolved:
        return False

    for conflict in resolved:
        file_path = conflict["file"]
        resolution = conflict["resolution"]
        choice = "--ours" if resolution == "ours" else "--theirs"
        result = _git(["checkout", choice, file_path], cwd=worktree, check=False)
        if result.returncode == 0:
            _git(["add", file_path], cwd=worktree, check=False)
            conflict["status"] = "applied"
        else:
            logger.warning("Failed to apply resolution for %s: %s", file_path, result.stderr.strip())

    save_pending_conflicts(data)

    # Commit all applied resolutions
    status = _git(["status", "--porcelain"], cwd=worktree, check=False)
    if status.stdout.strip():
        _git(["commit", "-m", "hub: apply conflict resolutions"], cwd=worktree, check=False)
    return True


# ── Poll loop ──────────────────────────────────────────────────────────────

def _run_stable_events() -> None:
    """Consume 'stable' events from sync-queue and trigger push_flow for each connector."""
    cfg = load_hub_config()
    worktree = hub_worktree_path()
    if worktree.exists():
        _apply_pending_resolutions(worktree)
    selected = set(cfg.get("selected_verticals", []))
    events = consume_sync_events(
        lambda e: e.get("event") in ("stable", "pull_requested") and e.get("connector") in selected
    )
    push_processed: set[str] = set()
    pull_requested: set[str] = set()
    for event in events:
        connector = event["connector"]
        if event.get("event") == "pull_requested":
            pull_requested.add(connector)
            continue
        if connector in push_processed:
            continue
        push_processed.add(connector)
        try:
            result = push_flow(connector)
            if result.get("ok"):
                logger.info("Hub push OK for %s", connector)
            elif result.get("conflict"):
                logger.warning("Hub push conflict for %s — %d file(s)", connector, len(result.get("files", [])))
            else:
                logger.error("Hub push failed for %s: %s", connector, result.get("error", "unknown"))
        except Exception as exc:
            logger.error("Hub push exception for %s: %s", connector, exc)

    for connector in pull_requested:
        try:
            result = pull_flow(connector)
            if result.get("action") == "imported":
                logger.info("Hub pull OK for %s (manual sync)", connector)
        except Exception as exc:
            logger.error("Hub pull exception for %s: %s", connector, exc)


def _run_pull_cycle() -> None:
    """Pull updates for all selected verticals."""
    cfg = load_hub_config()
    for connector in cfg.get("selected_verticals", []):
        try:
            result = pull_flow(connector)
            if result.get("action") == "imported":
                logger.info("Hub pull: imported updates for %s", connector)
        except Exception as exc:
            logger.error("Hub pull exception for %s: %s", connector, exc)


def run_poll_loop(stop_event: threading.Event | None = None) -> None:
    """Main sync agent loop. Polls stable events and runs periodic pull."""
    cfg = load_hub_config()
    poll_interval = int(cfg.get("poll_interval_seconds", 300))
    last_pull = 0.0
    logger.info("emerge_sync: poll loop started (interval=%ds)", poll_interval)
    while True:
        if stop_event and stop_event.is_set():
            break
        try:
            _run_stable_events()
            now = time.time()
            if now - last_pull >= poll_interval:
                _run_pull_cycle()
                last_pull = now
        except Exception as exc:
            logger.error("emerge_sync poll loop error: %s", exc)
        time.sleep(10)


# ── CLI entry point ─────────────────────────────────────────────────────────

def cmd_setup() -> None:
    """Interactive setup wizard."""
    print("emerge_sync setup")
    remote = input("Remote URL (e.g. git@quasar:team/hub.git): ").strip()
    branch = input("Branch name [emerge-hub]: ").strip() or "emerge-hub"
    author = input("Author (e.g. alice <alice@team.com>): ").strip()

    # List local connectors
    conns_root = _connectors_root()
    available: list[str] = []
    if conns_root.exists():
        available = [d.name for d in conns_root.iterdir() if d.is_dir()]
    if not available:
        print("No local connectors found. Add connectors first.")
        return
    print(f"Available connectors: {', '.join(available)}")
    selected_input = input("Select connectors (comma-separated): ").strip()
    selected = [s.strip() for s in selected_input.split(",") if s.strip() in available]

    cfg = {
        "remote": remote,
        "branch": branch,
        "poll_interval_seconds": 300,
        "selected_verticals": selected,
        "author": author,
    }
    save_hub_config(cfg)

    worktree = hub_worktree_path()
    print(f"Setting up hub worktree at {worktree}...")
    result = git_setup_worktree(worktree, remote, branch, author)
    print(f"Worktree ready: {result['action']}")
    print("Running initial pull...")
    for connector in selected:
        pull_flow(connector)
    print("Setup complete.")


def cmd_sync(connector: str | None = None) -> None:
    cfg = load_hub_config()
    verticals = [connector] if connector else cfg.get("selected_verticals", [])
    for c in verticals:
        result = push_flow(c)
        if result.get("ok"):
            print(f"sync {c}: ok")
        elif result.get("conflict"):
            print(f"sync {c}: conflict — resolve via icc_hub(action='status')")
        else:
            print(f"sync {c}: error — {result.get('error', 'unknown')}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = sys.argv[1:]
    if not args or args[0] == "run":
        if not is_configured():
            print("Not configured. Run: python scripts/emerge_sync.py setup")
            sys.exit(1)
        run_poll_loop()
    elif args[0] == "setup":
        cmd_setup()
    elif args[0] == "sync":
        connector_arg = args[1] if len(args) > 1 else None
        cmd_sync(connector_arg)
    else:
        print(f"Unknown command: {args[0]}")
        print("Usage: emerge_sync.py [run|setup|sync [connector]]")
        sys.exit(1)
```

- [ ] **Step 4: Run all emerge_sync tests**

```bash
python -m pytest tests/test_emerge_sync.py -q
```
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/emerge_sync.py tests/test_emerge_sync.py
git commit -m "feat: add push/pull flows and poll loop to emerge_sync"
```

---

### Task 5: daemon — write stable events to sync-queue

**Files:**
- Modify: `scripts/emerge_daemon.py` (write to sync-queue when status transitions to stable)
- Test: `tests/test_mcp_tools_integration.py` (add stable event test)

- [ ] **Step 1: Write failing test**

Add to `tests/test_mcp_tools_integration.py`:

```python
def test_stable_transition_writes_to_sync_queue(tmp_path, monkeypatch):
    """When a pipeline reaches stable, daemon writes a 'stable' event to sync-queue."""
    import sys
    from pathlib import Path
    ROOT = Path(__file__).resolve().parents[1]
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    from scripts.emerge_daemon import EmergeDaemon
    from scripts.hub_config import sync_queue_path, consume_sync_events
    import json

    # Point hub home at tmp_path so queue goes there, not ~/.emerge
    monkeypatch.setenv("EMERGE_HUB_HOME", str(tmp_path))
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path / "state"))

    daemon = EmergeDaemon(root=ROOT)

    # Inject a pipeline entry at canary with enough stats to transition to stable
    from scripts.policy_config import (
        STABLE_MIN_ATTEMPTS, STABLE_MIN_SUCCESS_RATE, STABLE_MIN_VERIFY_RATE
    )
    import time
    registry_path = daemon._state_root / "pipelines-registry.json"
    registry = {
        "pipelines": {
            "gmail.read.fetch": {
                "status": "canary",
                "rollout_pct": 20,
                "attempts": STABLE_MIN_ATTEMPTS,
                "successes": STABLE_MIN_ATTEMPTS,
                "verifications": STABLE_MIN_ATTEMPTS,
                "recent_outcomes": [1] * STABLE_MIN_ATTEMPTS,
                "consecutive_failures": 0,
                "last_ts_ms": int(time.time() * 1000),
            }
        }
    }
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    registry_path.write_text(json.dumps(registry), encoding="utf-8")

    # Also set up hub-config so daemon knows gmail is selected
    from scripts.hub_config import save_hub_config
    save_hub_config({
        "remote": "git@quasar:team/hub.git",
        "selected_verticals": ["gmail"],
    })

    # Build a candidate entry with stats sufficient to trigger stable transition
    entry = {
        "intent_signature": "gmail.read.fetch",
        "status": "canary",
        "attempts": STABLE_MIN_ATTEMPTS,
        "successes": STABLE_MIN_ATTEMPTS,
        "verify_passes": STABLE_MIN_ATTEMPTS,
        "human_fixes": 0,
        "consecutive_failures": 0,
        "recent_outcomes": [1] * STABLE_MIN_ATTEMPTS,
        "last_ts_ms": int(time.time() * 1000),
    }
    daemon._update_pipeline_registry(candidate_key="gmail.read.fetch", entry=entry)

    # Check sync-queue has a stable event
    events = consume_sync_events(lambda e: e.get("event") == "stable")
    assert any(e.get("connector") == "gmail" for e in events), \
        f"Expected stable event for gmail, got: {events}"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_stable_transition_writes_to_sync_queue -q
```
Expected: FAIL (no stable event written)

- [ ] **Step 3: Add sync-queue write to `_update_pipeline_registry` in emerge_daemon.py**

In `scripts/emerge_daemon.py`, find the block around line 2003–2015:
```python
        if transitioned:
            pipeline["last_transition_reason"] = reason
            pipeline["attempts_at_transition"] = attempts
            try:
                self._sink.emit(
                    "policy.transition",
                    {"candidate_key": candidate_key, "new_status": status, "session_id": self._base_session_id},
                )
            except Exception:
                pass
```

Add the stable event write after `self._sink.emit(...)`:

```python
        if transitioned:
            pipeline["last_transition_reason"] = reason
            pipeline["attempts_at_transition"] = attempts
            try:
                self._sink.emit(
                    "policy.transition",
                    {"candidate_key": candidate_key, "new_status": status, "session_id": self._base_session_id},
                )
            except Exception:
                pass
            if status == "stable":
                try:
                    from scripts.hub_config import append_sync_event, load_hub_config, is_configured
                    if is_configured():
                        parts = candidate_key.split(".", 2)
                        connector = parts[0] if parts else candidate_key
                        cfg = load_hub_config()
                        if connector in cfg.get("selected_verticals", []):
                            pipeline_name = parts[2] if len(parts) >= 3 else candidate_key
                            import time as _time
                            append_sync_event({
                                "event": "stable",
                                "connector": connector,
                                "pipeline": pipeline_name,
                                "ts_ms": int(_time.time() * 1000),
                            })
                except Exception:
                    pass
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_stable_transition_writes_to_sync_queue -q
```
Expected: PASS

- [ ] **Step 5: Run full test suite to verify no regressions**

```bash
python -m pytest tests -q
```
Expected: all previous tests still pass

- [ ] **Step 6: Commit**

```bash
git add scripts/emerge_daemon.py tests/test_mcp_tools_integration.py
git commit -m "feat: daemon writes stable event to sync-queue on policy promotion"
```

---

### Task 6: daemon — icc_hub MCP tool

**Files:**
- Modify: `scripts/emerge_daemon.py` (add `icc_hub` handler in `call_tool` + registration in `tools/list`)
- Test: `tests/test_mcp_tools_integration.py` (add icc_hub tests)

- [ ] **Step 1: Write failing tests**

Add to `tests/test_mcp_tools_integration.py`:

```python
def test_icc_hub_list_returns_config(tmp_path, monkeypatch):
    from scripts.emerge_daemon import EmergeDaemon
    from scripts.hub_config import save_hub_config

    monkeypatch.setenv("EMERGE_HUB_HOME", str(tmp_path))
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path / "state"))
    (tmp_path / "state").mkdir(parents=True)

    save_hub_config({
        "remote": "git@quasar:team/hub.git",
        "branch": "emerge-hub",
        "selected_verticals": ["gmail", "linear"],
    })

    daemon = EmergeDaemon(root=ROOT)
    result = daemon.call_tool("icc_hub", {"action": "list"})
    assert not result["isError"]
    import json
    payload = json.loads(result["content"][0]["text"])
    assert "gmail" in payload["selected_verticals"]
    assert payload["remote"] == "git@quasar:team/hub.git"


def test_icc_hub_add_connector(tmp_path, monkeypatch):
    from scripts.emerge_daemon import EmergeDaemon
    from scripts.hub_config import save_hub_config, load_hub_config

    monkeypatch.setenv("EMERGE_HUB_HOME", str(tmp_path))
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path / "state"))
    (tmp_path / "state").mkdir(parents=True)

    save_hub_config({"remote": "git@x:y.git", "selected_verticals": ["gmail"]})

    daemon = EmergeDaemon(root=ROOT)
    result = daemon.call_tool("icc_hub", {"action": "add", "connector": "linear"})
    assert not result["isError"]
    cfg = load_hub_config()
    assert "linear" in cfg["selected_verticals"]


def test_icc_hub_remove_connector(tmp_path, monkeypatch):
    from scripts.emerge_daemon import EmergeDaemon
    from scripts.hub_config import save_hub_config, load_hub_config

    monkeypatch.setenv("EMERGE_HUB_HOME", str(tmp_path))
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path / "state"))
    (tmp_path / "state").mkdir(parents=True)

    save_hub_config({"remote": "git@x:y.git", "selected_verticals": ["gmail", "slack"]})

    daemon = EmergeDaemon(root=ROOT)
    result = daemon.call_tool("icc_hub", {"action": "remove", "connector": "slack"})
    assert not result["isError"]
    cfg = load_hub_config()
    assert "slack" not in cfg["selected_verticals"]
    assert "gmail" in cfg["selected_verticals"]


def test_icc_hub_status_shows_pending_conflicts(tmp_path, monkeypatch):
    from scripts.emerge_daemon import EmergeDaemon
    from scripts.hub_config import save_hub_config, save_pending_conflicts
    import json

    monkeypatch.setenv("EMERGE_HUB_HOME", str(tmp_path))
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path / "state"))
    (tmp_path / "state").mkdir(parents=True)

    save_hub_config({"remote": "git@x:y.git", "selected_verticals": ["gmail"]})
    save_pending_conflicts({"conflicts": [
        {"conflict_id": "abc", "connector": "gmail", "file": "fetch.py",
         "status": "pending", "resolution": None, "ours_ts_ms": 1, "theirs_ts_ms": 0}
    ]})

    daemon = EmergeDaemon(root=ROOT)
    result = daemon.call_tool("icc_hub", {"action": "status"})
    assert not result["isError"]
    payload = json.loads(result["content"][0]["text"])
    assert payload["pending_conflicts"] == 1


def test_icc_hub_sync_enqueues_push_and_pull(tmp_path, monkeypatch):
    from scripts.emerge_daemon import EmergeDaemon
    from scripts.hub_config import save_hub_config, consume_sync_events
    import json

    monkeypatch.setenv("EMERGE_HUB_HOME", str(tmp_path))
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path / "state"))
    (tmp_path / "state").mkdir(parents=True)

    save_hub_config({"remote": "git@x:y.git", "selected_verticals": ["gmail"]})

    daemon = EmergeDaemon(root=ROOT)
    result = daemon.call_tool("icc_hub", {"action": "sync", "connector": "gmail"})
    assert not result["isError"]
    payload = json.loads(result["content"][0]["text"])
    assert "gmail" in payload["triggered"]

    stable_events = consume_sync_events(lambda e: e.get("event") == "stable")
    pull_events = consume_sync_events(lambda e: e.get("event") == "pull_requested")
    assert any(e["connector"] == "gmail" for e in stable_events)
    assert any(e["connector"] == "gmail" for e in pull_events)


def test_icc_hub_resolve_conflict(tmp_path, monkeypatch):
    from scripts.emerge_daemon import EmergeDaemon
    from scripts.hub_config import save_hub_config, save_pending_conflicts, load_pending_conflicts
    import json

    monkeypatch.setenv("EMERGE_HUB_HOME", str(tmp_path))
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path / "state"))
    (tmp_path / "state").mkdir(parents=True)

    save_hub_config({"remote": "git@x:y.git", "selected_verticals": ["gmail"]})
    save_pending_conflicts({"conflicts": [
        {"conflict_id": "abc", "connector": "gmail", "file": "fetch.py",
         "status": "pending", "resolution": None, "ours_ts_ms": 1, "theirs_ts_ms": 0}
    ]})

    daemon = EmergeDaemon(root=ROOT)
    result = daemon.call_tool("icc_hub", {
        "action": "resolve",
        "conflict_id": "abc",
        "resolution": "ours",
    })
    assert not result["isError"]
    data = load_pending_conflicts()
    conflict = data["conflicts"][0]
    assert conflict["resolution"] == "ours"
    assert conflict["status"] == "resolved"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_icc_hub_list_returns_config tests/test_mcp_tools_integration.py::test_icc_hub_add_connector -q
```
Expected: FAIL with `icc_hub` returning `_tool_error`

- [ ] **Step 3: Add icc_hub handler to call_tool in emerge_daemon.py**

In `scripts/emerge_daemon.py`, find the final `return self._tool_error(f"Unknown tool: {name}")` at the end of `call_tool` and add the `icc_hub` block before it:

```python
        if name == "icc_hub":
            return self._handle_icc_hub(arguments)
```

Then add the handler method to `EmergeDaemon` (before `call_tool` or after it):

```python
    def _handle_icc_hub(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from scripts.hub_config import (
            load_hub_config,
            save_hub_config,
            load_pending_conflicts,
            save_pending_conflicts,
            consume_sync_events,
            append_sync_event,
            is_configured,
        )
        import time as _time

        action = str(arguments.get("action", "")).strip()

        if action == "list":
            cfg = load_hub_config()
            return self._tool_ok_json({
                "remote": cfg.get("remote", ""),
                "branch": cfg.get("branch", "emerge-hub"),
                "selected_verticals": cfg.get("selected_verticals", []),
                "poll_interval_seconds": cfg.get("poll_interval_seconds", 300),
                "configured": is_configured(),
            })

        if action == "add":
            connector = str(arguments.get("connector", "")).strip()
            if not connector:
                return self._tool_error("icc_hub add: 'connector' is required")
            cfg = load_hub_config()
            selected = list(cfg.get("selected_verticals", []))
            if connector not in selected:
                selected.append(connector)
                cfg["selected_verticals"] = selected
                save_hub_config(cfg)
            return self._tool_ok_json({"ok": True, "selected_verticals": selected})

        if action == "remove":
            connector = str(arguments.get("connector", "")).strip()
            if not connector:
                return self._tool_error("icc_hub remove: 'connector' is required")
            cfg = load_hub_config()
            selected = [v for v in cfg.get("selected_verticals", []) if v != connector]
            cfg["selected_verticals"] = selected
            save_hub_config(cfg)
            return self._tool_ok_json({"ok": True, "selected_verticals": selected})

        if action == "status":
            cfg = load_hub_config()
            pending = load_pending_conflicts()
            unresolved = [c for c in pending.get("conflicts", []) if c.get("status") == "pending"]
            # Count queued stable events
            from scripts.hub_config import sync_queue_path
            queue_depth = 0
            qp = sync_queue_path()
            if qp.exists():
                queue_depth = sum(1 for line in qp.read_text(encoding="utf-8").splitlines() if line.strip())
            return self._tool_ok_json({
                "configured": is_configured(),
                "remote": cfg.get("remote", ""),
                "selected_verticals": cfg.get("selected_verticals", []),
                "pending_conflicts": len(unresolved),
                "conflicts": unresolved,
                "queue_depth": queue_depth,
            })

        if action == "resolve":
            conflict_id = str(arguments.get("conflict_id", "")).strip()
            resolution = str(arguments.get("resolution", "")).strip()
            if not conflict_id:
                return self._tool_error("icc_hub resolve: 'conflict_id' is required")
            if resolution not in ("ours", "theirs", "skip"):
                return self._tool_error("icc_hub resolve: 'resolution' must be ours|theirs|skip")
            data = load_pending_conflicts()
            matched = False
            for conflict in data.get("conflicts", []):
                if conflict.get("conflict_id") == conflict_id:
                    conflict["resolution"] = resolution
                    conflict["status"] = "resolved"
                    matched = True
                    break
            if not matched:
                return self._tool_error(f"icc_hub resolve: conflict_id '{conflict_id}' not found")
            save_pending_conflicts(data)
            # Signal emerge_sync to apply resolutions
            append_sync_event({
                "event": "resolution_applied",
                "conflict_id": conflict_id,
                "resolution": resolution,
                "ts_ms": int(_time.time() * 1000),
            })
            return self._tool_ok_json({"ok": True, "conflict_id": conflict_id, "resolution": resolution})

        if action == "sync":
            connector = str(arguments.get("connector", "")).strip() or None
            cfg = load_hub_config()
            verticals = [connector] if connector else cfg.get("selected_verticals", [])
            ts = int(_time.time() * 1000)
            for c in verticals:
                append_sync_event({
                    "event": "stable",
                    "connector": c,
                    "pipeline": "__manual__",
                    "ts_ms": ts,
                })
                append_sync_event({
                    "event": "pull_requested",
                    "connector": c,
                    "ts_ms": ts,
                })
            return self._tool_ok_json({"ok": True, "triggered": verticals})

        if action == "setup":
            append_sync_event({
                "event": "setup_requested",
                "ts_ms": int(_time.time() * 1000),
            })
            return self._tool_ok_json({
                "ok": True,
                "message": (
                    "Setup wizard runs in a terminal: python scripts/emerge_sync.py setup. "
                    "Run it there, then call icc_hub(action='list') to verify configuration."
                ),
            })

        return self._tool_error(f"icc_hub: unknown action '{action}'. Valid: list|add|remove|sync|status|resolve|setup")
```

- [ ] **Step 4: Register icc_hub in tools/list schema in emerge_daemon.py**

Find the `tools/list` response block (around line 1260 where the list ends) and add `icc_hub` before the closing `]`:

```python
                        {
                            "name": "icc_hub",
                            "description": (
                                "Manage Memory Hub — bidirectional connector asset sync via a self-hosted git repo. "
                                "Actions: list (show config), add/remove (manage verticals), "
                                "sync (manual push+pull), status (show pending conflicts), "
                                "resolve (resolve a conflict with ours|theirs|skip)."
                            ),
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "action": {
                                        "type": "string",
                                        "enum": ["list", "add", "remove", "sync", "status", "resolve", "setup"],
                                        "description": "Hub action to perform",
                                    },
                                    "connector": {
                                        "type": "string",
                                        "description": "Connector name (required for add/remove, optional for sync)",
                                    },
                                    "conflict_id": {
                                        "type": "string",
                                        "description": "Conflict ID from status output (required for resolve)",
                                    },
                                    "resolution": {
                                        "type": "string",
                                        "enum": ["ours", "theirs", "skip"],
                                        "description": "Resolution choice (required for resolve)",
                                    },
                                },
                                "required": ["action"],
                            },
                        },
```

- [ ] **Step 5: Run all icc_hub tests**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_icc_hub_list_returns_config tests/test_mcp_tools_integration.py::test_icc_hub_add_connector tests/test_mcp_tools_integration.py::test_icc_hub_remove_connector tests/test_mcp_tools_integration.py::test_icc_hub_status_shows_pending_conflicts tests/test_mcp_tools_integration.py::test_icc_hub_sync_enqueues_push_and_pull tests/test_mcp_tools_integration.py::test_icc_hub_resolve_conflict -q
```
Expected: 6 passed

- [ ] **Step 6: Run full test suite**

```bash
python -m pytest tests -q
```
Expected: all tests pass

- [ ] **Step 7: Commit**

```bash
git add scripts/emerge_daemon.py tests/test_mcp_tools_integration.py
git commit -m "feat: add icc_hub MCP tool to emerge_daemon"
```

---

### Task 7: docs — README and CLAUDE.md updates

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add Memory Hub to README.md component table**

Find the component table in README.md (the table with `EmergeDaemon`, `PipelineEngine`, etc.) and add a row:

```markdown
| `emerge_sync.py` | Memory Hub sync agent | Bidirectional connector asset sync via orphan-branch git repo; event-driven push on stable, periodic pull, AI-assisted conflict resolution via `icc_hub` MCP tool |
```

- [ ] **Step 2: Add hub architecture notes to CLAUDE.md**

In `CLAUDE.md`, find the Architecture section and add after the `**Frozen flag**` paragraph:

```markdown
**Memory Hub**: `emerge_sync.py` is a standalone sync agent that shares connector assets (pipelines, NOTES.md, spans.json) via a self-hosted git repo's orphan branch (`emerge-hub`). The daemon writes a `stable` event to `~/.emerge/sync-queue.jsonl` when a pipeline is promoted to stable; emerge_sync polls the queue and triggers a push flow. A background timer drives periodic pull. Conflicts are written to `~/.emerge/pending-conflicts.json` and resolved via `icc_hub(action="resolve", ...)`. Hub config lives in `~/.emerge/hub-config.json`. Never synced: credentials, operator-events, pipelines-registry.json.
```

- [ ] **Step 3: Update Documentation Update Rules table in CLAUDE.md**

Add a row to the table:

```markdown
| Memory Hub config or sync flow change | `README.md` component table + `CLAUDE.md` Architecture section |
```

- [ ] **Step 4: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs: add Memory Hub to README and CLAUDE.md"
```

---

## Self-Review

### Spec Coverage

| Spec requirement | Task |
|-----------------|------|
| Orphan branch layout (connectors/vertical/...) | Task 2: export_vertical |
| hub-config.json schema | Task 1: hub_config.py |
| sync-queue.jsonl event format | Task 1: hub_config.py |
| pending-conflicts.json | Task 1: hub_config.py |
| Push flow (fetch+merge → export → push) | Task 4: push_flow |
| Pull flow (fetch → detect changes → merge → import) | Task 4: pull_flow |
| Initial setup wizard | Task 4: cmd_setup |
| Conflict detection → pending-conflicts.json | Task 4: record_conflicts |
| Conflict resolution via icc_hub | Task 6: resolve action |
| AI status surface (icc_hub status) | Task 6: status action |
| icc_hub list/add/remove/sync/status/resolve/setup | Task 6 |
| Stable event from daemon | Task 5 |
| Export: spans.json from stable candidates only | Task 2: _export_spans_json |
| Import: spans merge (newer wins) | Task 2: _import_spans_json |
| Poll loop with configurable interval | Task 4: run_poll_loop |
| Error handling: remote unreachable → skip | Task 4: _run_pull_cycle try/except |
| README + CLAUDE.md update | Task 7 |

### No Placeholders: Confirmed clean.

### Type/Method Consistency Check

- `export_vertical` / `import_vertical` used in Task 2 tests → defined in Task 2 impl ✓
- `push_flow` / `pull_flow` used in Task 4 tests → defined in Task 4 impl ✓
- `_git`, `git_has_remote_changes`, `git_push`, `git_merge_remote`, `git_setup_worktree` used in Task 3 tests → defined in Task 3 impl ✓
- `_handle_icc_hub` called from `call_tool` → defined in Task 6 ✓
- `hub_config.py` functions referenced in daemon Task 5+6 → all defined in Task 1 ✓
