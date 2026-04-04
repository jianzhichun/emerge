# Muscle Memory Flywheel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transform the emerge plugin from a "REPL + pipelines" system into a self-improving muscle memory flywheel: structured error recovery, automatic pipeline crystallization from exec history, and real human-fix tracking.

**Architecture:** Four sequential work streams — (1) naming unification removes legacy "REPL"/"L1.5" framing, (2) recovery adds structured errors so Claude can self-correct, (3) auto-synthesis adds `icc_crystallize` to turn exec history into pipeline files, (4) human-fix tracking wires `icc_reconcile` to the promotion gate. Each stream leaves all tests green before the next begins.

**Tech Stack:** Python 3.11+, standard library only (no new deps). pytest for tests. YAML via PyYAML (already available).

---

## File Map

| File | Status | Role after this plan |
|---|---|---|
| `scripts/exec_session.py` | rename from `repl_state.py` | Persistent exec session: WAL, checkpoint, structured error parsing |
| `scripts/emerge_daemon.py` | rename from `repl_daemon.py` | MCP daemon: all four tool paths, flywheel bridge, crystallization |
| `scripts/pipeline_engine.py` | modify | Add `PipelineMissingError`; raise it from `_load_pipeline()` |
| `scripts/policy_config.py` | modify | Add `default_exec_root()`, keep `default_repl_root()` as alias |
| `scripts/remote_runner.py` | modify | Update imports + internal names after renames |
| `scripts/repl_admin.py` | modify | Update `_DEV_ONLY` set after file rename |
| `scripts/state_tracker.py` | modify | `schema_version: "l15.v1"` → `"flywheel.v1"` |
| `.claude-plugin/plugin.json` | modify | Update daemon path + description |
| `tests/test_repl_daemon_exec.py` | modify | Import from `emerge_daemon` |
| `tests/test_mcp_tools_integration.py` | modify | Import from `emerge_daemon`; assert `bridge_promoted` |
| `tests/test_exec_flywheel.py` | modify | Import from `emerge_daemon` |
| `tests/test_metrics.py` | modify | Import from `emerge_daemon` |
| `tests/test_repl_admin.py` | modify | Import from `emerge_daemon` |
| `tests/test_hook_scripts_output.py` | modify | Assert `schema_version == "flywheel.v1"` |
| `skills/muscle-memory-flywheel/SKILL.md` | create | Exec conventions, crystallize trigger, reconcile usage |

---

## Task 1: Add `default_exec_root()` and env-var aliases in `policy_config.py`

**Files:**
- Modify: `scripts/policy_config.py`

The rest of the codebase reads `REPL_STATE_ROOT` and `REPL_SESSION_ID`. After this task, code will check the new `EMERGE_STATE_ROOT` / `EMERGE_SESSION_ID` names first and fall back to the old names. Existing tests and configs keep working unchanged.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_settings.py` (it already exists and tests policy_config):

```python
def test_default_exec_root_matches_repl_root():
    from scripts.policy_config import default_exec_root, default_repl_root
    assert default_exec_root() == default_repl_root()


def test_default_exec_root_path():
    from scripts.policy_config import default_exec_root
    from pathlib import Path
    assert default_exec_root() == Path.home() / ".emerge" / "repl"
```

- [ ] **Step 2: Run to confirm fail**

```bash
python -m pytest tests/test_settings.py::test_default_exec_root_matches_repl_root tests/test_settings.py::test_default_exec_root_path -v
```
Expected: `AttributeError: module 'scripts.policy_config' has no attribute 'default_exec_root'`

- [ ] **Step 3: Add `default_exec_root()` to `policy_config.py`**

In `scripts/policy_config.py`, replace the existing `default_repl_root` function:

```python
def default_exec_root() -> Path:
    """Return the default execution-session state root (``~/.emerge/repl``).

    The on-disk directory name stays ``repl`` for data-compatibility with
    existing installations.
    """
    return default_emerge_home() / "repl"


def default_repl_root() -> Path:
    """Backward-compat alias for default_exec_root()."""
    return default_exec_root()
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/test_settings.py -v
```
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/policy_config.py tests/test_settings.py
git commit -m "refactor: add default_exec_root(), keep default_repl_root() as alias"
```

---

## Task 2: Rename `repl_state.py` → `exec_session.py`, `ReplState` → `ExecSession`

**Files:**
- Create: `scripts/exec_session.py`
- Delete: `scripts/repl_state.py` (content moves to exec_session.py)
- Modify: `scripts/remote_runner.py`

`repl_state.py` contains only `class ReplState`. We copy it to `exec_session.py` with the class renamed to `ExecSession`, then replace `repl_state.py` with a one-line compatibility shim so no imports break while we update them.

- [ ] **Step 1: Create `scripts/exec_session.py`**

Copy the entire content of `scripts/repl_state.py` to `scripts/exec_session.py`, then make two changes:

1. Update the import at the top:
```python
from scripts.policy_config import default_exec_root
```
(was `default_repl_root`)

2. Rename the class and its internal reference:
```python
class ExecSession:
    """Persistent Python execution state for icc_exec."""

    def __init__(self, state_root: Path | None = None, session_id: str = "default") -> None:
        self._globals: dict[str, Any] = {"__builtins__": __builtins__}
        base = state_root or default_exec_root()
        # ... rest of __init__ unchanged ...
```

Everything else in the file stays identical.

- [ ] **Step 2: Replace `repl_state.py` with a compatibility shim**

```python
# backward-compat shim — use exec_session.ExecSession going forward
from scripts.exec_session import ExecSession as ReplState  # noqa: F401
```

- [ ] **Step 3: Update `remote_runner.py` imports and usages**

In `scripts/remote_runner.py`:

Change:
```python
from scripts.repl_state import ReplState
```
To:
```python
from scripts.exec_session import ExecSession
```

Change every `ReplState(` to `ExecSession(` and every `: ReplState` to `: ExecSession` and every `dict[str, ReplState]` to `dict[str, ExecSession]`.

Specifically:
- Line `self._repl_by_profile: dict[str, ReplState] = {}` → `dict[str, ExecSession]`
- Line `self._repl_by_profile[profile_key] = ReplState(` → `ExecSession(`

- [ ] **Step 4: Run tests**

```bash
python -m pytest -q
```
Expected: all 117 pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/exec_session.py scripts/repl_state.py scripts/remote_runner.py
git commit -m "refactor: rename ReplState → ExecSession, repl_state.py → exec_session.py"
```

---

## Task 3: Rename `repl_daemon.py` → `emerge_daemon.py`, `ReplDaemon` → `EmergeDaemon`

**Files:**
- Create: `scripts/emerge_daemon.py`
- Delete content of: `scripts/repl_daemon.py` (replace with shim)
- Modify: `.claude-plugin/plugin.json`
- Modify: `scripts/repl_admin.py`
- Modify: `tests/test_repl_daemon_exec.py`
- Modify: `tests/test_mcp_tools_integration.py`
- Modify: `tests/test_exec_flywheel.py`
- Modify: `tests/test_metrics.py`
- Modify: `tests/test_repl_admin.py`

- [ ] **Step 1: Create `scripts/emerge_daemon.py`**

Copy entire content of `scripts/repl_daemon.py` to `scripts/emerge_daemon.py`, then make these changes:

1. Update imports at the top:
```python
from scripts.exec_session import ExecSession   # was: from scripts.repl_state import ReplState
from scripts.policy_config import (
    ...
    default_exec_root,   # was: default_repl_root
    ...
)
```

2. Rename the class:
```python
class EmergeDaemon:   # was: ReplDaemon
```

3. Update env var reading inside `__init__` (add new-name fallback):
```python
state_root = Path(
    os.environ.get("EMERGE_STATE_ROOT")
    or os.environ.get("REPL_STATE_ROOT")
    or str(default_exec_root())
).expanduser().resolve()
self._base_session_id = derive_session_id(
    os.environ.get("EMERGE_SESSION_ID") or os.environ.get("REPL_SESSION_ID"),
    resolved_root,
)
```

4. Change type annotation:
```python
self._repl_by_profile: dict[str, ExecSession] = {}   # was ReplState
```

5. Update `_get_repl()` internal usages of `ReplState(` → `ExecSession(`.

6. Update `run_stdio()` at the bottom:
```python
def run_stdio() -> None:
    daemon = EmergeDaemon()   # was ReplDaemon()
```

- [ ] **Step 2: Replace `repl_daemon.py` with a compatibility shim**

```python
# backward-compat shim — use emerge_daemon.EmergeDaemon going forward
from scripts.emerge_daemon import EmergeDaemon as ReplDaemon  # noqa: F401
from scripts.emerge_daemon import run_stdio  # noqa: F401

if __name__ == "__main__":
    run_stdio()
```

The `if __name__ == "__main__"` guard means `python repl_daemon.py` still works — so plugin.json can be updated at our own pace. But we update it now.

- [ ] **Step 3: Update `plugin.json`**

In `.claude-plugin/plugin.json`:

```json
{
  "name": "emerge",
  "version": "0.2.0",
  "description": "Emerge muscle memory flywheel for Claude Code",
  "mcpServers": {
    "emerge": {
      "command": "python3",
      "args": ["${CLAUDE_PLUGIN_ROOT}/scripts/emerge_daemon.py"]
    }
  },
  "permissions": {
    "filesystem": ["~/.emerge/"],
    "network": ["localhost", "192.168.122.0/24"]
  }
}
```

- [ ] **Step 4: Update `repl_admin.py` `_DEV_ONLY` set**

In `scripts/repl_admin.py`, change:
```python
_DEV_ONLY = {"repl_admin.py", "repl_daemon.py"}
```
To:
```python
_DEV_ONLY = {"repl_admin.py", "emerge_daemon.py"}
```

- [ ] **Step 5: Update all test imports**

In each of these five files, change:
```python
from scripts.repl_daemon import ReplDaemon
```
To:
```python
from scripts.emerge_daemon import EmergeDaemon as ReplDaemon
```

Files to update:
- `tests/test_repl_daemon_exec.py`
- `tests/test_mcp_tools_integration.py`
- `tests/test_exec_flywheel.py`
- `tests/test_repl_admin.py`

For `tests/test_metrics.py`, the import is inside a test function body — update that occurrence too:
```python
from scripts.emerge_daemon import EmergeDaemon as ReplDaemon
```

- [ ] **Step 6: Run tests**

```bash
python -m pytest -q
```
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add scripts/emerge_daemon.py scripts/repl_daemon.py .claude-plugin/plugin.json \
        scripts/repl_admin.py tests/test_repl_daemon_exec.py \
        tests/test_mcp_tools_integration.py tests/test_exec_flywheel.py \
        tests/test_metrics.py tests/test_repl_admin.py
git commit -m "refactor: rename ReplDaemon → EmergeDaemon, repl_daemon.py → emerge_daemon.py"
```

---

## Task 4: Remaining symbol renames (L1.5 → bridge, _get_repl → _get_session, schema_version)

**Files:**
- Modify: `scripts/emerge_daemon.py`
- Modify: `scripts/remote_runner.py`
- Modify: `scripts/state_tracker.py`
- Modify: `tests/test_mcp_tools_integration.py`
- Modify: `tests/test_hook_scripts_output.py`

These are pure symbol renames with no behaviour change. The `l15::` JSON key prefix on disk is intentionally left unchanged (data compatibility).

- [ ] **Step 1: Rename internal symbols in `emerge_daemon.py`**

Apply these renames throughout `scripts/emerge_daemon.py`:

| Old symbol | New symbol |
|---|---|
| `_try_l15_promote` | `_try_flywheel_bridge` |
| `_l15_candidate_key` | `_bridge_candidate_key` |
| `l15_promoted` (result dict field set in `_try_flywheel_bridge`) | `bridge_promoted` |
| `self._sink.emit("l15.promoted", ...)` | `self._sink.emit("flywheel.bridge.promoted", ...)` |
| `_repl_by_profile` | `_sessions_by_profile` |
| `_get_repl` | `_get_session` |

The `l15::` string **prefix inside `_bridge_candidate_key()`** stays unchanged:
```python
@staticmethod
def _bridge_candidate_key(pipeline_id: str, intent_signature: str, script_ref: str) -> str:
    return f"l15::{pipeline_id}::{intent_signature}::{script_ref}"   # disk key — do not change
```

Also update the call sites: anywhere `_try_l15_promote`, `_l15_candidate_key`, `_get_repl`, `_repl_by_profile` appear as references inside the class, update to the new names.

The comment on line `# L1.5 promotion:` → `# flywheel bridge:`.

The `tools/list` `base_pipeline_id` description: update `"Pipeline id for L1.5 promotion routing"` → `"Pipeline id for flywheel bridge routing"`.

The `"source": "l15_composed"` value in `_record_pipeline_event` stays — it's stored in `candidates.json` on disk.

- [ ] **Step 2: Rename `_get_repl` → `_get_session` in `remote_runner.py`**

In `scripts/remote_runner.py`:
- `self._repl_by_profile` → `self._sessions_by_profile`
- `def _get_repl(` → `def _get_session(`
- All call sites `self._get_repl(` → `self._get_session(`

- [ ] **Step 3: Update `state_tracker.py` schema version**

In `scripts/state_tracker.py`, find the line with `"schema_version": "l15.v1"` and change to:
```python
"schema_version": "flywheel.v1",
```

- [ ] **Step 4: Update tests that assert on renamed symbols**

In `tests/test_mcp_tools_integration.py`, change:
```python
assert body.get("l15_promoted") is True
```
→
```python
assert body.get("bridge_promoted") is True
```

And:
```python
assert body.get("l15_promoted") is not True
```
→
```python
assert body.get("bridge_promoted") is not True
```

In `tests/test_hook_scripts_output.py`, change every:
```python
assert token["schema_version"] == "l15.v1"
```
→
```python
assert token["schema_version"] == "flywheel.v1"
```

- [ ] **Step 5: Run tests**

```bash
python -m pytest -q
```
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/emerge_daemon.py scripts/remote_runner.py scripts/state_tracker.py \
        tests/test_mcp_tools_integration.py tests/test_hook_scripts_output.py
git commit -m "refactor: rename L1.5 → flywheel bridge, _get_repl → _get_session, schema_version → flywheel.v1"
```

---

## Task 5: Structured error fields from `icc_exec` failures

**Files:**
- Modify: `scripts/exec_session.py`
- Modify: `tests/test_repl_daemon_exec.py`

Add `_parse_exec_error(error_message, code)` to `ExecSession`. On failure, `exec_code()` returns additional top-level fields alongside `isError: true`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_repl_daemon_exec.py`:

```python
def test_icc_exec_structured_error_fields(tmp_path):
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path)
    try:
        from scripts.emerge_daemon import EmergeDaemon
        daemon = EmergeDaemon(root=ROOT)
        result = daemon.call_tool("icc_exec", {"code": "x = undefined_var"})
        assert result.get("isError") is True
        assert "error_class" in result
        assert result["error_class"] == "NameError"
        assert "error_summary" in result
        assert "undefined_var" in result["error_summary"]
        assert "failed_line" in result
        assert isinstance(result["failed_line"], int)
        assert result.get("recovery_suggestion") == "exec"
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
```

- [ ] **Step 2: Run to confirm fail**

```bash
python -m pytest tests/test_repl_daemon_exec.py::test_icc_exec_structured_error_fields -v
```
Expected: FAIL — `assert "error_class" in result` fails.

- [ ] **Step 3: Add `_parse_exec_error()` to `exec_session.py`**

Add this static method to `ExecSession` (place it after `_write_recovery_status`):

```python
@staticmethod
def _parse_exec_error(error_message: str, code: str) -> dict[str, Any]:
    """Extract structured fields from a traceback string.

    Returns dict with keys: error_class (str), error_summary (str), failed_line (int).
    """
    import re
    error_class = "Exception"
    error_summary = error_message.strip().splitlines()[-1] if error_message.strip() else ""
    failed_line = 0

    # Extract exception class from last line: "ExcClass: message"
    last_line = error_summary
    m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*):\s*(.*)", last_line)
    if m:
        error_class = m.group(1).split(".")[-1]   # e.g. "NameError" not "builtins.NameError"
        error_summary = m.group(2).strip()

    # Extract line number from "File ..., line N"
    for line in error_message.splitlines():
        lm = re.search(r",\s*line\s+(\d+)", line)
        if lm:
            failed_line = int(lm.group(1))

    return {
        "error_class": error_class,
        "error_summary": error_summary,
        "failed_line": failed_line,
    }
```

- [ ] **Step 4: Return structured fields from `exec_code()` on failure**

In `exec_session.py`, inside `exec_code()`, find the block that builds `payload` when `is_error` is True and update it:

```python
        text = "\n\n".join(text_parts) if text_parts else "ok"
        payload: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
        if is_error:
            payload["isError"] = True
            parsed = self._parse_exec_error(error_message, code)
            payload["error_class"] = parsed["error_class"]
            payload["error_summary"] = parsed["error_summary"]
            payload["failed_line"] = parsed["failed_line"]
            payload["recovery_suggestion"] = "exec"
        return payload
```

- [ ] **Step 5: Run tests**

```bash
python -m pytest -q
```
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/exec_session.py tests/test_repl_daemon_exec.py
git commit -m "feat(recovery): structured error fields on icc_exec failure"
```

---

## Task 6: `PipelineMissingError` in `pipeline_engine.py`

**Files:**
- Modify: `scripts/pipeline_engine.py`
- Modify: `tests/test_pipeline_engine.py`

Add a typed exception that `call_tool` can catch to distinguish "no such pipeline" from "pipeline crashed".

- [ ] **Step 1: Write the failing test**

Add to `tests/test_pipeline_engine.py`:

```python
def test_missing_pipeline_raises_pipeline_missing_error(tmp_path):
    from scripts.pipeline_engine import PipelineEngine, PipelineMissingError
    import os
    os.environ["EMERGE_CONNECTOR_ROOT"] = str(tmp_path / "connectors")
    try:
        engine = PipelineEngine()
        with pytest.raises(PipelineMissingError) as exc_info:
            engine.run_read({"connector": "nonexistent", "pipeline": "nope"})
        assert "nonexistent" in str(exc_info.value)
    finally:
        os.environ.pop("EMERGE_CONNECTOR_ROOT", None)
```

- [ ] **Step 2: Run to confirm fail**

```bash
python -m pytest tests/test_pipeline_engine.py::test_missing_pipeline_raises_pipeline_missing_error -v
```
Expected: `ImportError: cannot import name 'PipelineMissingError'`

- [ ] **Step 3: Add `PipelineMissingError` and raise it in `_load_pipeline()`**

At the top of `scripts/pipeline_engine.py`, after the imports, add:

```python
class PipelineMissingError(FileNotFoundError):
    """Raised when a pipeline's .yaml + .py files cannot be found in any connector root.

    Subclasses FileNotFoundError so existing broad ``except Exception`` handlers
    still catch it, but ``call_tool`` can distinguish it with a specific ``except``.
    """
    def __init__(self, connector: str, mode: str, pipeline: str, searched: str) -> None:
        self.connector = connector
        self.mode = mode
        self.pipeline = pipeline
        self.searched = searched
        super().__init__(
            f"Pipeline '{connector}/{mode}/{pipeline}' not found in: {searched}"
        )
```

In `_load_pipeline()`, replace the existing `raise FileNotFoundError(...)`:

```python
        else:
            searched = ", ".join(str(r / connector) for r in self._connector_roots)
            raise PipelineMissingError(
                connector=connector, mode=mode, pipeline=pipeline, searched=searched
            )
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest -q
```
Expected: all pass (existing tests that catch FileNotFoundError still work since PipelineMissingError is a subclass).

- [ ] **Step 5: Commit**

```bash
git add scripts/pipeline_engine.py tests/test_pipeline_engine.py
git commit -m "feat(recovery): add PipelineMissingError to pipeline_engine"
```

---

## Task 7: Structured `pipeline_missing` response and `recovery_suggestion` in `emerge_daemon.py`

**Files:**
- Modify: `scripts/emerge_daemon.py`
- Modify: `tests/test_mcp_tools_integration.py`

Catch `PipelineMissingError` in `icc_read`/`icc_write` handlers and return a guidance response instead of a bare error. Add `recovery_suggestion: "exec"` to execution failures too.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mcp_tools_integration.py`:

```python
def test_icc_read_pipeline_missing_returns_structured_fallback(tmp_path):
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_CONNECTOR_ROOT"] = str(tmp_path / "connectors")
    try:
        from scripts.emerge_daemon import EmergeDaemon
        daemon = EmergeDaemon(root=ROOT)
        result = daemon.call_tool("icc_read", {
            "connector": "nonexistent",
            "pipeline": "nope",
        })
        # Must NOT be an error — it's a guidance response
        assert result.get("isError") is not True
        assert result.get("pipeline_missing") is True
        assert result.get("connector") == "nonexistent"
        assert result.get("pipeline") == "nope"
        assert result.get("mode") == "read"
        assert result.get("fallback") == "icc_exec"
        assert "icc_exec" in result.get("fallback_hint", "")
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_CONNECTOR_ROOT", None)


def test_icc_write_pipeline_missing_returns_structured_fallback(tmp_path):
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_CONNECTOR_ROOT"] = str(tmp_path / "connectors")
    try:
        from scripts.emerge_daemon import EmergeDaemon
        daemon = EmergeDaemon(root=ROOT)
        result = daemon.call_tool("icc_write", {
            "connector": "nonexistent",
            "pipeline": "nope",
        })
        assert result.get("isError") is not True
        assert result.get("pipeline_missing") is True
        assert result.get("mode") == "write"
        assert result.get("fallback") == "icc_exec"
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_CONNECTOR_ROOT", None)
```

- [ ] **Step 2: Run to confirm fail**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_icc_read_pipeline_missing_returns_structured_fallback tests/test_mcp_tools_integration.py::test_icc_write_pipeline_missing_returns_structured_fallback -v
```
Expected: FAIL — `assert result.get("pipeline_missing") is True` fails.

- [ ] **Step 3: Update `icc_read` handler in `emerge_daemon.py`**

In `call_tool()`, find the `if name == "icc_read":` block. Add an import at the top of the method (or file-level):
```python
from scripts.pipeline_engine import PipelineMissingError
```

Replace the existing `except Exception as exc:` handler for `icc_read`:

```python
        if name == "icc_read":
            try:
                _read_client = self._runner_router.find_client(arguments) if self._runner_router else None
                if _read_client is not None:
                    result = _read_client.call_tool("icc_read", arguments)
                    text = str(result.get("content", [{}])[0].get("text", ""))
                    payload = json.loads(text)
                    if not isinstance(payload, dict):
                        raise ValueError("runner icc_read payload must be an object")
                    result = payload
                else:
                    result = self.pipeline.run_read(arguments)
                response = {
                    "isError": False,
                    "content": [{"type": "text", "text": json.dumps(result)}],
                }
                try:
                    self._record_pipeline_event(
                        tool_name=name,
                        arguments=arguments,
                        result=result,
                        is_error=False,
                    )
                except Exception as exc:
                    self._append_warning_text(response, f"policy bookkeeping failed: {exc}")
                return response
            except PipelineMissingError as exc:
                connector = str(arguments.get("connector", ""))
                pipeline = str(arguments.get("pipeline", ""))
                hint = (
                    f"no pipeline registered yet — use icc_exec with "
                    f"intent_signature='{connector}.read.{pipeline}' to explore"
                )
                return {
                    "isError": False,
                    "pipeline_missing": True,
                    "connector": connector,
                    "pipeline": pipeline,
                    "mode": "read",
                    "fallback": "icc_exec",
                    "fallback_hint": hint,
                    "content": [{"type": "text", "text": f"Pipeline not found. {hint}"}],
                }
            except Exception as exc:
                try:
                    self._record_pipeline_event(
                        tool_name=name,
                        arguments=arguments,
                        result={},
                        is_error=True,
                        error_text=str(exc),
                    )
                except Exception:
                    pass
                return {
                    "isError": True,
                    "recovery_suggestion": "exec",
                    "content": [{"type": "text", "text": f"icc_read failed: {exc}"}],
                }
```

- [ ] **Step 4: Apply the same pattern to `icc_write` handler**

In the `if name == "icc_write":` block, add a matching `except PipelineMissingError as exc:` clause:

```python
            except PipelineMissingError as exc:
                connector = str(arguments.get("connector", ""))
                pipeline = str(arguments.get("pipeline", ""))
                hint = (
                    f"no pipeline registered yet — use icc_exec with "
                    f"intent_signature='{connector}.write.{pipeline}' to explore"
                )
                return {
                    "isError": False,
                    "pipeline_missing": True,
                    "connector": connector,
                    "pipeline": pipeline,
                    "mode": "write",
                    "fallback": "icc_exec",
                    "fallback_hint": hint,
                    "content": [{"type": "text", "text": f"Pipeline not found. {hint}"}],
                }
            except Exception as exc:
                # existing handler — add recovery_suggestion
                ...
                return {
                    "isError": True,
                    "recovery_suggestion": "exec",
                    "content": [{"type": "text", "text": f"icc_write failed: {exc}"}],
                }
```

Also add `from scripts.pipeline_engine import PipelineMissingError` at the top of `emerge_daemon.py` (file-level import, not inside the method).

- [ ] **Step 5: Run tests**

```bash
python -m pytest -q
```
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/emerge_daemon.py tests/test_mcp_tools_integration.py
git commit -m "feat(recovery): pipeline_missing guidance response + recovery_suggestion on exec/pipeline errors"
```

---

## Task 8: `synthesis_ready` signal in `_update_pipeline_registry()`

**Files:**
- Modify: `scripts/emerge_daemon.py`
- Modify: `tests/test_exec_flywheel.py`

When a non-pipeline exec candidate transitions explore → canary, check whether the WAL has a synthesizable code block for that `intent_signature`. If yes, set `synthesis_ready: true` in the registry and emit `policy.synthesis_ready`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_exec_flywheel.py`:

```python
def test_synthesis_ready_flag_set_on_canary_promotion(tmp_path):
    """synthesis_ready is set when an exec candidate reaches canary and WAL has code."""
    import json, os
    from scripts.emerge_daemon import EmergeDaemon
    from scripts.policy_config import PROMOTE_MIN_ATTEMPTS

    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_SESSION_ID"] = "synth-test"
    try:
        daemon = EmergeDaemon(root=ROOT)
        # Run enough successful icc_exec calls to cross the promote threshold
        for i in range(PROMOTE_MIN_ATTEMPTS):
            daemon.call_tool("icc_exec", {
                "code": f"__result = [{{'i': {i}}}]",
                "intent_signature": "test.read.synth",
                "no_replay": False,
            })
        registry_path = tmp_path / "state" / "pipelines-registry.json"
        assert registry_path.exists()
        data = json.loads(registry_path.read_text())
        # Find the candidate entry
        entries = data.get("pipelines", {})
        synth_entry = next(
            (v for k, v in entries.items() if "test.read.synth" in k),
            None,
        )
        assert synth_entry is not None, "no registry entry found for test.read.synth"
        # Should be canary now
        assert synth_entry.get("status") == "canary", f"expected canary, got {synth_entry.get('status')}"
        assert synth_entry.get("synthesis_ready") is True
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)
```

- [ ] **Step 2: Run to confirm fail**

```bash
python -m pytest tests/test_exec_flywheel.py::test_synthesis_ready_flag_set_on_canary_promotion -v
```
Expected: FAIL — `assert synth_entry.get("synthesis_ready") is True` fails.

- [ ] **Step 3: Add `_has_synthesizable_wal_entry()` helper to `EmergeDaemon`**

Add this method to `EmergeDaemon` (next to `_load_json_object`):

```python
def _has_synthesizable_wal_entry(self, intent_signature: str) -> bool:
    """Return True if the current session's WAL has at least one success entry
    with no_replay=False for the given intent_signature.

    Used to gate synthesis_ready — we only signal readiness if there is
    actual synthesizable code recorded.
    """
    if not intent_signature:
        return False
    session_dir = self._state_root / self._base_session_id
    wal_path = session_dir / "wal.jsonl"
    if not wal_path.exists():
        return False
    try:
        with wal_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    entry.get("status") == "success"
                    and not entry.get("no_replay", False)
                    and entry.get("metadata", {}).get("intent_signature") == intent_signature
                ):
                    return True
    except OSError:
        pass
    return False
```

- [ ] **Step 4: Emit `synthesis_ready` in `_update_pipeline_registry()`**

In `_update_pipeline_registry()`, in the `if status == "explore": … if should_promote:` block, after setting `status = "canary"`, add:

```python
        if should_promote:
            status = "canary"
            transitioned = True
            reason = "promotion_threshold_met"
            pipeline["rollout_pct"] = 20
            # Signal that this exec candidate can be crystallized into a pipeline
            intent_sig = entry.get("intent_signature", "")
            if intent_sig and not candidate_key.startswith("pipeline::"):
                if self._has_synthesizable_wal_entry(intent_sig):
                    pipeline["synthesis_ready"] = True
                    try:
                        self._sink.emit(
                            "policy.synthesis_ready",
                            {
                                "candidate_key": candidate_key,
                                "intent_signature": intent_sig,
                                "session_id": self._base_session_id,
                            },
                        )
                    except Exception:
                        pass
```

- [ ] **Step 5: Run tests**

```bash
python -m pytest -q
```
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/emerge_daemon.py tests/test_exec_flywheel.py
git commit -m "feat(synthesis): synthesis_ready signal on exec→canary promotion"
```

---

## Task 9: `icc_crystallize` tool

**Files:**
- Modify: `scripts/emerge_daemon.py`
- Create: `tests/test_crystallize.py`

Add `icc_crystallize` to `call_tool()` and `tools/list`. The tool reads the WAL for the most recent synthesizable code block matching `intent_signature`, wraps it in a pipeline harness, writes `.py` + `.yaml`, and returns paths.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_crystallize.py`:

```python
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
```

- [ ] **Step 2: Run to confirm fail**

```bash
python -m pytest tests/test_crystallize.py -v
```
Expected: FAIL on `assert result.get("isError") is not True` — unknown tool.

- [ ] **Step 3: Add `_crystallize()` method to `EmergeDaemon`**

Add to `emerge_daemon.py`:

```python
def _crystallize(
    self,
    *,
    intent_signature: str,
    connector: str,
    pipeline_name: str,
    mode: str,
    target_profile: str = "default",
) -> dict[str, Any]:
    """Scan the WAL for the most recent synthesizable exec for intent_signature,
    wrap it in a pipeline harness, and write .py + .yaml to the user connector root.
    """
    import time as _time
    import textwrap

    # --- find synthesizable WAL entry ---
    # Resolve session for the given profile (same logic as _get_session)
    normalized = (target_profile or "default").strip() or "default"
    profile_key = "__default__" if normalized == "default" else derive_profile_token(normalized)
    if normalized == "default":
        session_id = self._base_session_id
    else:
        session_id = f"{self._base_session_id}__{profile_key}"

    session_dir = self._state_root / session_id
    wal_path = session_dir / "wal.jsonl"

    best_code: str | None = None
    if wal_path.exists():
        with wal_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    entry.get("status") == "success"
                    and not entry.get("no_replay", False)
                    and entry.get("metadata", {}).get("intent_signature") == intent_signature
                ):
                    best_code = str(entry.get("code", "")).strip()
                    # keep scanning — we want the LAST (most recent) match

    if not best_code:
        return {
            "isError": True,
            "content": [{"type": "text", "text": (
                f"icc_crystallize: no synthesizable WAL entry found for "
                f"intent_signature='{intent_signature}'. Run icc_exec with "
                f"intent_signature='{intent_signature}' and no_replay=false first."
            )}],
        }

    # --- generate pipeline harness ---
    ts = int(_time.time())
    indented = textwrap.indent(best_code, "    ")

    if mode == "read":
        py_src = (
            f"# auto-generated by icc_crystallize — review before promoting\n"
            f"# intent_signature: {intent_signature}\n"
            f"# synthesized_at: {ts}\n"
            f"\n"
            f"def run_read(metadata, args):\n"
            f"    __args = args  # compat with exec __args scope\n"
            f"    # --- CRYSTALLIZED ---\n"
            f"{indented}\n"
            f"    # --- END ---\n"
            f"    return __result  # exec code must set __result = [{{...}}]\n"
            f"\n"
            f"\n"
            f"def verify_read(metadata, args, rows):\n"
            f"    return {{\"ok\": bool(rows)}}\n"
        )
        yaml_src = (
            f"intent_signature: {intent_signature}\n"
            f"rollback_or_stop_policy: stop\n"
            f"read_steps:\n"
            f"  - run_read\n"
            f"verify_steps:\n"
            f"  - verify_read\n"
            f"synthesized: true\n"
            f"synthesized_at: {ts}\n"
        )
    else:  # write
        py_src = (
            f"# auto-generated by icc_crystallize — review before promoting\n"
            f"# intent_signature: {intent_signature}\n"
            f"# synthesized_at: {ts}\n"
            f"\n"
            f"def run_write(metadata, args):\n"
            f"    __args = args  # compat with exec __args scope\n"
            f"    # --- CRYSTALLIZED ---\n"
            f"{indented}\n"
            f"    # --- END ---\n"
            f"    return __action  # exec code must set __action = {{\"ok\": True, ...}}\n"
            f"\n"
            f"\n"
            f"def verify_write(metadata, args, action_result):\n"
            f"    return {{\"ok\": bool(action_result.get(\"ok\"))}}\n"
        )
        yaml_src = (
            f"intent_signature: {intent_signature}\n"
            f"rollback_or_stop_policy: stop\n"
            f"write_steps:\n"
            f"  - run_write\n"
            f"verify_steps:\n"
            f"  - verify_write\n"
            f"synthesized: true\n"
            f"synthesized_at: {ts}\n"
        )

    # --- write files ---
    # Use the first connector root that is user-writable (prefer EMERGE_CONNECTOR_ROOT or ~/.emerge/connectors)
    from scripts.pipeline_engine import _USER_CONNECTOR_ROOT
    env_root_str = os.environ.get("EMERGE_CONNECTOR_ROOT", "").strip()
    target_root = Path(env_root_str).expanduser() if env_root_str else _USER_CONNECTOR_ROOT

    pipeline_dir = target_root / connector / "pipelines" / mode
    pipeline_dir.mkdir(parents=True, exist_ok=True)

    py_path = pipeline_dir / f"{pipeline_name}.py"
    yaml_path = pipeline_dir / f"{pipeline_name}.yaml"
    py_path.write_text(py_src, encoding="utf-8")
    yaml_path.write_text(yaml_src, encoding="utf-8")

    # code_preview: first 20 lines
    preview_lines = py_src.splitlines()[:20]
    code_preview = "\n".join(preview_lines)

    return {
        "ok": True,
        "py_path": str(py_path),
        "yaml_path": str(yaml_path),
        "code_preview": code_preview,
        "content": [{"type": "text", "text": json.dumps({
            "ok": True,
            "py_path": str(py_path),
            "yaml_path": str(yaml_path),
        })}],
    }
```

- [ ] **Step 4: Wire `icc_crystallize` into `call_tool()` and `tools/list`**

In `call_tool()`, add before the final `return {"isError": True, ...}`:

```python
        if name == "icc_crystallize":
            try:
                intent_signature = str(arguments.get("intent_signature", "")).strip()
                connector = str(arguments.get("connector", "")).strip()
                pipeline_name = str(arguments.get("pipeline_name", "")).strip()
                mode = str(arguments.get("mode", "read")).strip()
                target_profile = str(arguments.get("target_profile", "default")).strip()
                if not all([intent_signature, connector, pipeline_name, mode]):
                    return {
                        "isError": True,
                        "content": [{"type": "text", "text": "icc_crystallize: intent_signature, connector, pipeline_name, and mode are required"}],
                    }
                if mode not in ("read", "write"):
                    return {
                        "isError": True,
                        "content": [{"type": "text", "text": f"icc_crystallize: mode must be 'read' or 'write', got {mode!r}"}],
                    }
                return self._crystallize(
                    intent_signature=intent_signature,
                    connector=connector,
                    pipeline_name=pipeline_name,
                    mode=mode,
                    target_profile=target_profile,
                )
            except Exception as exc:
                return {"isError": True, "content": [{"type": "text", "text": f"icc_crystallize failed: {exc}"}]}
```

In `tools/list`, add after the `icc_reconcile` entry:

```python
                        {
                            "name": "icc_crystallize",
                            "description": "Crystallize exec history into a pipeline file. Reads the WAL for the most recent successful icc_exec matching intent_signature and generates .py + .yaml in ~/.emerge/connectors/. Call when synthesis_ready is true in policy://current.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "intent_signature": {"type": "string", "description": "Intent signature used in icc_exec calls (e.g. zwcad.read.state)"},
                                    "connector": {"type": "string", "description": "Connector name for the output pipeline (e.g. zwcad)"},
                                    "pipeline_name": {"type": "string", "description": "Pipeline file name without extension (e.g. state)"},
                                    "mode": {"type": "string", "enum": ["read", "write"], "description": "Pipeline mode"},
                                    "target_profile": {"type": "string", "description": "Which exec profile's WAL to read", "default": "default"},
                                },
                                "required": ["intent_signature", "connector", "pipeline_name", "mode"],
                            },
                        },
```

- [ ] **Step 5: Run tests**

```bash
python -m pytest -q
```
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/emerge_daemon.py tests/test_crystallize.py
git commit -m "feat(synthesis): add icc_crystallize tool — crystallize exec WAL into pipeline files"
```

---

## Task 10: Wire `icc_reconcile(outcome=correct)` to `human_fixes`

**Files:**
- Modify: `scripts/emerge_daemon.py`
- Modify: `tests/test_mcp_tools_integration.py`

Remove the `trusted_human_fix = False` hardcoding in both `_record_exec_event` and `_record_pipeline_event`. Add `intent_signature` parameter to `icc_reconcile` so `outcome=correct` can increment the right candidate.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_mcp_tools_integration.py`:

```python
def test_icc_reconcile_correct_increments_human_fixes(tmp_path):
    """icc_reconcile(outcome=correct, intent_signature=X) must increment human_fixes
    on the matching candidate, which affects human_fix_rate in the policy registry."""
    import json, os
    from scripts.emerge_daemon import EmergeDaemon

    state_root = tmp_path / "state"
    os.environ["EMERGE_STATE_ROOT"] = str(state_root)
    os.environ["EMERGE_SESSION_ID"] = "reconcile-fix-test"
    try:
        daemon = EmergeDaemon(root=ROOT)
        # Run one exec to create a candidate
        daemon.call_tool("icc_exec", {
            "code": "x = 1",
            "intent_signature": "test.write.fixme",
        })
        # Reconcile with correct — simulates human correcting AI output
        daemon.call_tool("icc_reconcile", {
            "delta_id": "fake-delta",   # StateTracker is lenient about unknown deltas
            "outcome": "correct",
            "intent_signature": "test.write.fixme",
        })
        # Read candidates.json and verify human_fixes incremented
        session_dir = state_root / "reconcile-fix-test"
        cands = json.loads((session_dir / "candidates.json").read_text())
        matched = [
            v for k, v in cands["candidates"].items()
            if "test.write.fixme" in k
        ]
        assert matched, "no candidate found for test.write.fixme"
        assert matched[0]["human_fixes"] >= 1, "human_fixes not incremented"
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)
```

- [ ] **Step 2: Run to confirm fail**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_icc_reconcile_correct_increments_human_fixes -v
```
Expected: FAIL — `assert matched[0]["human_fixes"] >= 1` fails (currently 0).

- [ ] **Step 3: Update `icc_reconcile` handler in `emerge_daemon.py`**

Find the `if name == "icc_reconcile":` block. Replace it entirely:

```python
        if name == "icc_reconcile":
            delta_id = str(arguments.get("delta_id", "")).strip()
            outcome = str(arguments.get("outcome", "")).strip()
            intent_signature = str(arguments.get("intent_signature", "")).strip()
            if not delta_id:
                return {"isError": True, "content": [{"type": "text", "text": "icc_reconcile: delta_id is required"}]}
            if outcome not in ("confirm", "correct", "retract"):
                return {"isError": True, "content": [{"type": "text", "text": f"icc_reconcile: outcome must be confirm/correct/retract, got {outcome!r}"}]}
            from scripts.policy_config import default_hook_state_root
            from scripts.state_tracker import load_tracker, save_tracker
            state_path = Path(os.environ.get("CLAUDE_PLUGIN_DATA", str(default_hook_state_root()))) / "state.json"
            tracker = load_tracker(state_path)
            tracker.reconcile_delta(delta_id, outcome)
            save_tracker(state_path, tracker)
            td = tracker.to_dict()

            # When outcome=correct and intent_signature provided, increment human_fixes
            # on the matching candidate so human_fix_rate feeds the promotion gate.
            if outcome == "correct" and intent_signature:
                self._increment_human_fix(intent_signature)

            return {"isError": False, "content": [{"type": "text", "text": json.dumps({
                "delta_id": delta_id,
                "outcome": outcome,
                "intent_signature": intent_signature or None,
                "verification_state": td.get("verification_state", "unverified"),
                "goal": td.get("goal", ""),
            })}]}
```

- [ ] **Step 4: Add `_increment_human_fix()` to `EmergeDaemon`**

```python
def _increment_human_fix(self, intent_signature: str) -> None:
    """Increment human_fixes on the most recent candidate matching intent_signature.

    Searches all candidate key types (exec, pipeline, bridge) in the current
    session's candidates.json for any entry whose intent_signature matches.
    Updates candidates.json and triggers a registry update so human_fix_rate
    flows into the next policy evaluation.
    """
    session_dir = self._state_root / self._base_session_id
    candidates_path = session_dir / "candidates.json"
    if not candidates_path.exists():
        return
    registry = self._load_json_object(candidates_path, root_key="candidates")
    updated = False
    for key, entry in registry["candidates"].items():
        if not isinstance(entry, dict):
            continue
        if str(entry.get("intent_signature", "")) == intent_signature:
            entry["human_fixes"] = int(entry.get("human_fixes", 0)) + 1
            registry["candidates"][key] = entry
            updated = True
            # Trigger registry update so the new rate is reflected immediately
            try:
                self._update_pipeline_registry(candidate_key=key, entry=entry)
            except Exception:
                pass
    if updated:
        self._atomic_write_json(candidates_path, registry)
```

- [ ] **Step 5: Update `icc_reconcile` in `tools/list` to document `intent_signature`**

In the `tools/list` response, find the `icc_reconcile` entry and update its `inputSchema`:

```python
                        {
                            "name": "icc_reconcile",
                            "description": "Reconcile a state tracker delta — confirm, correct, or retract a recorded observation. Pass intent_signature with outcome=correct to register a human fix against the policy flywheel.",
                            "_internal": True,
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "delta_id": {"type": "string", "description": "ID of the delta to reconcile"},
                                    "outcome": {"type": "string", "enum": ["confirm", "correct", "retract"], "description": "Reconciliation outcome"},
                                    "intent_signature": {"type": "string", "description": "Intent signature of the exec/pipeline being corrected (required when outcome=correct to update human_fix_rate)"},
                                },
                                "required": ["delta_id", "outcome"],
                            },
                        },
```

- [ ] **Step 6: Run tests**

```bash
python -m pytest -q
```
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add scripts/emerge_daemon.py tests/test_mcp_tools_integration.py
git commit -m "feat(human-fix): wire icc_reconcile(outcome=correct) to human_fix_rate via _increment_human_fix()"
```

---

## Task 11: Remove obsolete `icc_promote` MCP prompt

**Files:**
- Modify: `scripts/emerge_daemon.py`
- Modify: `tests/test_mcp_tools_integration.py` (if it tests icc_promote)

`icc_crystallize` replaces the manual `icc_promote` prompt. Remove the prompt from `_PROMPTS` and `_get_prompt()`.

- [ ] **Step 1: Check whether any test asserts on `icc_promote` existing**

```bash
grep -n "icc_promote" tests/*.py
```

If tests assert on it, update them first. If not, proceed.

- [ ] **Step 2: Remove `icc_promote` from `_PROMPTS`**

In `emerge_daemon.py`, find `_PROMPTS` class variable. Remove the entire dict entry for `"name": "icc_promote"`.

- [ ] **Step 3: Remove `icc_promote` case from `_get_prompt()`**

In `_get_prompt()`, remove the `if name == "icc_promote":` block entirely.

- [ ] **Step 4: Run tests**

```bash
python -m pytest -q
```
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/emerge_daemon.py
git commit -m "refactor: remove obsolete icc_promote prompt (replaced by icc_crystallize tool)"
```

---

## Task 12: Write `skills/muscle-memory-flywheel/SKILL.md`

**Files:**
- Create: `skills/muscle-memory-flywheel/SKILL.md`

- [ ] **Step 1: Create the skill file**

```bash
mkdir -p skills/muscle-memory-flywheel
```

Create `skills/muscle-memory-flywheel/SKILL.md` with this content:

```markdown
---
name: muscle-memory-flywheel
description: How to use the emerge flywheel so AI tasks get faster over time. Covers exec conventions, crystallization trigger, reconcile usage, and pipeline lifecycle stages.
---

# Muscle Memory Flywheel

The emerge plugin turns repeated AI actions into deterministic pipelines. The first time a task runs, Claude reasons through it fully (`icc_exec`, slow). Successful patterns crystallize into pipelines (`icc_read`/`icc_write`, fast). Eventually the same task runs at near-native speed with no LLM overhead.

```
icc_exec (AI reasoning, slow)
  → flywheel log accumulates
  → synthesis_ready threshold crossed
  → icc_crystallize generates draft pipeline
  → Claude reviews + validates
  → explore → canary → stable
  → muscle memory: same task, ~native speed
```

---

## Exec Code Conventions

For code to be crystallizable, `icc_exec` calls must follow these conventions:

| Convention | Rule |
|---|---|
| Read output | Set `__result = [{"key": value, ...}]` before the end of the code block |
| Write output | Set `__action = {"ok": True, ...}` before the end of the code block |
| Side effects (COM calls, file writes, network) | Pass `no_replay=True` — excluded from crystallization and WAL replay |
| State setup (creating COM objects, imports) | No `no_replay` — replayed on restart and included in crystallization |

**Example — correct pattern for a read task:**

```python
# icc_exec call — sets up state (no no_replay)
icc_exec(
    code="import win32com.client; app = win32com.client.Dispatch('ZwCAD.Application')",
    intent_signature="zwcad.read.state",
)

# icc_exec call — side-effectful, not replayed
icc_exec(
    code="doc = app.ActiveDocument",
    intent_signature="zwcad.read.state",
    no_replay=True,
)

# icc_exec call — produces output, crystallizable
icc_exec(
    code="__result = [{'entity_count': doc.ModelSpace.Count}]",
    intent_signature="zwcad.read.state",
)
```

---

## When to Crystallize

Check `policy://current` resource. When an exec candidate shows `synthesis_ready: true`, the flywheel has accumulated enough history to crystallize:

```python
icc_crystallize(
    intent_signature="zwcad.read.state",
    connector="zwcad",
    pipeline_name="state",
    mode="read",
    target_profile="default",   # optional, defaults to "default"
)
```

This generates `~/.emerge/connectors/zwcad/pipelines/read/state.py` and `state.yaml`.

**After crystallization:**
1. Review the generated `.py` — verify `run_read`/`run_write` body looks correct
2. Edit if needed (the crystallized code is a starting point, not a final answer)
3. Validate: `icc_read(connector="zwcad", pipeline="state")`
4. Each successful `icc_read`/`icc_write` call feeds the policy flywheel toward canary then stable

---

## Registering Human Fixes

When you correct AI output, tell the flywheel so that pattern is not promoted:

```python
icc_reconcile(
    delta_id="<delta-id from state tracker>",
    outcome="correct",
    intent_signature="zwcad.write.apply-change",   # the pattern being corrected
)
```

A pattern with >5% human corrections stays in explore permanently. True muscle memory — where AI gets it right without help — promotes normally.

---

## Pipeline Lifecycle

| Stage | rollout_pct | Meaning |
|---|---|---|
| `explore` | 0% | Accumulating history, not yet trusted |
| `canary` | 20% | Threshold met, gradual rollout |
| `stable` | 100% | Fully trusted, native speed |

Promotion thresholds (configurable in `~/.emerge/settings.json`):
- explore → canary: 20 attempts, 95% success, 98% verify, ≤5% human-fix
- canary → stable: 40 attempts, 97% success, 99% verify
- Any stage → explore: 2 consecutive failures, or window failure rate <90%

---

## Recovery Signals

When `icc_exec` fails, the response includes structured fields:

```json
{
  "isError": true,
  "error_class": "NameError",
  "error_summary": "name 'app' is not defined",
  "failed_line": 3,
  "recovery_suggestion": "exec"
}
```

When `icc_read`/`icc_write` finds no pipeline yet, `isError` is **false** — it's a guidance response:

```json
{
  "isError": false,
  "pipeline_missing": true,
  "fallback": "icc_exec",
  "fallback_hint": "use icc_exec with intent_signature='zwcad.read.state'"
}
```
```

- [ ] **Step 2: Run tests (no new tests needed for skill doc)**

```bash
python -m pytest -q
```
Expected: all pass.

- [ ] **Step 3: Commit**

```bash
git add skills/muscle-memory-flywheel/SKILL.md
git commit -m "docs: add muscle-memory-flywheel skill documenting flywheel conventions"
```

---

## Self-Review

**Spec coverage check:**
- Part 1 (structured errors): Tasks 5, 6, 7 ✓
- Part 2 (synthesis_ready + icc_crystallize): Tasks 8, 9 ✓
- Part 3 (human fix tracking): Task 10 ✓
- Naming unification: Tasks 1–4 ✓
- Remove icc_promote: Task 11 ✓
- Skill doc: Task 12 ✓
- `trusted_human_fix = False` hardcoding removed: covered in Task 10 (`_increment_human_fix` makes it real; note: the `trusted_human_fix = False` lines in `_record_exec_event` and `_record_pipeline_event` remain since those paths don't receive human fix signals — `icc_reconcile` is the explicit signal path per the spec)

**Placeholder scan:** No TBD or TODO present. All code blocks are complete.

**Type consistency:**
- `ExecSession` used consistently in Tasks 2, 3
- `EmergeDaemon` used consistently in Tasks 3–10
- `PipelineMissingError` defined in Task 6, used in Task 7
- `_crystallize()` defined in Task 9, wired in Task 9
- `_increment_human_fix()` defined in Task 10, called in Task 10
- `_has_synthesizable_wal_entry()` defined in Task 8, called in Task 8
