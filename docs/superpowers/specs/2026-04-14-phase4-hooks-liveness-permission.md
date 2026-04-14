# Phase 4: Hooks — Watcher Liveness + Permission Denied + Cleanup

## Context

emerge is a CC flywheel — operators learn → patterns crystallize → zero-LLM pipelines execute.
Phase 3 (Plan 3) completed MCP 2026 feature adoption. A fresh audit against CC's current hook
surface (2026-04-14, context7) revealed three gaps not covered by any prior plan.

## Goals

1. **TeammateIdle hook** — agents-team watcher agents silently go idle after processing a
   pattern alert; there is no mechanism to keep them alive. CC's `TeammateIdle` event fires
   just before a teammate goes idle. Exit code 2 sends the stderr text back as agent feedback
   and forces it to continue working. This is the liveness guarantee for `/emerge:monitor`.

2. **PermissionDenied hook** — when CC's auto-mode classifier denies an `icc_*` tool call,
   the call is silently dropped and the model never sees an error. Returning `{"retry": true}`
   from a `PermissionDenied` hook tells CC to let the model retry with explicit permission.
   This prevents silent flywheel failures in auto mode.

3. **hooks.json dead-field cleanup** — the `PostToolUse` `tool_audit` entry has an
   `"if": "!mcp__plugin_emerge_emerge__icc_*"` field that CC does not parse (it is not a
   valid hook JSON field). Filtering is already done correctly by the `matcher` regex.
   Remove the dead field to prevent maintainer confusion.

## Out of Scope

Cockpit-based Elicitation routing (G4) requires `/api/elicitation` endpoint, SSE handshake,
and cockpit modal. It warrants its own phase and spec.

## Design

### TeammateIdle hook (`hooks/teammate_idle.py`)

Input payload from CC (top-level keys):
```json
{
  "hook_event_name": "TeammateIdle",
  "teammate_name": "<agent-name>",
  "team_name": "<team-name>",
  "session_id": "...",
  "transcript_path": "...",
  "cwd": "..."
}
```

Logic:
- If `team_name == "emerge-monitors"` AND `teammate_name` ends with `-watcher`:
  write feedback to stderr, **exit 2** → CC feeds stderr back to the agent as feedback
  so it resumes its monitoring loop.
- All other cases: print `{}`, exit 0 → agent goes idle normally.

Output contract: TeammateIdle is NOT in the CC `hookSpecificOutput`-allowed list
(same as TaskCompleted). Use raw stderr text + exit code 2. Never print JSON with
`hookSpecificOutput` for this event.

### PermissionDenied hook (`hooks/permission_denied.py`)

Input payload from CC:
```json
{
  "hook_event_name": "PermissionDenied",
  "tool_name": "mcp__plugin_emerge_emerge__icc_exec",
  "tool_input": { ... },
  "session_id": "...",
  ...
}
```

Logic:
- If `tool_name` matches `mcp__plugin_.*emerge.*__icc_.*` regex: return `{"retry": true}`.
- All other tools: return `{}` (no opinion).

Output contract: top-level `{"retry": true}`. Not `hookSpecificOutput`.

### hooks.json changes

Add two new top-level event entries:
```json
"TeammateIdle": [{"matcher": ".*", "hooks": [{"type": "command", "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/teammate_idle.py", "timeout": 10, "statusMessage": "Flywheel keeping watcher alive..."}]}],
"PermissionDenied": [{"matcher": "mcp__plugin_.*emerge.*__icc_.*", "hooks": [{"type": "command", "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/permission_denied.py", "timeout": 10, "statusMessage": "Flywheel requesting retry..."}]}]
```

Remove `"if": "!mcp__plugin_emerge_emerge__icc_*"` from the `tool_audit` PostToolUse entry.

## Testing

New test file: `tests/test_hooks_teammate_idle_permission_denied.py`

Pattern: subprocess.run the hook script with JSON on stdin and `EMERGE_DATA_ROOT` env var,
check returncode + stdout/stderr. Follow `tests/test_hooks_stop.py` exactly.

## CLAUDE.md Updates

- `hooks.json hook matchers` invariant line: add `TeammateIdle` (matcher `.*`) and
  `PermissionDenied` (matcher `mcp__plugin_.*emerge.*__icc_.*`).
- New invariant bullets for each hook.
