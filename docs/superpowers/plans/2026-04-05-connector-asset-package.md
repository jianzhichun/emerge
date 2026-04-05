# Connector Asset Package Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `connector-export` and `connector-import` sub-commands to `repl_admin.py` so a connector's pipeline files and registry entries can be packed into a zip and restored on another machine.

**Architecture:** Export copies `~/.emerge/connectors/<name>/` (excluding `__pycache__`) and the subset of `pipelines-registry.json` whose keys begin with `pipeline::<name>.` into a zip with a `manifest.json`. Import unpacks the zip into `_USER_CONNECTOR_ROOT` and merges the registry entries into the current session's registry, skipping or overwriting conflicts based on `--overwrite`.

**Tech Stack:** Python stdlib only (`zipfile`, `json`, `pathlib`). All logic goes in `scripts/repl_admin.py`. Tests go in `tests/test_repl_admin.py`.

---

### Task 1: Add `cmd_connector_export` with tests

**Files:**
- Modify: `scripts/repl_admin.py` (add `import zipfile` at top, add `cmd_connector_export`)
- Modify: `tests/test_repl_admin.py` (add two tests)

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_repl_admin.py`:

```python
def test_connector_export_produces_zip(tmp_path):
    """Export a connector directory into a zip with manifest, files, and registry."""
    connector_root = tmp_path / "connectors"
    connector_dir = connector_root / "mycon" / "pipelines" / "read"
    connector_dir.mkdir(parents=True)
    (connector_dir / "state.py").write_text("# state")
    (connector_dir / "state.yaml").write_text("pipeline: state")
    # __pycache__ should be excluded
    pycache = connector_dir / "__pycache__"
    pycache.mkdir()
    (pycache / "state.cpython-313.pyc").write_bytes(b"junk")

    state_root = tmp_path / "repl"
    state_root.mkdir()
    (state_root / "pipelines-registry.json").write_text(json.dumps({
        "pipelines": {
            "pipeline::mycon.read.state": {"status": "explore", "rollout_pct": 0},
            "pipeline::other.read.state": {"status": "stable", "rollout_pct": 100},
        }
    }))

    out_zip = tmp_path / "mycon-pkg.zip"
    result = repl_admin.cmd_connector_export(
        connector="mycon",
        out=str(out_zip),
        connector_root=connector_root,
        state_root=state_root,
    )

    assert result["ok"] is True
    assert out_zip.exists()

    import zipfile
    with zipfile.ZipFile(out_zip, "r") as zf:
        names = zf.namelist()
        assert "manifest.json" in names
        assert "pipelines-registry.json" in names
        assert "connectors/mycon/pipelines/read/state.py" in names
        assert "connectors/mycon/pipelines/read/state.yaml" in names
        # __pycache__ excluded
        assert not any("__pycache__" in n for n in names)
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["name"] == "mycon"
        reg = json.loads(zf.read("pipelines-registry.json"))
        assert "pipeline::mycon.read.state" in reg["pipelines"]
        assert "pipeline::other.read.state" not in reg["pipelines"]


def test_connector_export_missing_connector_returns_error(tmp_path):
    """Export returns error dict when connector directory does not exist."""
    connector_root = tmp_path / "connectors"
    connector_root.mkdir()
    state_root = tmp_path / "repl"
    state_root.mkdir()

    result = repl_admin.cmd_connector_export(
        connector="nonexistent",
        out=str(tmp_path / "pkg.zip"),
        connector_root=connector_root,
        state_root=state_root,
    )
    assert result["ok"] is False
    assert "nonexistent" in result["error"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_repl_admin.py::test_connector_export_produces_zip tests/test_repl_admin.py::test_connector_export_missing_connector_returns_error -q
```

Expected: FAIL — `cmd_connector_export` not defined.

- [ ] **Step 3: Add `import zipfile` at the top of `repl_admin.py`**

In `scripts/repl_admin.py`, insert `import zipfile` in the stdlib imports block (keep alphabetical order, after `tempfile`):

```python
import tempfile
import time
import zipfile
```

- [ ] **Step 4: Implement `_resolve_connector_root` helper and `cmd_connector_export` in `repl_admin.py`**

Add after `cmd_pipeline_set` (around line 220):

```python
def _resolve_connector_root() -> Path:
    """Return connector root: EMERGE_CONNECTOR_ROOT env var if set, else ~/.emerge/connectors."""
    from scripts.pipeline_engine import _USER_CONNECTOR_ROOT
    env_root = os.environ.get("EMERGE_CONNECTOR_ROOT", "").strip()
    if env_root:
        return Path(env_root).expanduser()
    return _USER_CONNECTOR_ROOT


def cmd_connector_export(
    *,
    connector: str,
    out: str,
    connector_root: Path | None = None,
    state_root: Path | None = None,
) -> dict:
    """Pack a connector directory and its registry entries into a zip file."""
    c_root = connector_root if connector_root is not None else _resolve_connector_root()
    connector_dir = c_root / connector
    if not connector_dir.exists():
        return {"ok": False, "error": f"connector not found: {connector_dir}"}

    s_root = state_root if state_root is not None else _resolve_state_root()
    _, registry_data = _load_registry(s_root)

    prefix = f"pipeline::{connector}."
    filtered = {
        k: v
        for k, v in registry_data.get("pipelines", {}).items()
        if k.startswith(prefix)
    }

    out_path = Path(out)
    manifest = {
        "name": connector,
        "emerge_version": _local_plugin_version(),
        "exported_at_ms": int(time.time() * 1000),
    }

    files = sorted(
        f for f in connector_dir.rglob("*")
        if f.is_file() and "__pycache__" not in f.parts
    )

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
        zf.writestr(
            "pipelines-registry.json",
            json.dumps({"pipelines": filtered}, indent=2, ensure_ascii=False),
        )
        for f in files:
            arcname = f"connectors/{connector}/{f.relative_to(connector_dir)}"
            zf.write(f, arcname)

    return {
        "ok": True,
        "connector": connector,
        "out": str(out_path),
        "pipeline_count": len(filtered),
        "file_count": len(files),
    }
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
python -m pytest tests/test_repl_admin.py::test_connector_export_produces_zip tests/test_repl_admin.py::test_connector_export_missing_connector_returns_error -q
```

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add scripts/repl_admin.py tests/test_repl_admin.py
git commit -m "feat: add cmd_connector_export to repl_admin"
```

---

### Task 2: Add `cmd_connector_import` with tests

**Files:**
- Modify: `scripts/repl_admin.py` (add `cmd_connector_import`)
- Modify: `tests/test_repl_admin.py` (add three tests)

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_repl_admin.py`:

```python
def _make_pkg(tmp_path: Path, connector: str = "mycon") -> Path:
    """Helper: build a valid connector zip package."""
    src_root = tmp_path / "src_connectors"
    connector_dir = src_root / connector / "pipelines" / "read"
    connector_dir.mkdir(parents=True)
    (connector_dir / "state.py").write_text("# state")
    (connector_dir / "state.yaml").write_text("pipeline: state")

    state_root = tmp_path / "src_repl"
    state_root.mkdir()
    (state_root / "pipelines-registry.json").write_text(json.dumps({
        "pipelines": {f"pipeline::{connector}.read.state": {"status": "explore", "rollout_pct": 0}}
    }))

    out_zip = tmp_path / f"{connector}-pkg.zip"
    repl_admin.cmd_connector_export(
        connector=connector,
        out=str(out_zip),
        connector_root=src_root,
        state_root=state_root,
    )
    return out_zip


def test_connector_import_extracts_files_and_merges_registry(tmp_path):
    """Import unpacks connector files and merges registry entries."""
    pkg = _make_pkg(tmp_path)

    dest_connector_root = tmp_path / "dest_connectors"
    dest_connector_root.mkdir()
    dest_state_root = tmp_path / "dest_repl"
    dest_state_root.mkdir()
    (dest_state_root / "pipelines-registry.json").write_text(json.dumps({"pipelines": {}}))

    result = repl_admin.cmd_connector_import(
        pkg=str(pkg),
        overwrite=False,
        connector_root=dest_connector_root,
        state_root=dest_state_root,
    )

    assert result["ok"] is True
    assert result["connector"] == "mycon"
    assert (dest_connector_root / "mycon" / "pipelines" / "read" / "state.py").exists()
    assert "pipeline::mycon.read.state" in result["pipelines_merged"]
    assert result["pipelines_skipped"] == []

    reg = json.loads((dest_state_root / "pipelines-registry.json").read_text())
    assert "pipeline::mycon.read.state" in reg["pipelines"]


def test_connector_import_conflict_no_overwrite_returns_error(tmp_path):
    """Import returns error when connector dir exists and --overwrite not set."""
    pkg = _make_pkg(tmp_path)

    dest_connector_root = tmp_path / "dest_connectors"
    existing = dest_connector_root / "mycon"
    existing.mkdir(parents=True)
    dest_state_root = tmp_path / "dest_repl"
    dest_state_root.mkdir()

    result = repl_admin.cmd_connector_import(
        pkg=str(pkg),
        overwrite=False,
        connector_root=dest_connector_root,
        state_root=dest_state_root,
    )

    assert result["ok"] is False
    assert "overwrite" in result["error"].lower() or "exists" in result["error"].lower()


def test_connector_import_overwrite_replaces_files_and_registry(tmp_path):
    """Import with overwrite=True replaces existing connector and registry entries."""
    pkg = _make_pkg(tmp_path)

    dest_connector_root = tmp_path / "dest_connectors"
    existing_file = dest_connector_root / "mycon" / "pipelines" / "read" / "state.py"
    existing_file.parent.mkdir(parents=True)
    existing_file.write_text("# old")

    dest_state_root = tmp_path / "dest_repl"
    dest_state_root.mkdir()
    (dest_state_root / "pipelines-registry.json").write_text(json.dumps({
        "pipelines": {"pipeline::mycon.read.state": {"status": "stable", "rollout_pct": 100}}
    }))

    result = repl_admin.cmd_connector_import(
        pkg=str(pkg),
        overwrite=True,
        connector_root=dest_connector_root,
        state_root=dest_state_root,
    )

    assert result["ok"] is True
    assert existing_file.read_text() == "# state"
    reg = json.loads((dest_state_root / "pipelines-registry.json").read_text())
    assert reg["pipelines"]["pipeline::mycon.read.state"]["status"] == "explore"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_repl_admin.py::test_connector_import_extracts_files_and_merges_registry tests/test_repl_admin.py::test_connector_import_conflict_no_overwrite_returns_error tests/test_repl_admin.py::test_connector_import_overwrite_replaces_files_and_registry -q
```

Expected: FAIL — `cmd_connector_import` not defined.

- [ ] **Step 3: Implement `cmd_connector_import` in `repl_admin.py`**

Add after `cmd_connector_export`:

```python
def cmd_connector_import(
    *,
    pkg: str,
    overwrite: bool = False,
    connector_root: Path | None = None,
    state_root: Path | None = None,
) -> dict:
    """Unpack a connector asset package and merge its registry entries."""
    pkg_path = Path(pkg)
    if not pkg_path.exists():
        return {"ok": False, "error": f"package not found: {pkg_path}"}

    with zipfile.ZipFile(pkg_path, "r") as zf:
        try:
            manifest = json.loads(zf.read("manifest.json"))
        except KeyError:
            return {"ok": False, "error": "invalid package: missing manifest.json"}

        connector = manifest.get("name", "")
        if not connector:
            return {"ok": False, "error": "invalid manifest: missing name"}

        c_root = connector_root if connector_root is not None else _resolve_connector_root()
        connector_dest = c_root / connector

        if connector_dest.exists() and not overwrite:
            return {
                "ok": False,
                "error": f"connector already exists: {connector_dest}. Use --overwrite to replace.",
            }

        try:
            imported_reg = json.loads(zf.read("pipelines-registry.json"))
        except KeyError:
            imported_reg = {"pipelines": {}}

        # Extract connector files
        arc_prefix = f"connectors/{connector}/"
        file_count = 0
        for item in zf.infolist():
            if not item.filename.startswith(arc_prefix):
                continue
            rel = item.filename[len(arc_prefix):]
            if not rel or rel.endswith("/"):
                continue
            dest = connector_dest / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(zf.read(item.filename))
            file_count += 1

    # Merge registry
    s_root = state_root if state_root is not None else _resolve_state_root()
    registry_path, existing = _load_registry(s_root)
    existing_pipelines = existing.get("pipelines", {})
    imported_pipelines = imported_reg.get("pipelines", {})

    merged: list[str] = []
    skipped: list[str] = []
    for k, v in imported_pipelines.items():
        if k in existing_pipelines and not overwrite:
            skipped.append(k)
        else:
            existing_pipelines[k] = v
            merged.append(k)

    existing["pipelines"] = existing_pipelines
    _save_registry(registry_path, existing)

    return {
        "ok": True,
        "connector": connector,
        "pkg": str(pkg_path),
        "file_count": file_count,
        "pipelines_merged": merged,
        "pipelines_skipped": skipped,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_repl_admin.py::test_connector_import_extracts_files_and_merges_registry tests/test_repl_admin.py::test_connector_import_conflict_no_overwrite_returns_error tests/test_repl_admin.py::test_connector_import_overwrite_replaces_files_and_registry -q
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/repl_admin.py tests/test_repl_admin.py
git commit -m "feat: add cmd_connector_import to repl_admin"
```

---

### Task 3: Wire commands into `main()` CLI

**Files:**
- Modify: `scripts/repl_admin.py` (`main()` only)
- Modify: `tests/test_repl_admin.py` (add two CLI smoke tests)

- [ ] **Step 1: Write the failing CLI tests**

Add to `tests/test_repl_admin.py`:

```python
def test_cli_connector_export(tmp_path):
    """connector-export sub-command produces a zip via CLI."""
    connector_root = tmp_path / "connectors"
    connector_dir = connector_root / "mycon" / "pipelines" / "read"
    connector_dir.mkdir(parents=True)
    (connector_dir / "state.py").write_text("# state")
    (connector_dir / "state.yaml").write_text("pipeline: state")

    state_root = tmp_path / "repl"
    state_root.mkdir()
    (state_root / "pipelines-registry.json").write_text(json.dumps({"pipelines": {}}))

    out_zip = tmp_path / "mycon-pkg.zip"
    env = {
        **os.environ,
        "EMERGE_CONNECTOR_ROOT": str(connector_root),
        "EMERGE_STATE_ROOT": str(state_root),
    }
    result = _run_admin(
        ["connector-export", "--connector", "mycon", "--out", str(out_zip)],
        env=env,
    )
    assert result["ok"] is True
    assert out_zip.exists()


def test_cli_connector_import(tmp_path):
    """connector-import sub-command extracts files via CLI."""
    # Build a package first
    src_connector_root = tmp_path / "src_connectors"
    connector_dir = src_connector_root / "mycon" / "pipelines" / "read"
    connector_dir.mkdir(parents=True)
    (connector_dir / "state.py").write_text("# state")
    (connector_dir / "state.yaml").write_text("pipeline: state")
    src_state_root = tmp_path / "src_repl"
    src_state_root.mkdir()
    (src_state_root / "pipelines-registry.json").write_text(json.dumps({
        "pipelines": {"pipeline::mycon.read.state": {"status": "explore", "rollout_pct": 0}}
    }))
    pkg = tmp_path / "mycon-pkg.zip"
    repl_admin.cmd_connector_export(
        connector="mycon",
        out=str(pkg),
        connector_root=src_connector_root,
        state_root=src_state_root,
    )

    dest_connector_root = tmp_path / "dest_connectors"
    dest_connector_root.mkdir()
    dest_state_root = tmp_path / "dest_repl"
    dest_state_root.mkdir()
    (dest_state_root / "pipelines-registry.json").write_text(json.dumps({"pipelines": {}}))

    env = {
        **os.environ,
        "EMERGE_CONNECTOR_ROOT": str(dest_connector_root),
        "EMERGE_STATE_ROOT": str(dest_state_root),
    }
    result = _run_admin(
        ["connector-import", "--pkg", str(pkg)],
        env=env,
    )
    assert result["ok"] is True
    assert (dest_connector_root / "mycon" / "pipelines" / "read" / "state.py").exists()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_repl_admin.py::test_cli_connector_export tests/test_repl_admin.py::test_cli_connector_import -q
```

Expected: FAIL — `connector-export` not a valid choice in argparse.

- [ ] **Step 3: Update `main()` in `repl_admin.py`**

In `main()`, add to the `choices` list:

```python
choices=[
    "status",
    "clear",
    "policy-status",
    "runner-status",
    "runner-config-status",
    "runner-config-set",
    "runner-config-unset",
    "runner-bootstrap",
    "runner-deploy",
    "pipeline-delete",
    "pipeline-set",
    "connector-export",   # new
    "connector-import",   # new
],
```

Add the new arguments after the existing `--set` argument (before `args = parser.parse_args()`):

```python
parser.add_argument("--connector", default="", help="Connector name for connector-export")
parser.add_argument("--out", default="", help="Output zip path for connector-export")
parser.add_argument("--pkg", default="", help="Package zip path for connector-import")
parser.add_argument("--overwrite", action="store_true", help="Overwrite existing connector/registry on import")
```

Add the new branches in the `if/elif` dispatch chain (before the final `else`):

```python
elif args.command == "connector-export":
    out = cmd_connector_export(
        connector=str(args.connector),
        out=str(args.out) if args.out else f"{args.connector}-emerge-pkg.zip",
    )
elif args.command == "connector-import":
    out = cmd_connector_import(
        pkg=str(args.pkg),
        overwrite=bool(args.overwrite),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_repl_admin.py::test_cli_connector_export tests/test_repl_admin.py::test_cli_connector_import -q
```

Expected: PASS

- [ ] **Step 5: Run full test suite to check for regressions**

```bash
python -m pytest tests -q
```

Expected: all tests pass (count matches or exceeds previous baseline).

- [ ] **Step 6: Commit**

```bash
git add scripts/repl_admin.py tests/test_repl_admin.py
git commit -m "feat: wire connector-export and connector-import into repl_admin CLI"
```
