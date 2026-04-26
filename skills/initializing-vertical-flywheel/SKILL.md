---
name: initializing-vertical-flywheel
description: Use when a user asks to initialize a domain flywheel from natural language context, especially when environment details are incomplete or mixed with execution assumptions.
---

# Initializing Vertical Flywheel

## Overview

Use this skill to convert one natural-language bootstrap request into concrete vertical flywheel assets under `~/.emerge/connectors/<vertical>/`, plus verified runtime readiness. Never place vertical connectors inside the plugin project directory.

Core principle: do not claim initialization complete until new read/write pipelines execute and policy state is observable.

## When to Use

- User asks for one-sentence bootstrap, such as "initialize desktop_drafting_app vertical flywheel".
- User context is natural language, not strict parameters.
- Environment assumptions are uncertain (host/tooling/executor not guaranteed).

Do not use when:

- User only asks for explanation, not initialization.
- User asks only for policy status or read-only review.

## Mandatory TDD Flow

1. **RED**: add/init tests for the requested vertical and watch them fail.
2. **GREEN**: add minimum assets and code to make tests pass.
3. **REFACTOR**: harden naming, verification, and output shape without changing behavior.

No bootstrap completion claim without RED and GREEN evidence.

## Core Initialization Contract

- Input is user natural language; do not force CLI-style parameter declarations.
- Extract only what is explicit in user text.
- Ask only minimal clarifying questions when execution cannot proceed.
- Produce:
  - bootstrap status (`init_ok`, `degraded`, or `blocked`)
  - created/updated assets
  - next verification actions

## Remote runner setup (when needed)
If the user context indicates remote execution, initialize runner connectivity first.

Minimum steps:
1. Generate install URLs from local plugin root (or Cockpit Monitors → Add Runner):
   - `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" runner-install-url --target-profile "<target_profile>" --pretty`
2. Send the printed command to the operator; they run it on the target machine (self-install).
3. Verify with one `icc_exec` smoke call before creating vertical assets.
4. Verify admin health signal:
   - `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" runner-status --pretty`
   - proceed only when `Runner reachable: True`.

**Runner HTTP protocol (summary):**

| Endpoint | Purpose |
|----------|---------|
| `POST /run` | Execute one `icc_exec` call |
| `GET /health` | Liveness probe |
| `GET /status` | Process info (pid, uptime, root) |
| `GET /logs?n=N` | Last N log lines |

Runner accepts **only `icc_exec`**. Pipeline bridge execution is handled by the daemon (loads files locally, sends as inline `icc_exec`). Request shape: `{"tool_name": "icc_exec", "arguments": {"code": "...", "target_profile": "...", "no_replay": false}}`.

## Connector Location Rule

This plugin is a **generic RWB flywheel engine**. Vertical-specific connectors must NOT be placed inside the plugin project directory.

- Plugin project (`connectors/mock/`) — testing only, committed to git
- User verticals → `~/.emerge/connectors/<vertical>/` (user-space, not committed)
- Override via env: `EMERGE_CONNECTOR_ROOT=<path>` (e.g. for remote runner deployments)

`PipelineEngine` searches `~/.emerge/connectors/` before the plugin root, so user verticals take precedence automatically.

## Assets To Create (Minimum)

For vertical `<vertical>` (for example `desktop_drafting_app`), create in **user-space**:

- `~/.emerge/connectors/<vertical>/pipelines/read/state.yaml`
- `~/.emerge/connectors/<vertical>/pipelines/read/state.py`
- `~/.emerge/connectors/<vertical>/pipelines/write/apply-change.yaml`
- `~/.emerge/connectors/<vertical>/pipelines/write/apply-change.py`
- `~/.emerge/connectors/<vertical>/synthesis_hints.yaml` when reverse-flywheel synthesis should learn from operator events
- tests (in plugin project, prefer existing suites unless there is a strong reason to split files):
  - `tests/test_pipeline_engine.py`
  - `tests/test_mcp_tools_integration.py`

Do not skip write verification hooks:

- `run_write(...)`
- `verify_write(...)`
- `rollback_write(...)` when policy is `rollback`

## Pipeline Metadata Rules

Each yaml/json metadata file must include:

- `intent_signature`
- `*_steps` (`read_steps` or `write_steps`)
- `verify_steps`
- `rollback_or_stop_policy` (`stop` or `rollback`)

## Implementation Pattern

1. Start with mock-safe behavior in `*.py` that returns deterministic objects.
2. Ensure read returns structured rows and verify payload.
3. Ensure write returns `verification_state` plus policy enforcement fields:
  - `policy_enforced`
  - `stop_triggered`
  - `rollback_executed`
  - `rollback_result`
4. Keep output keys stable; do not introduce ad-hoc text-only outputs.

## Verification Checklist

Run, in order:

1. Targeted tests for new vertical files.
2. `pytest -q` full suite.
3. Confirm policy observability:
  - `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" policy-status --pretty`
  - verify intent entry appears and counters move after calls.

Initialization is complete only when:

- read and write `icc_exec` calls succeed (using the connector's `intent_signature`)
- policy status includes the new intent key (shape: `<connector>.<mode>.<pipeline>`)
- when remote mode is used, at least one call is confirmed through runner dispatch
- tests and lint pass.

If runner is required and unreachable, return `blocked` (not `degraded`).

## TDD Test Surface (Required)

Do not treat `PipelineEngine` unit tests alone as sufficient proof.

For RED and GREEN phases, tests must exercise MCP-facing tool paths:

- `icc_exec` (primary exploration tool — pipeline bridge execution is via `icc_span_open` when stable)

Minimum expectation:

1. At least one failing-then-passing test through `EmergeDaemon.call_tool(...)` or JSON-RPC `tools/call` for each intent used by the init flow.
2. At least one integration assertion that policy registry changes after `icc_exec` calls.
3. If flywheel bridge is used, include a failing-then-passing test for bridge key updates (`flywheel::...`).

## Quick Reference

- **Read pipeline id:** `<vertical>.read.<pipeline>`
- **Write pipeline id:** `<vertical>.write.<pipeline>`
- **Flywheel bridge key shape:** `flywheel::<pipeline_id>::<intent_signature>::<script_ref>`
- **Policy states:** `explore -> canary -> stable`

## Rationalization Table


| Excuse                              | Reality                                                              |
| ----------------------------------- | -------------------------------------------------------------------- |
| "We can assume remote-vm exists"    | Executor is optional context, not a guaranteed dependency.           |
| "Mock connector means init is done" | Init requires runnable assets, passing tests, and policy visibility. |
| "No need for TDD on docs/skills"    | Skills are process code; TDD still applies.                          |
| "I can ship only yaml metadata"     | Flywheel requires executable `*.py` and verification behavior.       |


## Red Flags

- "I can skip baseline and start implementation."
- "I will hardcode environment assumptions from my local setup."
- "I can declare success without verification output."
- "I added files but did not run `icc_exec` integration tests."

Any red flag means stop and return to RED.

## Reverse Flywheel Integration

When reverse-flywheel synthesis is in scope, write `synthesis_hints.yaml`
beside `NOTES.md`. Keep it operational and compact:

```yaml
api_imports:
  - "from vendor_api import app"
event_examples:
  entity_added:
    python: "__action = {'ok': True}"
verify_guidance:
  read: "Return non-empty rows with stable keys."
  write: "Set __action['ok'] only after the application confirms the mutation."
default_exec_timeout: 600
```

When any `intent_signature` for this vertical reaches `stable` status in
`policy://current`, prompt the operator:

> "`<vertical>.*` pipeline flywheel is stable. Consider establishing a reverse
> flywheel to observe operator behavior and proactively identify repetitive
> actions the AI can take over. If needed, invoke the
> `operator-monitor-debug` skill."

This connects the forward flywheel (AI learns to DO tasks) to the reverse
flywheel (AI learns to RECOGNIZE when humans are doing those tasks repeatedly).
The vertical adapter shares the same `intent_signature` namespace and feeds the
same policy registry — a repeated operator pattern becomes
`pattern_pending_synthesis`, Claude Code skills inspect the facts, and verified
code enters the flywheel via `icc_exec` WAL at explore stage.