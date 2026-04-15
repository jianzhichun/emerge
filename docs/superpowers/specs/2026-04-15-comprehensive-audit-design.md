# Comprehensive Audit — Optimal Implementation Design

**Date:** 2026-04-15  
**Scope:** Bug fixes, structural refactoring, CC context signal quality, admin split  
**Goal:** Every component that runs in CC's hot path is correct, maintainable, and signal-efficient.

---

## Background

emerge is a CC flywheel system: hooks are CC's perception layer, daemon is the decision layer, flywheel is the memory layer. This audit aims at full optimal implementation — no bugs in the hot path, clean structure for future extension, and CC context injections that are precise and non-redundant.

---

## Section 1: Bug Fixes

### 1a. `pre_tool_use.py` — regex antipatterns

**Problem:** The intent_signature regex `r'^[a-z][a-z0-9_-]*\.(read|write)\.[a-z][a-z0-9_./-]*$'` is compiled inline three separate times (lines 63, 125, 147). `import re` is deferred inside three separate `elif` branches. Line 98 uses `__import__("re").compile(...)` antipattern.

**Fix:** Extract module-level constants:
```python
import re
_SIG_RE      = re.compile(r'^[a-z][a-z0-9_-]*\.(read|write)\.[a-z][a-z0-9_./-]*$')
_SAFE_SEG_RE = re.compile(r'^[a-z0-9][a-z0-9_-]*$')
_VAR_RE      = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
```
Remove all deferred `import re as _re` inside branches.

### 1b. `observer_plugin.py` — `__import__` antipattern

**Problem:** `_SAFE_NAME_RE = __import__("re").compile(r'^[a-z0-9][a-z0-9_-]*$')` at class body level.

**Fix:** `import re` at module top; `_SAFE_NAME_RE = re.compile(...)` at class body.

### 1c. `emerge_daemon.py` — redundant emit field

**Problem:** `self._sink.emit("flywheel.bridge.promoted", {"key": base_pipeline_id, "pipeline_id": base_pipeline_id})` — both `key` and `pipeline_id` hold the same value.

**Fix:** Remove `key` field: `{"pipeline_id": base_pipeline_id}`.

### 1d. Dead comment in `pre_tool_use.py`

**Problem:** Line 29 has a floating comment `# icc_read / icc_write are fully deleted. Use icc_span_open for bridge execution.` inside `main()` before any tool check. This belongs in `schemas.py` (which already has it), not in the validation hot path.

**Fix:** Remove the comment from `pre_tool_use.py:main()`.

---

## Section 2: `pre_tool_use.py` Structural Refactor

### Problem

224-line `main()` is a monolithic `if/elif` validation chain. Adding a new tool's validation requires reading the entire function. Inline regex makes each branch harder to unit-test.

### Design

**Module-level regex constants** (from Section 1).

**Per-tool validator functions** — each returns `str | None` (error message or None):

```python
def _normalize_sig(raw: str) -> tuple[str, str | None, str | None]:
    """Lowercase-normalize intent_signature. Returns (normalized, from_raw, to_norm).
    from_raw/to_norm are None if no change needed."""

def _validate_icc_exec(args: dict, sig: str) -> str | None: ...
def _validate_icc_reconcile(args: dict) -> str | None: ...
def _validate_icc_crystallize(args: dict, sig: str) -> str | None: ...
def _validate_icc_span_open(args: dict, sig: str) -> str | None: ...
def _validate_icc_span_close(args: dict) -> str | None: ...
def _validate_icc_span_approve(args: dict, sig: str) -> str | None: ...
def _validate_icc_goal_rollback(args: dict) -> str | None: ...
```

**Dispatch table:**
```python
_SIG_TOOLS = frozenset({
    "__icc_exec", "__icc_crystallize",
    "__icc_span_open", "__icc_span_approve",
})

_VALIDATORS: dict[str, Callable] = {
    "__icc_exec":           _validate_icc_exec,
    "__icc_reconcile":      _validate_icc_reconcile,
    "__icc_crystallize":    _validate_icc_crystallize,
    "__icc_span_open":      _validate_icc_span_open,
    "__icc_span_close":     _validate_icc_span_close,
    "__icc_span_approve":   _validate_icc_span_approve,
    "__icc_goal_rollback":  _validate_icc_goal_rollback,
}
```

**`main()` becomes ~40 lines:**
1. Parse payload
2. Extract tool suffix (`tool_name.rsplit("__", 1)[-1]` with `__` prefix for dict lookup)
3. Normalize `intent_signature` if tool is in `_SIG_TOOLS`
4. Dispatch to validator → `error_msg`
5. Build output: `deny` if `error_msg`, else tool-specific `ask` / `allow` / `additionalContext`

**Tool-specific output cases** (non-error, non-standard-allow):
- `icc_goal_rollback` → `permissionDecision: ask` with irreversibility warning
- `icc_span_approve` → `permissionDecision: ask` with confirmation prompt
- `icc_hub` with `action=resolve` → `permissionDecision: ask`
- sig normalization → `permissionDecision: allow` + `updatedInput`
- all others → `additionalContext: approved`

These are expressed as a small `_build_output()` function after validation.

### Result

`main()` 180→40 lines. Each validator independently testable. Adding a new tool: add one function + one dict entry.

---

## Section 3: CC Context Signal Quality

### 3a. Span nudge deduplication

**Problem:** `tool_audit.py` fires a one-shot nudge (stored in `_SPAN_NUDGE_FLAG` in `state.json`) when the first non-trivial tool runs without a span. `user_prompt_submit.py` fires a reminder every 5 turns independently. In the first 5 turns, both can fire — user sees two similar messages.

**Fix:** In `user_prompt_submit.py`, before emitting the turn-5 reminder, check `_SPAN_NUDGE_FLAG` in `state.json`. If already set (nudge already sent this session), skip the turn-5 reminder — the nudge already educated the user. The turn-10, turn-15 etc. reminders still fire normally (nudge is a one-shot; reminders are for long sessions that still haven't opened spans).

```python
# user_prompt_submit.py
if not active_span_id and turn_count > 1 and turn_count % _SPAN_REMINDER_INTERVAL == 0:
    # Skip if tool_audit already sent a nudge this session
    try:
        _raw = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        if not _raw.get("_span_nudge_sent"):
            reminder = "..."
            context_text = reminder + "\n\n" + context_text
    except Exception:
        pass
```

### 3b. `instructions_loaded.py` NOTES injection — add intent comment

**Problem:** `session_start.py` writes connector rules files with 400-char excerpt; `instructions_loaded.py` injects full NOTES up to 1200 chars. The discrepancy is intentional (rules file = navigation stub; InstructionsLoaded = full operational context on first access) but undocumented.

**Fix:** Add a comment in `instructions_loaded.py` explaining the 1200 vs 400 asymmetry — the 400-char stub in the rules file exists only to trigger the lazy load; the 1200-char injection here is the actual payload.

### 3c. `_span_run_pipeline` / `_run_connector_pipeline` — clarify non-overlap

**Problem:** Both methods do runner resolution + local/remote dispatch. Risk of future accidental consolidation that breaks the different return types / recording semantics.

**Fix:** Add docstring to `_span_run_pipeline` explicitly stating:
- Returns `(result_dict, execution_path_str)` tuple (SpanHandlers API)
- Does NOT record pipeline events (SpanHandlers does that via `record_pipeline_event` callback)
- Must NOT be replaced by `_run_connector_pipeline` (different signature + different caller contract)

---

## Section 4: `admin/api.py` Split

### Problem

1388-line single file containing: SSE broadcast, status/clear, policy lifecycle, connector export/import, cockpit actions, all control-plane read endpoints, all control-plane write endpoints, goal endpoints, settings, HTML injection helpers.

### Design

Split into three focused modules + thin orchestration layer:

**`admin/control_plane.py`** (~600 lines) — all `cmd_control_plane_*` read and write functions:
- `cmd_control_plane_state`, `cmd_control_plane_intents`, `cmd_control_plane_session`
- `cmd_control_plane_hook_state`, `cmd_control_plane_exec_events`, `cmd_control_plane_tool_events`
- `cmd_control_plane_pipeline_events`, `cmd_control_plane_spans`, `cmd_control_plane_span_candidates`
- `cmd_control_plane_reflection_cache`, `cmd_control_plane_monitors`
- `cmd_control_plane_delta_reconcile`, `cmd_control_plane_risk_update`, `cmd_control_plane_risk_add`
- `cmd_control_plane_policy_freeze`, `cmd_control_plane_policy_unfreeze`
- `cmd_control_plane_session_export`, `cmd_control_plane_session_reset`

**`admin/pipeline.py`** (~300 lines) — pipeline/connector/policy operations:
- `cmd_policy_status`, `cmd_pipeline_delete`, `cmd_pipeline_set`
- `cmd_connector_export`, `cmd_connector_import`, `cmd_normalize_intents`
- `_normalize_pipeline_key`, `_load_registry`, `_save_registry`, `_normalize_intent_signature`

**`admin/api.py`** (~500 lines, was 1388) — thin orchestration + remaining:
- SSE: `_sse_broadcast`, `_SSE_CLIENTS`
- Status/clear/assets: `cmd_status`, `cmd_clear`, `cmd_assets`
- Actions: `cmd_submit_actions`, `_validate_action`, `_enrich_actions`
- Goal: `_cmd_set_goal`, `_cmd_goal_history`, `_cmd_goal_rollback`
- Settings: `_cmd_save_settings`
- HTML injection helpers: `_cockpit_inject_html`, `_cockpit_list_injected_html`
- Shared helpers: `_local_plugin_version`, `_resolve_state_root`, `_resolve_repl_root`, etc.
- **Re-exports** from `control_plane` and `pipeline` modules for backward compat

### Backward compatibility

`admin/api.py` re-exports all moved symbols:
```python
from scripts.admin.control_plane import (
    cmd_control_plane_state, cmd_control_plane_intents, ...
)
from scripts.admin.pipeline import (
    cmd_policy_status, cmd_pipeline_delete, ...
)
```

`repl_admin.py` import path `from scripts.admin.api import ...` unchanged.
Tests that import directly from `admin.api` continue to work via re-exports.
Tests that can be updated to import from the sub-module directly will be updated.

---

## Testing

- All existing 605 tests must pass after each section.
- New unit tests for each `_validate_icc_*` function in `pre_tool_use.py` (currently not independently testable).
- Section 4: verify `test_repl_admin.py` still passes (imports via re-export).

## Implementation Order

1. Section 1 (bugs) — apply and verify tests
2. Section 2 (pre_tool_use refactor) — apply and verify tests
3. Section 3 (signal quality) — apply and verify tests
4. Section 4 (admin split) — apply and verify tests, update direct imports in test files

Each section is independently shippable.
