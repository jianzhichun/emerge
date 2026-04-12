# Emerge CC Hook Coverage & Code Optimal Design

**Goal:** Leverage the full Claude Code 2025 hook lifecycle in emerge, fix the `_write_json` static method bug, complete the notification migration batch, and eliminate the watch-script duplication.

**Architecture:** Three independent layers — (1) foundation fix + batch commit, (2) three new hooks that close the CC lifecycle gap, (3) watch-file DRY extraction. Each layer is independently testable and shippable.

**Tech Stack:** Python 3.11+, Claude Code hooks (JSON/subprocess), emerge MCP plugin, pytest

---

## Context

### What emerge is

A **learning layer** on Claude Code. Every `icc_exec` call teaches the flywheel; once a pattern reaches `stable`, LLM inference is bypassed entirely. Hooks are the nervous system — they inject goal/delta context, enforce conventions, and guard state integrity at session boundaries.

### What the CC 2025 hook lifecycle adds

CC now fires: `StopFailure`, `TaskCompleted`, `SubagentStart` (in addition to the existing set). emerge uses none of these yet. The gaps:

| CC Event | Gap |
|----------|-----|
| `StopFailure` | Active spans are left dangling when CC exits due to rate_limit/auth/billing errors |
| `TaskCompleted` | Agent-team tasks can complete while a span is open (no safety guard) |
| `SubagentStart` | Subagents don't know they are inside a parent span and may accidentally close it |

### Current notification architecture (post-migration)

`notifications/claude/channel` is silently dropped for plugin MCP servers. All notification paths now use file-based alternatives:
- **Cockpit actions**: `watch_pending.py` Monitor + `UserPromptSubmit` hook fallback
- **Pattern alerts**: `watch_patterns.py` Monitor reads `pattern-alerts.json`
- **Bridge failures**: `_last_bridge_failure` instance variable → injected by `icc_exec`
- **Span skeleton ready**: `icc_span_close` response → PostToolUse hook injects reminder

---

## Layer 1: Foundation — Bug Fix + Batch Commit

### 1.1 `_write_json` static method bug

**File:** `scripts/emerge_daemon.py`

The uncommitted diff removed `@staticmethod` from `_write_json`. All callers use `self._write_json(path, data)` — without the decorator, `self` maps to `path`, causing TypeError at runtime.

**Fix:**
```python
@staticmethod
def _write_json(path: Path, data: dict[str, Any]) -> None:
    from scripts.policy_config import atomic_write_json
    atomic_write_json(path, data)
```

Type annotations restored. No other changes to the method body.

### 1.2 Batch commit scope

9 files with clean uncommitted changes after the bug fix:
- `CLAUDE.md` — notification architecture documentation update
- `commands/cockpit.md` — add Monitor 2 (watch_patterns.py) to cockpit launch steps
- `scripts/emerge_daemon.py` — `_write_json` fix + `_notify` → file-based alternatives
- `scripts/hub_config.py` — remove local `_atomic_write_json`, use `atomic_write_json`
- `scripts/policy_config.py` — add `atomic_write_json` function
- `scripts/repl_admin.py` — remove local `_atomic_write_json`, use canonical
- `scripts/span_tracker.py` — remove local `_atomic_write`, use canonical
- `skills/operator-monitor-debug/SKILL.md` — update for file-based pattern alerts
- `tests/test_mcp_tools_integration.py` — align tests with file-based notification

Also add `scripts/watch_patterns.py` (new untracked file).

**Commit message:** `chore: complete atomic-write migration and file-based notification paths`

---

## Layer 2: New Hook Coverage

### 2.1 `StopFailure` hook — auto-abort active span on error

**New file:** `hooks/stop_failure.py`

**When it fires:** CC encounters `rate_limit`, `authentication_failed`, `billing_error`, `invalid_request`, `server_error`, `max_output_tokens`, or `unknown` error.

**No decision control** — cannot block the error. Goal is cleanup.

**Behavior:**
1. Read `state.json` from `default_hook_state_root()`
2. If `active_span_id` is present:
   - Pop `active_span_id` and `active_span_intent` from `state.json`, save via `atomic_write_json`
   - Emit `systemMessage` noting the span was aborted and the error type
   - (span-candidates.json cleanup is handled by `SessionStart` on the next session — no action needed here)
3. If no active span: emit `{}`

**Output contract:** Top-level `systemMessage` (not `hookSpecificOutput` — same rule as `SessionEnd`, `Stop`, etc.)

```python
# output when span found:
{"systemMessage": "StopFailure (rate_limit): span span-xyz aborted automatically."}

# output when no span:
{}
```

**hooks.json entry:**
```json
"StopFailure": [
  {
    "matcher": ".*",
    "hooks": [
      {
        "type": "command",
        "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/stop_failure.py",
        "timeout": 10
      }
    ]
  }
]
```

**Test:** `test_hook_scripts_output.py::test_stop_failure_aborts_active_span` — write state.json with active span, run hook, verify span aborted in state.json and systemMessage present.

### 2.2 `TaskCompleted` hook — span safety guard for agent teams

**New file:** `hooks/task_completed.py`

**When it fires:** `TaskUpdate` marks a task `completed`, OR an agent-team teammate finishes its turn with in-progress tasks.

**Decision control:** Exit code 2 + stderr → task not marked complete, CC feeds stderr back as model feedback.

**Behavior:**
1. Read `state.json`
2. If `active_span_id` present:
   - Write error message to stderr
   - `sys.exit(2)` — blocks task completion
3. If no active span: `print("{}")` + exit 0

**Output contract:** No JSON — raw stderr text for exit-2 path. `{}` for pass-through.

```
# stderr when blocking:
Active span <span_id> is open (<intent>). Call icc_span_close(outcome='aborted') before marking this task complete.
```

**hooks.json entry** (no matcher for TaskCompleted per CC docs):
```json
"TaskCompleted": [
  {
    "hooks": [
      {
        "type": "command",
        "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/task_completed.py",
        "timeout": 10
      }
    ]
  }
]
```

**Test:** `test_hook_scripts_output.py::test_task_completed_blocks_when_span_open` — write state.json with active span, run hook subprocess, verify exit code 2 and stderr message.

### 2.3 `SubagentStart` hook — inject parent span context

**New file:** `hooks/subagent_start.py`

**When it fires:** A subagent is dispatched from the main session.

**Purpose:** Subagent PostToolUse hooks already read `active_span_id` from the shared `state.json`, so span WAL recording works without this hook. This hook adds a *guardrail*: tell the subagent it must not call `icc_span_close` (the parent session owns the span lifecycle).

**Behavior:**
1. Read `state.json`
2. If `active_span_id` present:
   - Output `systemMessage` with span ID, intent, and the ownership rule
3. If no active span: emit `{}`

**Output contract:** Top-level `systemMessage`

```python
# output when span found:
{"systemMessage": "Active span span-xyz (test.read.op) is open from the parent session. Do NOT call icc_span_close — the parent session manages this span's lifecycle."}
```

**hooks.json entry:**
```json
"SubagentStart": [
  {
    "matcher": ".*",
    "hooks": [
      {
        "type": "command",
        "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/subagent_start.py",
        "timeout": 10
      }
    ]
  }
]
```

**Test:** `test_hook_scripts_output.py::test_subagent_start_injects_span_context` — write state.json with active span, run hook, verify systemMessage contains span ID. Also test no-span path returns `{}`.

### 2.4 CLAUDE.md invariant update

The Key Invariants section must be updated to document:
- `StopFailure` hook behavior and `systemMessage` output contract
- `TaskCompleted` hook: exit code 2 + stderr for block, `{}` for pass-through
- `SubagentStart` hook: `systemMessage` when span active
- `hooks.json` matchers line updated to include all three new hooks

---

## Layer 3: Watch File DRY

### 3.1 Extract `scripts/watch_file.py`

`watch_pending.py` and `watch_patterns.py` share ~80 lines of identical structure: signal handler, poll loop, timestamp dedup, rename, sleep 0.5s. Extract to a single `run_watcher` function.

**New file:** `scripts/watch_file.py`

```python
def run_watcher(
    path: Path,
    formatter,           # (data: dict) -> str | None  — return None to skip
    rename_suffix: str = ".processed.json",
    sleep_s: float = 0.5,
) -> None:
    """Generic file watcher: poll path, format, print, rename."""
```

Contract:
- `formatter(data)` returns the string to print, or `None` to skip (e.g. empty actions list)
- Handles `SIGTERM`/`SIGINT` cleanly
- Deduplicates by `submitted_at` timestamp
- Renames to `path.parent / (path.stem + rename_suffix)` after processing

### 3.2 Refactor `watch_pending.py`

Becomes ~25 lines:
```python
from scripts.watch_file import run_watcher
from scripts.pending_actions import format_pending_actions

def _fmt(data):
    actions = data.get("actions", [])
    return format_pending_actions(actions) if actions else None

run_watcher(_state_root() / "pending-actions.json", _fmt)
```

`_state_root()` helper stays in the file for direct-execution path resolution.

### 3.3 Refactor `watch_patterns.py`

Becomes ~25 lines:
```python
from scripts.watch_file import run_watcher
from scripts.pending_actions import format_pattern_alert

run_watcher(_state_root() / "pattern-alerts.json", format_pattern_alert)
```

### 3.4 Add `format_pattern_alert` to `scripts/pending_actions.py`

Move the inline formatting from `watch_patterns.py` into `pending_actions.py` alongside `format_pending_actions`:

```python
def format_pattern_alert(data: dict) -> str | None:
    """Format a pattern-alerts.json payload into a human-readable Monitor line."""
    stage = data.get("stage", "?")
    sig = data.get("intent_signature", "?")
    message = data.get("message", "")
    meta = data.get("meta", {})
    lines = [f"[OperatorMonitor] Pattern alert (stage={stage}):"]
    if message:
        lines.append(message)
    if meta:
        lines.append(
            f"  occurrences={meta.get('occurrences', '?')} "
            f"window={meta.get('window_minutes', '?')}min "
            f"machines={meta.get('machine_ids', [])}"
        )
    return "\n".join(lines)
```

**Tests:** `test_hook_scripts_output.py::test_format_pattern_alert_*` covering stage/sig/meta fields; `test_run_watcher_calls_formatter` using a tmp file.

---

## File Map

| File | Action |
|------|--------|
| `scripts/emerge_daemon.py` | Fix `@staticmethod` on `_write_json` |
| `scripts/policy_config.py` | +`atomic_write_json` (already in working tree) |
| `scripts/hub_config.py` | remove local `_atomic_write_json` (already in working tree) |
| `scripts/span_tracker.py` | remove local `_atomic_write` (already in working tree) |
| `scripts/repl_admin.py` | remove local `_atomic_write_json` (already in working tree) |
| `scripts/watch_patterns.py` | new → thin wrapper using `watch_file.run_watcher` |
| `scripts/watch_file.py` | **new** — shared poll loop |
| `scripts/pending_actions.py` | +`format_pattern_alert` |
| `hooks/stop_failure.py` | **new** — auto-abort span on CC error exit |
| `hooks/task_completed.py` | **new** — span safety guard for agent teams |
| `hooks/subagent_start.py` | **new** — inject parent span guardrail |
| `hooks/hooks.json` | +`StopFailure`, `TaskCompleted`, `SubagentStart` entries |
| `CLAUDE.md` | update notification docs + hooks.json matchers invariant |
| `commands/cockpit.md` | already updated (in working tree) |
| `tests/test_hook_scripts_output.py` | +5 new tests (one per new hook + 2 for watcher) |

---

## CLAUDE.md Invariants to Update

1. **hooks.json hook matchers** line: add `StopFailure`, `TaskCompleted`, `SubagentStart`
2. **Hook output schema** line: add `TaskCompleted` exit-code-2 + stderr contract note
3. **Notification delivery** section: no change needed (already updated in working tree)
4. **Stop/SubagentStop hooks** line: note that `StopFailure` and `TaskCompleted` provide complementary coverage

---

## What is NOT in scope

- `InstructionsLoaded` hook: CC docs don't specify an output schema for context injection; verify before implementing
- `Notification` hook observation: passive / informational only, no emerge action needed
- Generalizing event dispatch into a `NotifyDispatch` layer: YAGNI at current scale
- Any change to the flywheel policy lifecycle or span bridge
