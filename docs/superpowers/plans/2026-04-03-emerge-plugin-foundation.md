# Emerge Plugin Foundation Implementation Plan

**Goal:** Build a runnable Emerge Claude Code plugin skeleton that realizes the minimal model in spec: `read/write/bash` + A-track pipeline evolution + state-delta context compression.

**Source grounding (from Claude Code internals already verified):**
- `PostToolUse` can inject `additionalContext` and can override MCP output via `updatedMCPToolOutput`.
- `PreCompact` uses plain stdout as compaction instructions, not `additionalContext`.
- Plugin MCP tools are namespaced in normalized tool IDs (match by robust regex for plugin server names).
- Hook commands can use `CLAUDE_PLUGIN_ROOT` / `CLAUDE_PLUGIN_DATA`, and `CLAUDE_ENV_FILE` for session env export.

**Tech stack:** Claude Code plugin format, Python 3.11+, pytest, YAML (`pyyaml`), stdio MCP server.

---

## Task 1 - Bootstrap Emerge plugin static skeleton

**Files**
- Create: `.claude-plugin/plugin.json`
- Create: `.mcp.json`
- Create: `hooks/hooks.json`
- Create: `tests/test_plugin_static_config.py`

**Implementation notes**
- Plugin `name` must be `emerge`.
- MCP server key can remain `core`; tools exposed by daemon are `icc_read`, `icc_write`, `icc_exec`.
- `PostToolUse.matcher` should match namespaced plugin MCP tools, e.g.:
  - `mcp__plugin_.*emerge.*__icc_(read|write|exec)`

**Test command**
- `pytest tests/test_plugin_static_config.py -v`

**Acceptance**
- Static files exist, parse as JSON, and contain required keys.

---

## Task 2 - Implement persistent REPL path (`icc_exec`)

**Files**
- Create: `scripts/repl_state.py`
- Create: `scripts/repl_daemon.py`
- Create: `tests/test_repl_daemon_exec.py`

**Implementation notes**
- `ReplState` stores shared globals across calls.
- `icc_exec` executes Python code in persistent context and returns stdout/stderr in MCP response content.
- Errors are explicit (`isError: true` equivalent behavior in response).

**Test command**
- `pytest tests/test_repl_daemon_exec.py -v`

**Acceptance**
- Variables persist between consecutive `exec` calls in same daemon runtime.

---

## Task 3 - Implement A-track pipeline engine for `icc_read`/`icc_write`

**Files**
- Create: `scripts/pipeline_engine.py`
- Create: `connectors/mock/pipelines/read/layers.yaml`
- Create: `connectors/mock/pipelines/read/layers.py`
- Create: `connectors/mock/pipelines/write/add-wall.yaml`
- Create: `connectors/mock/pipelines/write/add-wall.py`
- Create: `tests/test_pipeline_engine.py`

**Implementation notes**
- Keep pipeline shape minimal:
  - `intent_signature`
  - `read_steps[]` / `write_steps[]`
  - `verify_steps[]`
  - `rollback_or_stop_policy`
- Runtime loads YAML metadata + Python action module.
- `run_read` and `run_write` are deterministic and testable.

**Test command**
- `pytest tests/test_pipeline_engine.py -v`

**Acceptance**
- Mock read returns structured rows.
- Mock write runs action + verify path and returns verified result.

---

## Task 4 - Implement state-delta tracker and hook output contracts

**Files**
- Create: `scripts/state_tracker.py`
- Create: `hooks/session_start.py`
- Create: `hooks/user_prompt_submit.py`
- Create: `hooks/post_tool_use.py`
- Create: `hooks/pre_compact.py`
- Create: `tests/test_hook_scripts_output.py`

**Implementation notes**
- Follow minimal context injection contract (3 segments):
  - `Goal`
  - `Delta`
  - `Open Risks`
- `SessionStart` and `UserPromptSubmit` output JSON with `hookSpecificOutput.additionalContext`.
- `PostToolUse` outputs delta summary in `additionalContext`.
- `PreCompact` returns plain instruction text on stdout.

**Test command**
- `pytest tests/test_hook_scripts_output.py -v`

**Acceptance**
- Hook outputs are parseable and event names are correct.
- `PreCompact` returns plain text instructions.

---

## Task 5 - Wire MCP calls end-to-end (`icc_read`/`icc_write`/`icc_exec`)

**Files**
- Modify: `scripts/repl_daemon.py`
- Modify: `scripts/pipeline_engine.py` (if needed for argument normalization)
- Create: `tests/test_mcp_tools_integration.py`

**Implementation notes**
- `tools/call` routes:
  - `icc_exec` -> `ReplState.exec_code`
  - `icc_read` -> `PipelineEngine.run_read`
  - `icc_write` -> `PipelineEngine.run_write`
- Keep response payload stable for hooks and post-processing.

**Test command**
- `pytest tests/test_mcp_tools_integration.py -v`

**Acceptance**
- All three tool paths run in same runtime and produce expected outputs.

---

## Task 6 - Add context-budget policy to reduce token usage

**Files**
- Modify: `scripts/state_tracker.py`
- Modify: `hooks/post_tool_use.py`
- Modify: `hooks/user_prompt_submit.py`
- Create: `tests/test_context_budgeting.py`

**Implementation notes**
- Classify delta into:
  - `Core Critical` (never trimmed)
  - `Core Secondary` (aggregatable)
  - `Peripheral` (first to trim)
- Enforce trim order:
  1. drop peripheral details
  2. aggregate secondary
  3. keep core critical verbatim
- Output always follows `Goal / Delta / Open Risks`.

**Test command**
- `pytest tests/test_context_budgeting.py -v`

**Acceptance**
- Under budget pressure, output shrinks while preserving core critical changes.

---

## Task 7 - Add degrade/reconcile behavior for async consistency window

**Files**
- Modify: `scripts/state_tracker.py`
- Modify: `hooks/post_tool_use.py`
- Create: `tests/test_degrade_reconcile.py`

**Implementation notes**
- Add `consistency_window_ms` handling:
  - provisional delta before window closes
  - reconcile after window (confirm/correct/retract)
- If event/state evidence mismatches, mark `verification_state=degraded`.
- In degraded state, block auto-chain high-risk writes (policy guard only; no full policy engine yet).

**Test command**
- `pytest tests/test_degrade_reconcile.py -v`

**Acceptance**
- Mismatch enters degraded deterministically and can recover on reconcile.

---

## Cross-task verification (must run)

- `pytest tests/test_plugin_static_config.py -v`
- `pytest tests/test_repl_daemon_exec.py -v`
- `pytest tests/test_pipeline_engine.py -v`
- `pytest tests/test_hook_scripts_output.py -v`
- `pytest tests/test_mcp_tools_integration.py -v`
- `pytest tests/test_context_budgeting.py -v`
- `pytest tests/test_degrade_reconcile.py -v`

Optional final sweep:
- `pytest tests -v`

---

## Delivery checkpoints

1. **Foundation ready**
   - Plugin boots, daemon responds, read/write/exec all callable.
2. **A-track ready**
   - At least 2 stable mock pipelines with verify path.
3. **Delta context ready**
   - Hook-injected context follows 3-section minimal format.
4. **Token discipline ready**
   - Budget policy trims non-core details first.
5. **Safety ready**
   - Degraded path explicit, recoverable, and test-covered.

---

## Out of scope (this plan intentionally excludes)

- Multi-domain generalized adapter framework
- Production CAD/Trading connector SDK integration
- UI productization and brand assets

This plan only implements the minimal Emerge kernel path that can be validated locally and then iterated.
