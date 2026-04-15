# Comprehensive Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix bugs in the hot path, restructure `pre_tool_use.py` for maintainability, tighten CC context signal quality, and split `admin/api.py` into focused sub-modules.

**Architecture:** Four independent sections, each shippable on its own. Section 1 (bugs) → Section 2 (pre_tool_use refactor) → Section 3 (signal quality) → Section 4 (admin split). All 605 existing tests must pass after each section.

**Tech Stack:** Python 3.11+, pytest, `unittest.mock.patch`

---

## File Map

| File | Action | Section |
|------|---------|---------|
| `hooks/pre_tool_use.py` | Major refactor — module-level regex, per-tool validators, dispatch | 1+2 |
| `scripts/observer_plugin.py` | Fix `__import__` antipattern | 1 |
| `scripts/emerge_daemon.py` | Remove redundant `key` field in emit call | 1 |
| `hooks/user_prompt_submit.py` | Add nudge-flag check before span reminder | 3 |
| `hooks/instructions_loaded.py` | Add clarifying comment on 1200-char limit | 3 |
| `scripts/emerge_daemon.py` | Add docstring to `_span_run_pipeline` | 3 |
| `scripts/admin/shared.py` | **Create** — shared path resolvers used by 2+ modules | 4 |
| `scripts/admin/control_plane.py` | **Create** — all `cmd_control_plane_*` functions | 4 |
| `scripts/admin/pipeline.py` | **Create** — pipeline/connector/policy operations | 4 |
| `scripts/admin/api.py` | Shrink to ~500 lines — keep SSE/actions/goal/settings + re-exports | 4 |
| `tests/test_cockpit_api.py` | Update patch targets to new module locations | 4 |

---

## Task 1: Bug Fixes — regex antipatterns and dead code

**Files:**
- Modify: `hooks/pre_tool_use.py`
- Modify: `scripts/observer_plugin.py`
- Modify: `scripts/emerge_daemon.py`

- [ ] **Step 1: Run existing tests to establish baseline**

```bash
python -m pytest tests/test_hooks_pre_tool_use.py tests/test_mcp_tools_integration.py -q
```
Expected: all pass.

- [ ] **Step 2: Fix `pre_tool_use.py` — module-level regex + clean import**

Replace the top of `hooks/pre_tool_use.py`. Current file starts with:
```python
from __future__ import annotations

import json
import sys
from pathlib import Path
...
def main() -> None:
    ...
    # icc_read / icc_write are fully deleted. Use icc_span_open for bridge execution.  ← DELETE THIS
    ...
    if tool_name.endswith("__icc_exec"):
        ...
        import re as _re          ← DELETE (3 occurrences)
        ...
        _sig_pattern = _re.compile(r'^[a-z][a-z0-9_-]*\.(read|write)\.[a-z][a-z0-9_./-]*$')  ← DELETE
        ...
        _safe_seg = __import__("re").compile(r"^[a-z0-9][a-z0-9_-]*$")  ← DELETE
```

New file header (replace everything before `def main()`):
```python
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Compiled once at module load — shared across all validator functions.
_SIG_RE = re.compile(r'^[a-z][a-z0-9_-]*\.(read|write)\.[a-z][a-z0-9_./-]*$')
_SAFE_SEG_RE = re.compile(r'^[a-z0-9][a-z0-9_-]*$')
_VAR_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
```

Then in `main()`:
- Remove the floating comment `# icc_read / icc_write are fully deleted...` (line ~29)
- Replace each `import re as _re` / `_re.compile(...)` / `__import__("re").compile(...)` with the corresponding module-level constant:
  - `_re.compile(r'^[a-z][a-z0-9_-]*...')` → `_SIG_RE`
  - `_re.compile(r"^[A-Za-z_]...")` → `_VAR_RE`
  - `__import__("re").compile(r"^[a-z0-9][a-z0-9_-]*$")` → `_SAFE_SEG_RE`

- [ ] **Step 3: Fix `observer_plugin.py` — `__import__` antipattern**

Find in `scripts/observer_plugin.py` (class body):
```python
_SAFE_NAME_RE = __import__("re").compile(r"^[a-z0-9][a-z0-9_-]*$")
```

Replace with a module-level constant. At the top of the file, add after the existing imports:
```python
import re as _re
```
(or if `re` is already imported, skip this line)

Then change the class body line to:
```python
_SAFE_NAME_RE = _re.compile(r"^[a-z0-9][a-z0-9_-]*$")
```

- [ ] **Step 4: Fix `emerge_daemon.py` — redundant emit field**

Find in `scripts/emerge_daemon.py` (inside `_try_flywheel_bridge`):
```python
self._sink.emit("flywheel.bridge.promoted", {"key": base_pipeline_id, "pipeline_id": base_pipeline_id})
```

Replace with:
```python
self._sink.emit("flywheel.bridge.promoted", {"pipeline_id": base_pipeline_id})
```

- [ ] **Step 5: Run tests to confirm no regressions**

```bash
python -m pytest tests -q --tb=short
```
Expected: 605 passed.

- [ ] **Step 6: Commit**

```bash
git add hooks/pre_tool_use.py scripts/observer_plugin.py scripts/emerge_daemon.py
git commit -m "fix: module-level regex constants in pre_tool_use + observer_plugin, remove emit redundant key"
```

---

## Task 2: TDD — write tests for per-tool validators

**Files:**
- Modify: `tests/test_hooks_pre_tool_use.py`

These tests will **fail** until Task 3 implements the functions. That is the intent (TDD).

- [ ] **Step 1: Add validator unit tests to `tests/test_hooks_pre_tool_use.py`**

Append to the end of the file:

```python
# ---------------------------------------------------------------------------
# Unit tests for extracted per-tool validator functions (Task 3)
# ---------------------------------------------------------------------------

def test_validate_icc_exec_valid():
    from hooks.pre_tool_use import _validate_icc_exec
    assert _validate_icc_exec({"mode": "inline_code", "code": "x=1"}, "zwcad.read.state") is None

def test_validate_icc_exec_missing_code():
    from hooks.pre_tool_use import _validate_icc_exec
    err = _validate_icc_exec({"mode": "inline_code", "code": ""}, "zwcad.read.state")
    assert err is not None and "code" in err

def test_validate_icc_exec_missing_sig():
    from hooks.pre_tool_use import _validate_icc_exec
    err = _validate_icc_exec({"mode": "inline_code", "code": "x=1"}, "")
    assert err is not None and "intent_signature" in err

def test_validate_icc_exec_two_part_sig():
    from hooks.pre_tool_use import _validate_icc_exec
    err = _validate_icc_exec({"mode": "inline_code", "code": "x=1"}, "zwcad.state")
    assert err is not None and "2 parts" in err

def test_validate_icc_exec_invalid_mode():
    from hooks.pre_tool_use import _validate_icc_exec
    err = _validate_icc_exec({"mode": "bad_mode", "code": "x=1"}, "zwcad.read.state")
    assert err is not None and "mode" in err

def test_validate_icc_exec_invalid_result_var():
    from hooks.pre_tool_use import _validate_icc_exec
    err = _validate_icc_exec({"mode": "inline_code", "code": "x=1", "result_var": "123bad"}, "zwcad.read.state")
    assert err is not None and "result_var" in err

def test_validate_icc_exec_valid_script_ref():
    from hooks.pre_tool_use import _validate_icc_exec
    assert _validate_icc_exec({"mode": "script_ref", "script_ref": "my_script.py"}, "zwcad.read.state") is None

def test_validate_icc_reconcile_valid():
    from hooks.pre_tool_use import _validate_icc_reconcile
    assert _validate_icc_reconcile({"delta_id": "d-1", "outcome": "confirm"}) is None

def test_validate_icc_reconcile_missing_delta_id():
    from hooks.pre_tool_use import _validate_icc_reconcile
    err = _validate_icc_reconcile({"delta_id": "", "outcome": "confirm"})
    assert err is not None and "delta_id" in err

def test_validate_icc_reconcile_bad_outcome():
    from hooks.pre_tool_use import _validate_icc_reconcile
    err = _validate_icc_reconcile({"delta_id": "d-1", "outcome": "wrong"})
    assert err is not None and "outcome" in err

def test_validate_icc_crystallize_valid():
    from hooks.pre_tool_use import _validate_icc_crystallize
    assert _validate_icc_crystallize(
        {"connector": "zwcad", "pipeline_name": "my-pipe", "mode": "read"},
        "zwcad.read.my-pipe",
    ) is None

def test_validate_icc_crystallize_unsafe_connector():
    from hooks.pre_tool_use import _validate_icc_crystallize
    err = _validate_icc_crystallize(
        {"connector": "ZWCAD", "pipeline_name": "p", "mode": "read"}, "zwcad.read.p"
    )
    assert err is not None and "connector" in err

def test_validate_icc_crystallize_path_traversal():
    from hooks.pre_tool_use import _validate_icc_crystallize
    err = _validate_icc_crystallize(
        {"connector": "zwcad", "pipeline_name": "../evil", "mode": "read"}, "zwcad.read.x"
    )
    assert err is not None and "pipeline_name" in err

def test_validate_icc_span_open_valid():
    from hooks.pre_tool_use import _validate_icc_span_open
    assert _validate_icc_span_open({}, "lark.read.get-doc") is None

def test_validate_icc_span_open_missing_sig():
    from hooks.pre_tool_use import _validate_icc_span_open
    err = _validate_icc_span_open({}, "")
    assert err is not None and "intent_signature" in err

def test_validate_icc_span_close_valid():
    from hooks.pre_tool_use import _validate_icc_span_close
    for outcome in ("success", "failure", "aborted"):
        assert _validate_icc_span_close({"outcome": outcome}) is None

def test_validate_icc_span_close_bad_outcome():
    from hooks.pre_tool_use import _validate_icc_span_close
    err = _validate_icc_span_close({"outcome": "done"})
    assert err is not None and "outcome" in err

def test_validate_icc_span_approve_valid():
    from hooks.pre_tool_use import _validate_icc_span_approve
    assert _validate_icc_span_approve({}, "zwcad.write.apply") is None

def test_validate_icc_span_approve_missing_sig():
    from hooks.pre_tool_use import _validate_icc_span_approve
    err = _validate_icc_span_approve({}, "")
    assert err is not None and "intent_signature" in err

def test_validate_icc_goal_rollback_valid():
    from hooks.pre_tool_use import _validate_icc_goal_rollback
    assert _validate_icc_goal_rollback({"target_event_id": "evt-abc"}) is None

def test_validate_icc_goal_rollback_missing():
    from hooks.pre_tool_use import _validate_icc_goal_rollback
    err = _validate_icc_goal_rollback({})
    assert err is not None and "target_event_id" in err

def test_normalize_sig_no_change():
    from hooks.pre_tool_use import _normalize_sig
    sig, frm, to = _normalize_sig("zwcad.read.state")
    assert sig == "zwcad.read.state"
    assert frm is None and to is None

def test_normalize_sig_lowercases():
    from hooks.pre_tool_use import _normalize_sig
    sig, frm, to = _normalize_sig("ZWCAD.READ.State")
    assert sig == "zwcad.read.state"
    assert frm == "ZWCAD.READ.State"
    assert to == "zwcad.read.state"
```

- [ ] **Step 2: Run tests to confirm they fail with ImportError**

```bash
python -m pytest tests/test_hooks_pre_tool_use.py -k "test_validate or test_normalize_sig" -q
```
Expected: FAIL — `ImportError: cannot import name '_validate_icc_exec'`

---

## Task 3: Extract per-tool validator functions

**Files:**
- Modify: `hooks/pre_tool_use.py`

- [ ] **Step 1: Add `_normalize_sig` after the regex constants**

After the `_VAR_RE` line at module level, add:

```python
def _normalize_sig(raw: str) -> tuple[str, str | None, str | None]:
    """Lowercase-normalize intent_signature.

    Returns (normalized, from_raw, to_norm).
    from_raw and to_norm are None if no change was needed.
    """
    normalized = raw.lower()
    if normalized != raw:
        return normalized, raw, normalized
    return raw, None, None
```

- [ ] **Step 2: Add the seven validator functions**

Add these functions to `hooks/pre_tool_use.py` at module level (before `main()`):

```python
def _validate_icc_exec(args: dict, sig: str) -> str | None:
    mode = str(args.get("mode", "inline_code")).strip()
    if mode not in ("inline_code", "script_ref"):
        return f"icc_exec: 'mode' must be inline_code or script_ref, got {mode!r}"
    if mode == "inline_code" and not str(args.get("code", "")).strip():
        return "icc_exec (mode=inline_code): 'code' argument is required"
    if mode == "script_ref" and not str(args.get("script_ref", "")).strip():
        return "icc_exec (mode=script_ref): 'script_ref' argument is required"
    if not sig:
        return (
            "icc_exec: 'intent_signature' is required (e.g. 'zwcad.read.state'). "
            "Read tasks must set __result=[{...}] in code. "
            "Write tasks must set __action={'ok': True, ...} in code. "
            "Side-effectful calls (COM, file writes, network) must use no_replay=True. "
            "State setup calls (imports, object creation) must NOT use no_replay."
        )
    if len(sig.split(".")) == 2:
        return (
            f"icc_exec: intent_signature {sig!r} has only 2 parts. "
            "Required format: connector.mode.name (e.g. 'zwcad.read.layers'). "
            "Add the connector name as the first part."
        )
    if not _SIG_RE.match(sig):
        return (
            f"icc_exec: intent_signature {sig!r} is invalid. "
            "Must be <connector>.(read|write).<name> — e.g. 'zwcad.read.state', "
            "'hypermesh.write.apply-change'. Middle segment must be 'read' or 'write'. "
            "Check connector://notes to see existing intents for this connector."
        )
    result_var = str(args.get("result_var", "")).strip()
    if result_var and not _VAR_RE.match(result_var):
        return (
            f"icc_exec: result_var {result_var!r} is invalid. "
            "Must be a Python identifier, e.g. '__result' or 'output_rows'."
        )
    return None


def _validate_icc_reconcile(args: dict) -> str | None:
    delta_id = str(args.get("delta_id", "")).strip()
    outcome = str(args.get("outcome", "")).strip()
    if not delta_id:
        return "icc_reconcile: 'delta_id' is required"
    if outcome not in ("confirm", "correct", "retract"):
        return f"icc_reconcile: 'outcome' must be confirm/correct/retract, got {outcome!r}"
    return None


def _validate_icc_crystallize(args: dict, sig: str) -> str | None:
    connector = str(args.get("connector", "")).strip()
    pipeline_name = str(args.get("pipeline_name", "")).strip()
    mode = str(args.get("mode", "")).strip()
    if not sig:
        return "icc_crystallize: 'intent_signature' is required"
    if not connector:
        return "icc_crystallize: 'connector' is required"
    if not _SAFE_SEG_RE.match(connector):
        return "icc_crystallize: 'connector' must be lowercase alphanumeric/underscore/dash, no path separators"
    if not pipeline_name:
        return "icc_crystallize: 'pipeline_name' is required"
    if ".." in pipeline_name or "/" in pipeline_name or "\\" in pipeline_name:
        return "icc_crystallize: 'pipeline_name' cannot contain '..', '/', or '\\'"
    if mode not in ("read", "write"):
        return f"icc_crystallize: 'mode' must be read or write, got {mode!r}"
    return None


def _validate_icc_span_open(args: dict, sig: str) -> str | None:
    if not sig:
        return (
            "icc_span_open: 'intent_signature' is required "
            "(e.g. 'lark.read.get-doc'). "
            "Format: <connector>.(read|write).<name>"
        )
    if not _SIG_RE.match(sig):
        return (
            f"icc_span_open: intent_signature {sig!r} is invalid. "
            "Must be <connector>.(read|write).<name> — e.g. 'lark.read.get-doc'."
        )
    return None


def _validate_icc_span_close(args: dict) -> str | None:
    outcome = str(args.get("outcome", "")).strip()
    if outcome not in ("success", "failure", "aborted"):
        return f"icc_span_close: 'outcome' must be success/failure/aborted, got {outcome!r}"
    return None


def _validate_icc_span_approve(args: dict, sig: str) -> str | None:
    if not sig:
        return "icc_span_approve: 'intent_signature' is required"
    if not _SIG_RE.match(sig):
        return (
            f"icc_span_approve: intent_signature {sig!r} is invalid. "
            "Must be <connector>.(read|write).<name> — e.g. 'lark.read.get-doc'."
        )
    return None


def _validate_icc_goal_rollback(args: dict) -> str | None:
    if not str(args.get("target_event_id", "")).strip():
        return "icc_goal_rollback: 'target_event_id' is required"
    return None
```

- [ ] **Step 3: Run new validator tests**

```bash
python -m pytest tests/test_hooks_pre_tool_use.py -k "test_validate or test_normalize_sig" -q
```
Expected: all pass.

- [ ] **Step 4: Run full test suite to confirm no regressions**

```bash
python -m pytest tests -q --tb=short
```
Expected: all pass (605 + new validator tests).

- [ ] **Step 5: Commit**

```bash
git add hooks/pre_tool_use.py tests/test_hooks_pre_tool_use.py
git commit -m "feat: extract per-tool validators + _normalize_sig in pre_tool_use.py"
```

---

## Task 4: Refactor `main()` with dispatch table

**Files:**
- Modify: `hooks/pre_tool_use.py`

- [ ] **Step 1: Add `_build_output` and dispatch constants before `main()`**

Add after the validator functions:

```python
# Tools whose intent_signature must be normalized and validated.
_SIG_TOOLS: frozenset[str] = frozenset({
    "__icc_exec", "__icc_crystallize", "__icc_span_open", "__icc_span_approve",
})

# Maps tool suffix → validator function.
# Validators that take sig receive (args, sig); others receive (args,) only.
_SIG_VALIDATORS: dict[str, object] = {
    "__icc_exec":          _validate_icc_exec,
    "__icc_crystallize":   _validate_icc_crystallize,
    "__icc_span_open":     _validate_icc_span_open,
    "__icc_span_approve":  _validate_icc_span_approve,
}
_PLAIN_VALIDATORS: dict[str, object] = {
    "__icc_reconcile":     _validate_icc_reconcile,
    "__icc_span_close":    _validate_icc_span_close,
    "__icc_goal_rollback": _validate_icc_goal_rollback,
}


def _build_output(
    tool_name: str,
    suffix: str,
    arguments: dict,
    sig: str,
    sig_from: str | None,
    sig_to: str | None,
    error_msg: str | None,
) -> dict:
    """Build the hook JSON output given validation results."""
    if error_msg:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": error_msg,
            },
            "systemMessage": f"Tool call blocked by emerge PreToolUse validator: {error_msg}",
        }
    if suffix == "__icc_goal_rollback":
        target_event_id = str(arguments.get("target_event_id", "")).strip()
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
            },
            "systemMessage": (
                f"emerge: icc_goal_rollback to target_event_id={target_event_id!r}. "
                "This is irreversible — it will overwrite the active goal state. "
                "Confirm only if the user explicitly requested this rollback."
            ),
        }
    if suffix == "__icc_span_approve":
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
            },
            "systemMessage": (
                "icc_span_approve 将把 span skeleton 移动到正式 pipeline 目录并激活自动化执行路径。"
                "请确认批准此操作？"
            ),
        }
    if suffix == "__icc_hub" and arguments.get("action") == "resolve":
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
            },
            "systemMessage": "icc_hub resolve 将应用冲突解决方案，此操作不可撤销。请确认继续？",
        }
    if sig_to is not None:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "updatedInput": {"intent_signature": sig_to},
            },
            "systemMessage": (
                f"pre_tool_use: normalized intent_signature "
                f"from {sig_from!r} to {sig_to!r}"
            ),
        }
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": f"pre_tool_use: {tool_name} approved",
        }
    }
```

- [ ] **Step 2: Replace `main()` with the dispatch version**

Replace the entire `main()` function with:

```python
def main() -> None:
    payload_text = sys.stdin.read().strip()
    try:
        payload = json.loads(payload_text) if payload_text else {}
    except Exception:
        payload = {}

    tool_name = payload.get("tool_name", "")
    arguments = payload.get("tool_input", {}) or {}
    if not isinstance(arguments, dict):
        arguments = {}

    # Derive suffix: "mcp__plugin_emerge__icc_exec" → "__icc_exec"
    suffix = f"__{tool_name.rsplit('__', 1)[-1]}" if "__" in tool_name else ""

    # Normalize intent_signature for tools that carry one.
    sig = ""
    sig_from: str | None = None
    sig_to: str | None = None
    if suffix in _SIG_TOOLS:
        raw_sig = str(arguments.get("intent_signature", "")).strip()
        sig, sig_from, sig_to = _normalize_sig(raw_sig)

    # Validate.
    error_msg: str | None = None
    if suffix in _SIG_VALIDATORS:
        error_msg = _SIG_VALIDATORS[suffix](arguments, sig)  # type: ignore[operator]
    elif suffix in _PLAIN_VALIDATORS:
        error_msg = _PLAIN_VALIDATORS[suffix](arguments)  # type: ignore[operator]

    # Normalization only matters when validation passed.
    if error_msg is not None:
        sig_to = None

    print(json.dumps(_build_output(tool_name, suffix, arguments, sig, sig_from, sig_to, error_msg)))


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Run all pre_tool_use tests**

```bash
python -m pytest tests/test_hooks_pre_tool_use.py -v
```
Expected: all pass (including the new validator tests from Task 2).

- [ ] **Step 4: Run full test suite**

```bash
python -m pytest tests -q --tb=short
```
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add hooks/pre_tool_use.py
git commit -m "refactor: pre_tool_use.py — dispatch table + _build_output, main() 180→40 lines"
```

---

## Task 5: CC context signal quality

**Files:**
- Modify: `hooks/user_prompt_submit.py`
- Modify: `hooks/instructions_loaded.py`
- Modify: `scripts/emerge_daemon.py`

- [ ] **Step 1: Add nudge-flag guard in `user_prompt_submit.py`**

Find this block in `hooks/user_prompt_submit.py`:

```python
    # Every N turns: remind CC to open a span if none is active
    active_span_id = str(tracker.state.get("active_span_id", "") or "")
    if not active_span_id and turn_count > 1 and turn_count % _SPAN_REMINDER_INTERVAL == 0:
        reminder = (
            "[Span] No active span. "
            "If this turn involves tool use, open one first: "
            'icc_span_open(intent_signature="connector.mode.name").'
        )
        context_text = reminder + "\n\n" + context_text
```

Replace with:

```python
    # Every N turns: remind CC to open a span if none is active.
    # Skip turn-5 reminder when tool_audit already sent a one-shot nudge this session
    # (prevents double-message on the first few turns).
    active_span_id = str(tracker.state.get("active_span_id", "") or "")
    if not active_span_id and turn_count > 1 and turn_count % _SPAN_REMINDER_INTERVAL == 0:
        _skip_reminder = False
        if turn_count == _SPAN_REMINDER_INTERVAL:  # first reminder window only
            try:
                _raw = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
                _skip_reminder = bool(_raw.get("_span_nudge_sent"))
            except Exception:
                pass
        if not _skip_reminder:
            reminder = (
                "[Span] No active span. "
                "If this turn involves tool use, open one first: "
                'icc_span_open(intent_signature="connector.mode.name").'
            )
            context_text = reminder + "\n\n" + context_text
```

- [ ] **Step 2: Add clarifying comment in `instructions_loaded.py`**

Find this block in `hooks/instructions_loaded.py`:

```python
    # 3. Connector NOTES.md — if the file loaded is a .claude/rules/connector-*.md,
    #    also inject the live NOTES.md in case it was updated since session start.
    try:
        file_path = str(payload.get("file_path", ""))
        if "/rules/connector-" in file_path and file_path.endswith(".md"):
            name = Path(file_path).stem.removeprefix("connector-")
            notes_path = Path.home() / ".emerge" / "connectors" / name / "NOTES.md"
            if notes_path.exists():
                notes_text = notes_path.read_text(encoding="utf-8").strip()
                if notes_text:
                    parts.append(f"[Connector:{name} NOTES]\n{notes_text[:1200]}")
```

Replace the comment block with:

```python
    # 3. Connector NOTES.md — fires when CC lazily loads a .claude/rules/connector-*.md file.
    #    session_start.py writes those files with a 400-char stub; the stub's only purpose
    #    is to trigger this lazy load. Here we inject the full NOTES.md (up to 1200 chars)
    #    as the actual operational payload. The asymmetry (400 stub vs 1200 here) is
    #    intentional: the stub is a navigation hint, not a context payload.
    #    InstructionsLoaded fires at most once per rules file per session, so token cost
    #    is bounded to one injection per connector encountered during the session.
    try:
        file_path = str(payload.get("file_path", ""))
        if "/rules/connector-" in file_path and file_path.endswith(".md"):
            name = Path(file_path).stem.removeprefix("connector-")
            notes_path = Path.home() / ".emerge" / "connectors" / name / "NOTES.md"
            if notes_path.exists():
                notes_text = notes_path.read_text(encoding="utf-8").strip()
                if notes_text:
                    parts.append(f"[Connector:{name} NOTES]\n{notes_text[:1200]}")
```

- [ ] **Step 3: Add docstring to `_span_run_pipeline` in `emerge_daemon.py`**

Find this method in `scripts/emerge_daemon.py`:

```python
    def _span_run_pipeline(self, mode: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], str]:
        _rr = self._get_runner_router()
        _client = _rr.find_client(arguments) if _rr else None
        if _client is not None:
            return self._run_pipeline_remotely(mode, arguments, _client), "remote"
        if mode == "write":
            return self.pipeline.run_write(arguments), "local"
        return self.pipeline.run_read(arguments), "local"
```

Replace with:

```python
    def _span_run_pipeline(self, mode: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """Execute a pipeline for SpanHandlers and return (result_dict, execution_path).

        This method exists separately from _run_connector_pipeline because the two have
        different calling contracts:
        - _span_run_pipeline: used by SpanHandlers; returns a (result, path) tuple;
          does NOT record pipeline events (SpanHandlers calls record_pipeline_event itself).
        - _run_connector_pipeline: used by icc_exec; takes tool_name; records events
          internally; returns a full MCP response dict, not a bare result.

        Do NOT consolidate these — the different return types and recording semantics
        are load-bearing for the flywheel bridge and span promotion paths.
        """
        _rr = self._get_runner_router()
        _client = _rr.find_client(arguments) if _rr else None
        if _client is not None:
            return self._run_pipeline_remotely(mode, arguments, _client), "remote"
        if mode == "write":
            return self.pipeline.run_write(arguments), "local"
        return self.pipeline.run_read(arguments), "local"
```

- [ ] **Step 4: Run full test suite**

```bash
python -m pytest tests -q --tb=short
```
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add hooks/user_prompt_submit.py hooks/instructions_loaded.py scripts/emerge_daemon.py
git commit -m "fix: span nudge dedup in user_prompt_submit; document instructions_loaded injection contract; clarify _span_run_pipeline docstring"
```

---

## Task 6: Create `admin/shared.py`

**Files:**
- Create: `scripts/admin/shared.py`

- [ ] **Step 1: Create the file**

Create `scripts/admin/shared.py` with the three resolver helpers that are used by two or more of the admin sub-modules:

```python
"""Shared path resolvers for admin sub-modules.

Only functions used by two or more of control_plane / pipeline / api live here.
Module-specific helpers stay in their own module.
"""
from __future__ import annotations

import os
from pathlib import Path

import sys
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.policy_config import default_exec_root, pin_plugin_data_path_if_present  # noqa: E402


def _resolve_state_root() -> Path:
    """Return the daemon state root directory (EMERGE_STATE_ROOT or default)."""
    pin_plugin_data_path_if_present()
    return Path(
        os.environ.get("EMERGE_STATE_ROOT", str(default_exec_root()))
    ).expanduser().resolve()


def _resolve_repl_root() -> Path:
    """Return the base directory where session subdirectories are stored."""
    return _resolve_state_root()


def _resolve_connector_root() -> Path:
    """Return the user connector root (EMERGE_CONNECTOR_ROOT or ~/.emerge/connectors)."""
    from scripts.policy_config import resolve_connector_root
    return Path(resolve_connector_root())
```

- [ ] **Step 2: Verify it imports cleanly**

```bash
python -c "from scripts.admin.shared import _resolve_state_root, _resolve_repl_root, _resolve_connector_root; print('ok')"
```
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add scripts/admin/shared.py
git commit -m "feat: admin/shared.py — shared path resolvers extracted from api.py"
```

---

## Task 7: Create `admin/control_plane.py`

**Files:**
- Create: `scripts/admin/control_plane.py`
- Source: `scripts/admin/api.py` (move functions, do not delete from api.py yet)

Functions to move (cut from api.py, paste into control_plane.py):
- `_resolve_session_id`
- `_session_paths`
- `_load_hook_state_summary`
- `_span_policy_label`
- `cmd_control_plane_state`
- `cmd_control_plane_intents`
- `cmd_control_plane_session`
- `cmd_control_plane_hook_state`
- `cmd_control_plane_exec_events`
- `cmd_control_plane_tool_events`
- `cmd_control_plane_pipeline_events`
- `cmd_control_plane_spans`
- `cmd_control_plane_span_candidates`
- `cmd_control_plane_reflection_cache`
- `cmd_control_plane_monitors`
- `cmd_control_plane_delta_reconcile`
- `cmd_control_plane_risk_update`
- `cmd_control_plane_risk_add`
- `cmd_control_plane_policy_freeze`
- `cmd_control_plane_policy_unfreeze`
- `cmd_control_plane_session_export`
- `cmd_control_plane_session_reset`

- [ ] **Step 1: Create `scripts/admin/control_plane.py`**

```python
"""Control-plane read/write API functions.

All cmd_control_plane_* functions live here.
Imported helpers: _resolve_state_root, _resolve_repl_root from admin.shared.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

import sys
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.admin.shared import _resolve_repl_root, _resolve_state_root  # noqa: E402
from scripts.policy_config import (  # noqa: E402
    PROMOTE_MAX_HUMAN_FIX_RATE,
    PROMOTE_MIN_ATTEMPTS,
    PROMOTE_MIN_SUCCESS_RATE,
    PROMOTE_MIN_VERIFY_RATE,
    ROLLBACK_CONSECUTIVE_FAILURES,
    STABLE_MIN_ATTEMPTS,
    STABLE_MIN_SUCCESS_RATE,
    STABLE_MIN_VERIFY_RATE,
    atomic_write_json,
    default_hook_state_root,
    default_exec_root,
    derive_session_id,
    pin_plugin_data_path_if_present,
)
from scripts.state_tracker import StateTracker, load_tracker, save_tracker  # noqa: E402
```

Then paste all the control-plane functions listed above from `api.py` (keep their implementations exactly as-is, only update any references from `_resolve_state_root()` to use the imported one, and `_resolve_repl_root()` similarly).

- [ ] **Step 2: Verify the module imports cleanly**

```bash
python -c "from scripts.admin.control_plane import cmd_control_plane_state; print('ok')"
```
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add scripts/admin/control_plane.py
git commit -m "feat: admin/control_plane.py — extract all cmd_control_plane_* functions"
```

---

## Task 8: Create `admin/pipeline.py`

**Files:**
- Create: `scripts/admin/pipeline.py`
- Source: `scripts/admin/api.py` (move functions)

Functions to move:
- `_normalize_pipeline_key`
- `_load_registry`
- `_save_registry`
- `_normalize_intent_signature`
- `cmd_policy_status`
- `cmd_pipeline_delete`
- `cmd_pipeline_set`
- `cmd_connector_export`
- `cmd_connector_import`
- `cmd_normalize_intents`

- [ ] **Step 1: Create `scripts/admin/pipeline.py`**

```python
"""Pipeline, connector, and policy lifecycle operations."""
from __future__ import annotations

import json
import os
import re
import shutil
import zipfile
from pathlib import Path

import sys
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.admin.shared import _resolve_connector_root, _resolve_state_root  # noqa: E402
from scripts.policy_config import (  # noqa: E402
    PROMOTE_MAX_HUMAN_FIX_RATE,
    PROMOTE_MIN_ATTEMPTS,
    PROMOTE_MIN_SUCCESS_RATE,
    PROMOTE_MIN_VERIFY_RATE,
    ROLLBACK_CONSECUTIVE_FAILURES,
    STABLE_MIN_ATTEMPTS,
    STABLE_MIN_SUCCESS_RATE,
    STABLE_MIN_VERIFY_RATE,
    atomic_write_json,
    derive_profile_token,
    pin_plugin_data_path_if_present,
)
```

Then paste all the pipeline functions listed above from `api.py` (keep implementations exactly as-is).

- [ ] **Step 2: Verify**

```bash
python -c "from scripts.admin.pipeline import cmd_policy_status, cmd_pipeline_delete; print('ok')"
```
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add scripts/admin/pipeline.py
git commit -m "feat: admin/pipeline.py — extract pipeline/connector/policy operations"
```

---

## Task 9: Thin `admin/api.py` and add re-exports

**Files:**
- Modify: `scripts/admin/api.py`

- [ ] **Step 1: Remove moved functions from `api.py`**

Delete from `api.py` all functions that were moved to `control_plane.py` and `pipeline.py` (the full lists from Tasks 7 and 8). Keep:
- `_sse_clients`, `_sse_lock`, `_sse_broadcast`
- `_local_plugin_version`
- `_injected_runtime_basename`, `_cockpit_inject_html`, `_cockpit_list_injected_html`
- `cmd_status`, `cmd_clear`, `cmd_assets`
- `_validate_action`, `cmd_submit_actions`, `_enrich_actions`
- `_cmd_set_goal`, `_cmd_goal_history`, `_cmd_goal_rollback`
- `_cmd_save_settings`
- `render_policy_status_pretty`

Also remove `_resolve_state_root`, `_resolve_repl_root`, `_resolve_connector_root` from `api.py` — they now live in `shared.py`.

- [ ] **Step 2: Add imports from shared, control_plane, pipeline at top of `api.py`**

After existing imports in `api.py`, add:

```python
from scripts.admin.shared import (  # noqa: E402
    _resolve_state_root,
    _resolve_repl_root,
    _resolve_connector_root,
)
from scripts.admin.control_plane import (  # noqa: E402
    _resolve_session_id,
    _session_paths,
    _load_hook_state_summary,
    _span_policy_label,
    cmd_control_plane_state,
    cmd_control_plane_intents,
    cmd_control_plane_session,
    cmd_control_plane_hook_state,
    cmd_control_plane_exec_events,
    cmd_control_plane_tool_events,
    cmd_control_plane_pipeline_events,
    cmd_control_plane_spans,
    cmd_control_plane_span_candidates,
    cmd_control_plane_reflection_cache,
    cmd_control_plane_monitors,
    cmd_control_plane_delta_reconcile,
    cmd_control_plane_risk_update,
    cmd_control_plane_risk_add,
    cmd_control_plane_policy_freeze,
    cmd_control_plane_policy_unfreeze,
    cmd_control_plane_session_export,
    cmd_control_plane_session_reset,
)
from scripts.admin.pipeline import (  # noqa: E402
    _normalize_pipeline_key,
    _load_registry,
    _save_registry,
    _normalize_intent_signature,
    cmd_policy_status,
    cmd_pipeline_delete,
    cmd_pipeline_set,
    cmd_connector_export,
    cmd_connector_import,
    cmd_normalize_intents,
)
```

These re-exports ensure all callers of `from scripts.admin.api import <anything>` continue to work.

- [ ] **Step 3: Verify api.py imports cleanly and re-exports work**

```bash
python -c "
from scripts.admin.api import (
    cmd_control_plane_state, cmd_policy_status, cmd_pipeline_delete,
    cmd_submit_actions, _resolve_state_root, _session_paths,
    render_policy_status_pretty,
)
print('all re-exports ok')
"
```
Expected: `all re-exports ok`

- [ ] **Step 4: Run full test suite**

```bash
python -m pytest tests -q --tb=short
```
Expected: all pass. If `test_cockpit_api.py` fails with patch targets, proceed to Task 10.

- [ ] **Step 5: Commit**

```bash
git add scripts/admin/api.py
git commit -m "refactor: thin admin/api.py — re-exports from control_plane + pipeline sub-modules"
```

---

## Task 10: Update `test_cockpit_api.py` patch targets

**Files:**
- Modify: `tests/test_cockpit_api.py`

The tests patch functions in the module where they are *used*, not where they are *defined*. After the split, `cmd_control_plane_*` functions live in `control_plane.py` and look up `_resolve_state_root`, `default_hook_state_root`, `_session_paths`, and `time` from their own module's namespace.

- [ ] **Step 1: Update all patch targets in `test_cockpit_api.py`**

Make these replacements throughout the file:

| Old patch target | New patch target |
|---|---|
| `scripts.admin.api.default_hook_state_root` | `scripts.admin.control_plane.default_hook_state_root` |
| `scripts.admin.api._resolve_state_root` | `scripts.admin.control_plane._resolve_state_root` |
| `scripts.admin.api._session_paths` | `scripts.admin.control_plane._session_paths` |
| `scripts.admin.api.time.time` | `scripts.admin.control_plane.time.time` |

Example — test at line 23:
```python
# Before:
with patch("scripts.admin.api.default_hook_state_root", return_value=str(tmp_path)):
# After:
with patch("scripts.admin.control_plane.default_hook_state_root", return_value=str(tmp_path)):
```

Apply to all 8 patch calls in the file (lines 23, 38, 50, 59, 75, 97, 117, 132, 153, 154).

- [ ] **Step 2: Run `test_cockpit_api.py` alone**

```bash
python -m pytest tests/test_cockpit_api.py -v
```
Expected: all pass.

- [ ] **Step 3: Run full test suite**

```bash
python -m pytest tests -q --tb=short
```
Expected: all pass (605 + new validator tests).

- [ ] **Step 4: Verify line counts reflect the split**

```bash
wc -l scripts/admin/api.py scripts/admin/control_plane.py scripts/admin/pipeline.py scripts/admin/shared.py
```
Expected: `api.py` ≈ 500 lines (was 1388), `control_plane.py` ≈ 600 lines, `pipeline.py` ≈ 300 lines, `shared.py` ≈ 40 lines.

- [ ] **Step 5: Final commit**

```bash
git add tests/test_cockpit_api.py
git commit -m "fix: update test_cockpit_api.py patch targets to admin.control_plane namespace after split"
```

---

## Final Verification

- [ ] **Run complete test suite**

```bash
python -m pytest tests -q
```
Expected: all pass (≥605).

- [ ] **Update memory audit record**

Update `/Users/apple/.claude/projects/-Users-apple-Documents-workspace-emerge/memory/project_emerge_audit_cc_optimal.md` — add the Section 1–4 items as completed, update test count.
