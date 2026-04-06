# Audit Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix all issues found in the comprehensive code audit: 3 broken tests, hardcoded versions, O(N) metrics append, non-atomic crystallize writes, missing tmp_path reset, bad defaults, and stale docs.

**Architecture:** Fixes are grouped by concern. Categories A–C are code changes with tests; Category D is docs-only. Each task is independently committable. No new files needed — all changes are in-place.

**Tech Stack:** Python 3.11+, pytest, pathlib, atomic file writes via tempfile+os.replace

---

## File Map

| File | Change |
|---|---|
| `tests/test_plugin_static_config.py:44-58` | Fix `test_hooks_json_commands_use_claude_plugin_root` — access nested `entry["hooks"][0]["command"]` |
| `.mcp.json` | Change absolute path to relative `scripts/emerge_daemon.py` |
| `tests/test_repl_admin.py:438` | Update mock version `"0.2.0"` → current `"0.2.2"` |
| `scripts/emerge_daemon.py:517` | Read version from `.claude-plugin/plugin.json` at init, not hardcoded |
| `scripts/emerge_daemon.py:62-85` | Add `self._version` attribute read from plugin.json |
| `scripts/metrics.py:20-36` | Replace read-entire-file-then-write with direct append |
| `scripts/exec_session.py:242,285` | Add `tmp_path = ""` after `os.replace` in both methods |
| `scripts/emerge_daemon.py:244-255` | Make `_crystallize` py+yaml writes atomic |
| `scripts/emerge_daemon.py:942-944` | Remove test-fixture defaults in `_record_pipeline_event` |
| `README.md` | Fix `EMERGE_MONITOR_MACHINES` default, add 7 missing env vars, update test badge |
| `CLAUDE.md` | Remove `icc_crystallize mode=adapter` references |

---

## Task 1: Fix test — `test_hooks_json_commands_use_claude_plugin_root`

**Files:**
- Modify: `tests/test_plugin_static_config.py:44-58`

The test does `entry["command"]` but `hooks.json` structure is `{ "matcher": "...", "hooks": [{ "type": "command", "command": "..." }] }`.

- [ ] **Step 1: Verify the test currently fails**

```bash
python -m pytest tests/test_plugin_static_config.py::test_hooks_json_commands_use_claude_plugin_root -q
```
Expected: FAILED with `KeyError: 'command'`

- [ ] **Step 2: Fix the test to access nested structure**

In `tests/test_plugin_static_config.py`, replace lines 52-58:

```python
    for event, matchers in data["hooks"].items():
        for entry in matchers:
            cmd = entry["command"]
            assert "${CLAUDE_PLUGIN_ROOT}" in cmd, (
                f"Hook {event!r} command {cmd!r} must use ${{CLAUDE_PLUGIN_ROOT}} "
                "to work when CC is run from outside the plugin directory"
            )
```

with:

```python
    for event, matchers in data["hooks"].items():
        for entry in matchers:
            for hook in entry.get("hooks", []):
                cmd = hook.get("command", "")
                if not cmd:
                    continue
                assert "${CLAUDE_PLUGIN_ROOT}" in cmd, (
                    f"Hook {event!r} command {cmd!r} must use ${{CLAUDE_PLUGIN_ROOT}} "
                    "to work when CC is run from outside the plugin directory"
                )
```

- [ ] **Step 3: Run the test to verify it passes**

```bash
python -m pytest tests/test_plugin_static_config.py::test_hooks_json_commands_use_claude_plugin_root -q
```
Expected: PASSED

- [ ] **Step 4: Commit**

```bash
git add tests/test_plugin_static_config.py
git commit -m "fix(test): access nested hooks[].command in hooks.json structure check"
```

---

## Task 2: Fix test — `test_mcp_config_has_core_stdio_and_expected_tools_path`

**Files:**
- Modify: `.mcp.json`

The test checks `"scripts/emerge_daemon.py" in server["args"]` (list membership, not substring). The `.mcp.json` has an absolute path so the exact-match check fails. Fix: use a relative path in `.mcp.json`.

- [ ] **Step 1: Verify the test currently fails**

```bash
python -m pytest tests/test_plugin_static_config.py::test_mcp_config_has_core_stdio_and_expected_tools_path -q
```
Expected: FAILED with `AssertionError`

- [ ] **Step 2: Update `.mcp.json` to use relative path**

Replace the entire `.mcp.json` content:

```json
{
  "mcpServers": {
    "core": {
      "type": "stdio",
      "command": "python3",
      "args": ["scripts/emerge_daemon.py"],
      "env": {}
    }
  }
}
```

- [ ] **Step 3: Run the test to verify it passes**

```bash
python -m pytest tests/test_plugin_static_config.py::test_mcp_config_has_core_stdio_and_expected_tools_path -q
```
Expected: PASSED

- [ ] **Step 4: Commit**

```bash
git add .mcp.json
git commit -m "fix: use relative path in .mcp.json so test and cross-machine use works"
```

---

## Task 3: Fix test — `test_runner_bootstrap_reuses_existing_healthy_runner`

**Files:**
- Modify: `tests/test_repl_admin.py:438`

The mock returns `"0.2.0"` but `_local_plugin_version()` reads the real `.claude-plugin/plugin.json` which is `"0.2.2"`. Version mismatch triggers the re-deploy guard. Fix: mock must return current version.

- [ ] **Step 1: Verify the test currently fails**

```bash
python -m pytest tests/test_repl_admin.py::test_runner_bootstrap_reuses_existing_healthy_runner -q
```
Expected: FAILED (version mismatch triggers re-deploy instead of reuse)

- [ ] **Step 2: Read the current version from plugin.json and update mock**

Check current version:
```bash
python3 -c "import json; print(json.load(open('.claude-plugin/plugin.json'))['version'])"
```

Then in `tests/test_repl_admin.py` at line 438, change:

```python
            return json.dumps({"name": "emerge", "version": "0.2.0"})
```

to:

```python
            return json.dumps({"name": "emerge", "version": "0.2.2"})
```

- [ ] **Step 3: Run the test**

```bash
python -m pytest tests/test_repl_admin.py::test_runner_bootstrap_reuses_existing_healthy_runner -q
```
Expected: PASSED

- [ ] **Step 4: Make mock version-independent (best solution)**

Rather than hardcoding a version in the mock, import `repl_admin._local_plugin_version` and use the live value. Replace the mock function body:

```python
    def fake_run_checked(command: list[str], *, timeout_s: int = 90) -> str:
        if command and command[0] == "ssh" and "cat .claude-plugin/plugin.json" in command[-1]:
            return json.dumps({"name": "emerge", "version": repl_admin._local_plugin_version()})
        return ""
```

- [ ] **Step 5: Run the test again**

```bash
python -m pytest tests/test_repl_admin.py::test_runner_bootstrap_reuses_existing_healthy_runner -q
```
Expected: PASSED (and will stay passing across future version bumps)

- [ ] **Step 6: Run full test suite to confirm no regressions**

```bash
python -m pytest tests -q
```
Expected: all previously passing tests still pass, 3 failures become 0.

- [ ] **Step 7: Commit**

```bash
git add tests/test_repl_admin.py
git commit -m "fix(test): use live _local_plugin_version() in runner bootstrap reuse mock"
```

---

## Task 4: Dynamic `serverInfo.version` in MCP initialize response

**Files:**
- Modify: `scripts/emerge_daemon.py:62-85` (init), `scripts/emerge_daemon.py:517`

The user confirmed: hardcoded `"0.2.0"` in `serverInfo` should read from `.claude-plugin/plugin.json` dynamically — same approach `repl_admin._local_plugin_version()` already uses.

- [ ] **Step 1: Write a failing test**

Add to `tests/test_mcp_tools_integration.py`:

```python
def test_server_info_version_matches_plugin_json():
    import json
    from pathlib import Path
    daemon = EmergeDaemon()
    req = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    }}
    resp = daemon._handle_request(req)
    reported = resp["result"]["serverInfo"]["version"]
    plugin_version = json.loads(
        (Path(__file__).resolve().parents[1] / ".claude-plugin" / "plugin.json").read_text()
    )["version"]
    assert reported == plugin_version, f"serverInfo.version={reported!r} != plugin.json version={plugin_version!r}"
```

- [ ] **Step 2: Run it to verify it fails**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_server_info_version_matches_plugin_json -q
```
Expected: FAILED (`"0.2.0" != "0.2.2"`)

- [ ] **Step 3: Add version loading to `EmergeDaemon.__init__`**

In `scripts/emerge_daemon.py`, inside `EmergeDaemon.__init__` (after line 84, before `self._operator_monitor`), add:

```python
        try:
            _plugin_manifest = resolved_root / ".claude-plugin" / "plugin.json"
            self._version = json.loads(_plugin_manifest.read_text(encoding="utf-8")).get("version", "0.0.0")
        except Exception:
            self._version = "0.0.0"
```

- [ ] **Step 4: Replace hardcoded version in `_handle_request`**

In `scripts/emerge_daemon.py` line 517, change:

```python
                    "serverInfo": {"name": "emerge", "version": "0.2.0"},
```

to:

```python
                    "serverInfo": {"name": "emerge", "version": self._version},
```

- [ ] **Step 5: Run the test**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_server_info_version_matches_plugin_json -q
```
Expected: PASSED

- [ ] **Step 6: Commit**

```bash
git add scripts/emerge_daemon.py tests/test_mcp_tools_integration.py
git commit -m "feat: load serverInfo.version dynamically from .claude-plugin/plugin.json"
```

---

## Task 5: Fix `LocalJSONLSink.emit` O(N) — use append

**Files:**
- Modify: `scripts/metrics.py:20-36`

Current implementation reads entire file, rewrites via temp file. Safe but O(N). For JSONL, atomic append is unnecessary — the format is line-delimited so a partial write of a new line at the end is the only failure mode, and recovery is trivial (skip incomplete last line). Use `open(path, "a")` instead.

- [ ] **Step 1: Write a failing test capturing the performance contract**

Add to `tests/test_mcp_tools_integration.py` (or a new file `tests/test_metrics.py`):

```python
def test_metrics_emit_appends_not_rewrites(tmp_path):
    from scripts.metrics import LocalJSONLSink
    path = tmp_path / "metrics.jsonl"
    sink = LocalJSONLSink(path=path)
    sink.emit("event_a", {"k": "v1"})
    sink.emit("event_b", {"k": "v2"})
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 2
    import json
    assert json.loads(lines[0])["event_type"] == "event_a"
    assert json.loads(lines[1])["event_type"] == "event_b"
```

- [ ] **Step 2: Verify the test passes already (it should — behavior is the same)**

```bash
python -m pytest tests/test_metrics.py::test_metrics_emit_appends_not_rewrites -q
```
Expected: PASSED (the behavior is correct, we are changing the implementation, not the contract)

- [ ] **Step 3: Replace `emit` with append implementation**

In `scripts/metrics.py`, replace the entire `emit` method (lines 20-36):

```python
    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        event = {"ts_ms": int(time.time() * 1000), "event_type": event_type, **payload}
        line = json.dumps(event, ensure_ascii=True) + "\n"
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(line)
```

Remove the now-unused imports: `import tempfile` and `import os` if they are only used here. Check first — `os` is used in `get_sink` indirectly via Path, so only remove `tempfile` if unused elsewhere in the file.

- [ ] **Step 4: Run the test again to verify behavior preserved**

```bash
python -m pytest tests/test_metrics.py -q
```
Expected: PASSED

- [ ] **Step 5: Commit**

```bash
git add scripts/metrics.py tests/test_metrics.py
git commit -m "perf: replace O(N) metrics emit with direct append"
```

---

## Task 6: Fix `exec_session.py` missing `tmp_path = ""` reset

**Files:**
- Modify: `scripts/exec_session.py:242`, `scripts/exec_session.py:285`

After `os.replace(tmp_path, dest)`, the `tmp_path` variable still holds the old value. If something raises in the same `try` block after the replace, the `finally` block does `os.unlink(tmp_path)` — but `tmp_path` no longer exists at that path (it was the source of the rename). On POSIX this is harmless since `os.path.exists` returns False, but it's inconsistent with the pattern used in `emerge_daemon.py`.

- [ ] **Step 1: Fix `_write_checkpoint` in `exec_session.py`**

At line 242-246, change:

```python
            os.replace(tmp_path, self._checkpoint_path)
            self._wal_seq_applied = wal_seq_applied
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
```

to:

```python
            os.replace(tmp_path, self._checkpoint_path)
            tmp_path = ""
            self._wal_seq_applied = wal_seq_applied
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
```

- [ ] **Step 2: Fix `_write_recovery_status` in `exec_session.py`**

At line 285-288, change:

```python
            os.replace(tmp_path, self._recovery_path)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
```

to:

```python
            os.replace(tmp_path, self._recovery_path)
            tmp_path = ""
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
```

- [ ] **Step 3: Run tests**

```bash
python -m pytest tests -q -x
```
Expected: all passing (no behavior change, only defensive correctness)

- [ ] **Step 4: Commit**

```bash
git add scripts/exec_session.py
git commit -m "fix: zero tmp_path after os.replace to prevent spurious unlink in finally"
```

---

## Task 7: Make `_crystallize` pipeline file writes atomic

**Files:**
- Modify: `scripts/emerge_daemon.py:244-255`

Currently `py_path.write_text(...)` and `yaml_path.write_text(...)` are direct writes. If the process is interrupted between the two, the connector root is in an inconsistent state (one file written, one missing/old). Fix: write each via temp file + `os.replace`.

- [ ] **Step 1: Write a test to verify both files are written together**

Add to `tests/test_mcp_tools_integration.py`:

```python
def test_crystallize_writes_both_files_or_neither(tmp_path, monkeypatch):
    """Both .py and .yaml must be present after a successful crystallize."""
    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(tmp_path))
    # Setup: write a WAL entry that can be crystallized
    import os, time
    os.makedirs(tmp_path / "mock" / "pipelines" / "read", exist_ok=True)
    daemon = EmergeDaemon()
    # Prime WAL with a successful exec entry
    session_dir = daemon._state_root / daemon._base_session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    wal_path = session_dir / "wal.jsonl"
    import json as _json
    entry = {"seq": 1, "status": "success", "code": "__result = [{'x': 1}]",
             "metadata": {"intent_signature": "test_crystallize_atomic"}}
    wal_path.write_text(_json.dumps(entry) + "\n", encoding="utf-8")
    result = daemon.call_tool("icc_crystallize", {
        "connector": "mock", "pipeline": "test_pipe",
        "intent_signature": "test_crystallize_atomic", "mode": "read",
    })
    assert not result.get("isError"), result
    py_path = tmp_path / "mock" / "pipelines" / "read" / "test_pipe.py"
    yaml_path = tmp_path / "mock" / "pipelines" / "read" / "test_pipe.yaml"
    assert py_path.exists()
    assert yaml_path.exists()
```

- [ ] **Step 2: Run to verify it passes (confirms current write works)**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_crystallize_writes_both_files_or_neither -q
```
Expected: PASSED

- [ ] **Step 3: Replace direct writes with atomic writes in `_crystallize`**

In `scripts/emerge_daemon.py`, replace lines 244-255:

```python
        py_path.write_text(py_src, encoding="utf-8")
        yaml_path.write_text(yaml_src, encoding="utf-8")
```

with:

```python
        import tempfile as _tempfile
        for dest_path, content in ((py_path, py_src), (yaml_path, yaml_src)):
            fd, tmp = _tempfile.mkstemp(prefix=".crystallize-", dir=str(pipeline_dir))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, dest_path)
                tmp = ""
            finally:
                if tmp and os.path.exists(tmp):
                    os.unlink(tmp)
```

- [ ] **Step 4: Run the test again**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_crystallize_writes_both_files_or_neither -q
```
Expected: PASSED

- [ ] **Step 5: Commit**

```bash
git add scripts/emerge_daemon.py tests/test_mcp_tools_integration.py
git commit -m "fix: make _crystallize py+yaml writes atomic via temp file + os.replace"
```

---

## Task 8: Fix test-fixture defaults in `_record_pipeline_event`

**Files:**
- Modify: `scripts/emerge_daemon.py:942-944`

`"mock"`, `"layers"`, `"add-wall"` are test connector names — if `connector` or `pipeline` is unexpectedly missing in production, these defaults silently produce wrong `pipeline_id`. Use `""` as defaults so the fallback `pipeline_id` computation (`connector.mode.pipeline`) makes the gap visible.

- [ ] **Step 1: Change defaults**

In `scripts/emerge_daemon.py` lines 942-944, change:

```python
        connector = str(arguments.get("connector", "mock"))
        mode = "read" if tool_name == "icc_read" else "write"
        pipeline = str(arguments.get("pipeline", "layers" if mode == "read" else "add-wall"))
```

to:

```python
        connector = str(arguments.get("connector", ""))
        mode = "read" if tool_name == "icc_read" else "write"
        pipeline = str(arguments.get("pipeline", ""))
```

- [ ] **Step 2: Run full tests to confirm no breakage**

```bash
python -m pytest tests -q
```
Expected: all passing (integration tests always pass `connector` and `pipeline` explicitly)

- [ ] **Step 3: Commit**

```bash
git add scripts/emerge_daemon.py
git commit -m "fix: remove test-fixture connector/pipeline defaults from _record_pipeline_event"
```

---

## Task 9: Docs — fix README env var table and stale content

**Files:**
- Modify: `README.md`

Three docs issues: wrong default for `EMERGE_MONITOR_MACHINES`, 7 missing env vars, stale test count badge.

- [ ] **Step 1: Fix `EMERGE_MONITOR_MACHINES` default**

Find the line:
```
| `EMERGE_MONITOR_MACHINES` | Comma-separated runner profile names to monitor | all configured |
```
Change to:
```
| `EMERGE_MONITOR_MACHINES` | Comma-separated runner profile names to monitor | `default` |
```

- [ ] **Step 2: Add 7 missing env vars to the configuration table**

After the existing env var rows, add:

```markdown
| `EMERGE_STATE_ROOT`         | Override where session state (WAL, checkpoints, registry) is written | `~/.emerge/sessions` |
| `EMERGE_SESSION_ID`         | Override the derived session identifier                               | derived from cwd+git  |
| `EMERGE_RUNNER_CONFIG_PATH` | Path to `runner-map.json` (overrides default location)               | `~/.emerge/runner-map.json` |
| `EMERGE_SETTINGS_PATH`      | Override the settings file path                                       | `~/.emerge/settings.json` |
| `EMERGE_SCRIPT_ROOTS`       | Comma-separated allowed roots for `script_ref` resolution             | project root |
| `EMERGE_TARGET_PROFILE`     | Default runner target profile for `repl_admin` commands              | `default` |
| `CLAUDE_PLUGIN_DATA`        | Hook-state root used by `icc_reconcile` and hooks                    | `~/.claude/plugin-data` |
```

- [ ] **Step 3: Update test badge count**

Find:
```
![Tests](https://img.shields.io/badge/tests-192%20passing-brightgreen)
```
Update to reflect actual count after all fixes pass. Run the suite first:
```bash
python -m pytest tests -q 2>&1 | tail -3
```
Then update the number in the badge URL (e.g., `202%20passing` if 202 pass).

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: fix EMERGE_MONITOR_MACHINES default, add 7 missing env vars, update test badge"
```

---

## Task 10: Docs — remove `icc_crystallize mode=adapter` ghost references

**Files:**
- Modify: `CLAUDE.md`
- Modify: `skills/writing-vertical-adapter/SKILL.md` (if it references this)

The `icc_crystallize` handler explicitly rejects `mode=adapter`. The feature is not implemented. Remove the documentation references to avoid misleading users and operators.

- [ ] **Step 1: Check all references**

```bash
grep -rn "mode=adapter\|mode.*adapter" CLAUDE.md skills/ README.md
```

- [ ] **Step 2: Remove from CLAUDE.md**

Find and remove/update the line in the `ObserverPlugin` / `AdapterRegistry` section:
```
`ObserverPlugin` (`scripts/observer_plugin.py`) is the ABC for all operator observation. `AdapterRegistry` loads built-in observers (`scripts/observers/`) and crystallized vertical adapters from `~/.emerge/adapters/<vertical>/adapter.py`. Vertical adapters are built via `icc_crystallize mode=adapter` (not shipped, crystallized per-user).
```

Change to remove `icc_crystallize mode=adapter` reference since it's not implemented (adapters are crystallized manually):
```
`ObserverPlugin` (`scripts/observer_plugin.py`) is the ABC for all operator observation. `AdapterRegistry` loads built-in observers (`scripts/observers/`) and user-authored vertical adapters from `~/.emerge/adapters/<vertical>/adapter.py`.
```

- [ ] **Step 3: Update skills/writing-vertical-adapter/SKILL.md if it references mode=adapter**

```bash
grep -n "mode=adapter" skills/writing-vertical-adapter/SKILL.md
```
If found, remove the reference or replace with the actual crystallization workflow.

- [ ] **Step 4: Run tests to confirm nothing broken**

```bash
python -m pytest tests -q
```

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md skills/
git commit -m "docs: remove unimplemented icc_crystallize mode=adapter references"
```

---

## Final Verification

- [ ] **Run the complete test suite**

```bash
python -m pytest tests -q
```
Expected: 0 failures, count matches README badge.

- [ ] **Confirm the 3 originally-failing tests now pass**

```bash
python -m pytest tests/test_plugin_static_config.py tests/test_repl_admin.py::test_runner_bootstrap_reuses_existing_healthy_runner -v
```
Expected: all PASSED.
