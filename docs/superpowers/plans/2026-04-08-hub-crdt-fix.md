# Hub Per-Pipeline CRDT Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the Memory Hub so that a second member joining an existing hub never silently overwrites teammates' pipelines — each pipeline is independently merged by `last_ts_ms`, making the hub a union of all members' stable work.

**Architecture:** Three targeted changes: (1) `export_vertical` becomes additive — it compares per-pipeline `last_ts_ms` from local `span-candidates.json` vs remote `spans.json` and only overwrites when local is newer; (2) `_export_spans_json` merges into the existing worktree `spans.json` instead of overwriting; (3) `icc_hub configure` calls `import_vertical` after cloning so the joining member immediately receives all existing pipelines.

**Tech Stack:** Python 3, `scripts/emerge_sync.py`, `scripts/emerge_daemon.py`, `tests/test_emerge_sync.py`, `tests/test_mcp_tools_integration.py`

---

## File Map

| File | Change |
|------|--------|
| `scripts/emerge_sync.py` | Add 3 helpers; rewrite `export_vertical`; rewrite `_export_spans_json` |
| `scripts/emerge_daemon.py` | Add `import_vertical` call in `_handle_icc_hub configure` after `"cloned"` |
| `tests/test_emerge_sync.py` | Add 4 new tests |
| `tests/test_mcp_tools_integration.py` | Add 1 new test |

---

## Task 1: Add helper functions to `emerge_sync.py`

**Files:**
- Modify: `scripts/emerge_sync.py` — add after the `_connectors_root` function (line ~38)

These three helpers are used by the rewritten `export_vertical`. Add them between `_connectors_root` and the `# ── Export ──` section comment.

- [ ] **Step 1: Write the failing tests for the helpers**

Add to `tests/test_emerge_sync.py` (after the existing imports, before `_make_connector`):

```python
from scripts.emerge_sync import (
    _file_to_intent_sig,
    _load_candidate_timestamps,
    _load_spans_timestamps,
)
from pathlib import Path


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
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
python -m pytest tests/test_emerge_sync.py::test_file_to_intent_sig_read tests/test_emerge_sync.py::test_load_candidate_timestamps_returns_stable_only tests/test_emerge_sync.py::test_load_spans_timestamps_parses_spans_json -q
```

Expected: `ImportError` or `AttributeError` — functions don't exist yet.

- [ ] **Step 3: Add the three helpers to `emerge_sync.py`**

In `scripts/emerge_sync.py`, insert after the `_connectors_root` function (after line ~38, before `# ── Export ──`):

```python
def _file_to_intent_sig(connector: str, rel: Path) -> str:
    """Convert pipelines/read/foo.py → connector.read.foo"""
    parts = rel.parts
    if len(parts) == 2:
        mode = parts[0]
        name = Path(parts[1]).stem
        return f"{connector}.{mode}.{name}"
    return ""


def _load_candidate_timestamps(connector_dir: Path) -> dict[str, int]:
    """Return {intent_sig: last_ts_ms} for stable entries in span-candidates.json."""
    p = connector_dir / "span-candidates.json"
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return {
            k: int(v.get("last_ts_ms", 0))
            for k, v in raw.get("candidates", {}).items()
            if isinstance(v, dict) and v.get("status") == "stable"
        }
    except Exception:
        return {}


def _load_spans_timestamps(worktree_connector_dir: Path) -> dict[str, int]:
    """Return {intent_sig: last_ts_ms} from spans.json in the hub worktree connector dir."""
    p = worktree_connector_dir / "spans.json"
    if not p.exists():
        return {}
    try:
        spans = json.loads(p.read_text(encoding="utf-8")).get("spans", {})
        return {k: int(v.get("last_ts_ms", 0)) for k, v in spans.items() if isinstance(v, dict)}
    except Exception:
        return {}
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
python -m pytest tests/test_emerge_sync.py::test_file_to_intent_sig_read tests/test_emerge_sync.py::test_file_to_intent_sig_write tests/test_emerge_sync.py::test_file_to_intent_sig_unknown_depth_returns_empty tests/test_emerge_sync.py::test_load_candidate_timestamps_returns_stable_only tests/test_emerge_sync.py::test_load_candidate_timestamps_missing_file tests/test_emerge_sync.py::test_load_spans_timestamps_parses_spans_json tests/test_emerge_sync.py::test_load_spans_timestamps_missing_file -q
```

Expected: all 7 PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/emerge_sync.py tests/test_emerge_sync.py
git commit -m "feat: add hub helper functions for per-pipeline timestamp comparison"
```

---

## Task 2: Fix `_export_spans_json` to merge instead of overwrite

**Files:**
- Modify: `scripts/emerge_sync.py:68-93` — rewrite `_export_spans_json`
- Test: `tests/test_emerge_sync.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_emerge_sync.py`:

```python
def test_export_spans_json_merges_remote_spans(connector_home):
    """Exporting B's spans must not erase A's spans already in the worktree."""
    from scripts.emerge_sync import export_vertical
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
```

- [ ] **Step 2: Run to verify it fails**

```bash
python -m pytest tests/test_emerge_sync.py::test_export_spans_json_merges_remote_spans -q
```

Expected: FAIL — A's span is missing from result (current code overwrites).

- [ ] **Step 3: Rewrite `_export_spans_json` in `emerge_sync.py`**

Replace the current `_export_spans_json` function (lines ~68-93):

```python
def _export_spans_json(src: Path, dst: Path) -> None:
    """Merge local stable spans into the worktree spans.json. Remote-only spans are preserved."""
    candidates_path = src / "span-candidates.json"
    if not candidates_path.exists():
        return
    try:
        raw = json.loads(candidates_path.read_text(encoding="utf-8"))
        candidates = raw.get("candidates", {})
    except Exception:
        return

    local_spans: dict[str, Any] = {}
    for key, entry in candidates.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("status") != "stable":
            continue
        local_spans[key] = {
            "intent_signature": entry.get("intent_signature", key),
            "status": "stable",
            "last_ts_ms": entry.get("last_ts_ms", 0),
        }

    # Load existing worktree spans so we don't erase other members' entries
    existing_path = dst / "spans.json"
    existing_spans: dict[str, Any] = {}
    if existing_path.exists():
        try:
            existing_spans = json.loads(existing_path.read_text(encoding="utf-8")).get("spans", {})
        except Exception:
            pass

    merged = dict(existing_spans)
    for key, entry in local_spans.items():
        existing = merged.get(key)
        if existing is None or entry.get("last_ts_ms", 0) >= existing.get("last_ts_ms", 0):
            merged[key] = entry

    dst.mkdir(parents=True, exist_ok=True)
    (dst / "spans.json").write_text(
        json.dumps({"spans": merged}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
```

- [ ] **Step 4: Run new test + existing spans tests to verify all pass**

```bash
python -m pytest tests/test_emerge_sync.py::test_export_spans_json_merges_remote_spans tests/test_emerge_sync.py::test_export_generates_spans_json_from_stable_candidates tests/test_emerge_sync.py::test_import_merges_spans_json_newer_wins -q
```

Expected: all 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/emerge_sync.py tests/test_emerge_sync.py
git commit -m "fix: export_spans_json merges into existing worktree spans instead of overwriting"
```

---

## Task 3: Fix `export_vertical` to be additive (per-pipeline timestamp comparison)

**Files:**
- Modify: `scripts/emerge_sync.py:43-65` — rewrite `export_vertical`
- Test: `tests/test_emerge_sync.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_emerge_sync.py`:

```python
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
```

- [ ] **Step 2: Run to verify they fail**

```bash
python -m pytest tests/test_emerge_sync.py::test_export_vertical_preserves_remote_only_pipeline tests/test_emerge_sync.py::test_export_vertical_local_wins_when_newer tests/test_emerge_sync.py::test_export_vertical_remote_wins_when_newer -q
```

Expected: `test_export_vertical_preserves_remote_only_pipeline` FAIL (rmtree wipes A's file), others may pass or fail.

- [ ] **Step 3: Rewrite `export_vertical` in `emerge_sync.py`**

Replace the current `export_vertical` function (lines ~43-65):

```python
def export_vertical(
    connector: str,
    *,
    connectors_root: Path | None = None,
    hub_worktree: Path | None = None,
) -> None:
    """Copy connector assets from local connectors dir into the hub worktree.

    Per-pipeline additive export: only overwrites a worktree pipeline when the
    local candidate has a higher (or equal) last_ts_ms than the remote version.
    Remote-only pipelines are left untouched — they remain in the worktree from
    the preceding git_merge_remote call in push_flow.
    """
    src = (connectors_root or _connectors_root()) / connector
    dst = (hub_worktree or hub_worktree_path()) / "connectors" / connector
    dst.mkdir(parents=True, exist_ok=True)

    src_pipelines = src / "pipelines"
    dst_pipelines = dst / "pipelines"

    if src_pipelines.exists():
        local_ts = _load_candidate_timestamps(src)
        remote_ts = _load_spans_timestamps(dst)

        for py_file in src_pipelines.rglob("*.py"):
            rel = py_file.relative_to(src_pipelines)
            intent_sig = _file_to_intent_sig(connector, rel)
            l_ts = local_ts.get(intent_sig, 0)
            r_ts = remote_ts.get(intent_sig, 0)
            if l_ts >= r_ts:
                dst_file = dst_pipelines / rel
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(py_file, dst_file)
                yaml_src = py_file.with_suffix(".yaml")
                if yaml_src.exists():
                    shutil.copy2(yaml_src, dst_file.with_suffix(".yaml"))

    notes_src = src / "NOTES.md"
    if notes_src.exists():
        shutil.copy2(notes_src, dst / "NOTES.md")

    _export_spans_json(src, dst)
```

- [ ] **Step 4: Run the new tests + existing export tests**

```bash
python -m pytest tests/test_emerge_sync.py::test_export_vertical_preserves_remote_only_pipeline tests/test_emerge_sync.py::test_export_vertical_local_wins_when_newer tests/test_emerge_sync.py::test_export_vertical_remote_wins_when_newer tests/test_emerge_sync.py::test_export_copies_pipelines_and_notes tests/test_emerge_sync.py::test_export_generates_spans_json_from_stable_candidates -q
```

Expected: all 5 PASS.

- [ ] **Step 5: Run the full emerge_sync test suite**

```bash
python -m pytest tests/test_emerge_sync.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/emerge_sync.py tests/test_emerge_sync.py
git commit -m "fix: export_vertical additive per-pipeline merge — preserves remote-only pipelines"
```

---

## Task 4: Fix `icc_hub configure` to import remote pipelines after cloning

**Files:**
- Modify: `scripts/emerge_daemon.py:1162-1185` — add `import_vertical` call after `action_taken == "cloned"`
- Test: `tests/test_mcp_tools_integration.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_mcp_tools_integration.py` (after `test_icc_hub_configure_saves_config_and_inits_worktree`):

```python
def test_icc_hub_configure_imports_existing_hub_on_clone(tmp_path, monkeypatch):
    """When configure clones an existing hub branch, remote pipelines must be
    imported into the local connectors directory immediately."""
    import subprocess
    from scripts.emerge_daemon import EmergeDaemon
    from scripts.hub_config import hub_worktree_path

    monkeypatch.setenv("EMERGE_HUB_HOME", str(tmp_path))
    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(tmp_path / "connectors"))
    (tmp_path / "state").mkdir(parents=True)

    # Create a bare remote
    bare = tmp_path / "remote.git"
    bare.mkdir()
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)

    # Machine A bootstraps the branch and pushes a pipeline
    worktree_a = tmp_path / "worktree_a"
    worktree_a.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@a.com",
           "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@a.com"}

    def _git(*args):
        subprocess.run(list(args), cwd=str(worktree_a), check=True, capture_output=True, env=env)

    _git("git", "init")
    _git("git", "config", "user.name", "a")
    _git("git", "config", "user.email", "a@a.com")
    _git("git", "remote", "add", "origin", str(bare))
    _git("git", "checkout", "--orphan", "emerge-hub")
    _git("git", "commit", "--allow-empty", "-m", "chore: init emerge-hub")
    _git("git", "push", "-u", "origin", "emerge-hub")

    # A pushes a pipeline file
    pipeline_dir = worktree_a / "connectors" / "cloud-server" / "pipelines" / "read"
    pipeline_dir.mkdir(parents=True)
    (pipeline_dir / "list_vms.py").write_text("# list_vms from A", encoding="utf-8")
    spans_dir = worktree_a / "connectors" / "cloud-server"
    (spans_dir / "spans.json").write_text(
        json.dumps({"spans": {"cloud-server.read.list_vms": {"intent_signature": "cloud-server.read.list_vms", "status": "stable", "last_ts_ms": 1000}}}),
        encoding="utf-8",
    )
    _git("git", "add", "-A")
    _git("git", "commit", "-m", "hub: sync cloud-server")
    _git("git", "push", "origin", "emerge-hub")

    # Machine B configures — should clone and import
    daemon = EmergeDaemon(root=ROOT)
    result = daemon.call_tool("icc_hub", {
        "action": "configure",
        "remote": str(bare),
        "author": "b <b@b.com>",
        "selected_verticals": ["cloud-server"],
        "branch": "emerge-hub",
    })
    assert not result["isError"], result["content"][0]["text"]
    payload = json.loads(result["content"][0]["text"])
    assert payload["action"] == "cloned"

    # B's local connectors must have A's pipeline
    local_pipeline = tmp_path / "connectors" / "cloud-server" / "pipelines" / "read" / "list_vms.py"
    assert local_pipeline.exists(), "A's pipeline must be imported to B's local connectors on configure"
    assert "list_vms from A" in local_pipeline.read_text(encoding="utf-8")
```

- [ ] **Step 2: Run to verify it fails**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_icc_hub_configure_imports_existing_hub_on_clone -q
```

Expected: FAIL — `local_pipeline.exists()` is False (configure doesn't import yet).

- [ ] **Step 3: Add the import call in `emerge_daemon.py`**

In `scripts/emerge_daemon.py`, find the configure block (around line 1162). After `action_taken = result.get("action", "unknown")`, add:

```python
            action_taken = result.get("action", "unknown")

            # If we just cloned an existing hub, import remote pipelines into local connectors
            # so the joining member starts with the full team's stable pipeline set.
            if action_taken == "cloned" and cfg.get("selected_verticals"):
                from scripts.emerge_sync import import_vertical as _import_vertical
                from scripts.emerge_sync import _connectors_root
                _conn_root = _connectors_root()
                for _connector in cfg["selected_verticals"]:
                    try:
                        _import_vertical(_connector, connectors_root=_conn_root, hub_worktree=worktree)
                    except Exception as _exc:
                        logger.warning("icc_hub configure: initial import failed for %s: %s", _connector, _exc)
```

- [ ] **Step 4: Run the new test**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_icc_hub_configure_imports_existing_hub_on_clone -q
```

Expected: PASS.

- [ ] **Step 5: Run all icc_hub integration tests**

```bash
python -m pytest tests/test_mcp_tools_integration.py -k "icc_hub" -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/emerge_daemon.py tests/test_mcp_tools_integration.py
git commit -m "fix: icc_hub configure imports existing hub pipelines after cloning"
```

---

## Task 5: Full test suite verification

**Files:** None modified — verification only.

- [ ] **Step 1: Run the complete test suite**

```bash
python -m pytest tests -q
```

Expected: all pass. If any pre-existing test fails, it was failing before — check `git stash` to confirm.

- [ ] **Step 2: Commit if clean**

If everything passes and there are no uncommitted changes:

```bash
git log --oneline -5
```

All four commits from Tasks 1–4 should be present. No further action needed.
