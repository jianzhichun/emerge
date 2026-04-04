---

## name: initializing-vertical-flywheel
description: Use when a user asks to initialize a domain flywheel from natural language context, especially when environment details are incomplete or mixed with execution assumptions.

# Initializing Vertical Flywheel

## Overview

Use this skill to convert one natural-language bootstrap request into concrete vertical flywheel assets under `connectors/`, plus verified runtime readiness.

Core principle: do not claim initialization complete until new read/write pipelines execute and policy state is observable.

## When to Use

- User asks for one-sentence bootstrap, such as "initialize zwcad vertical flywheel".
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

## Assets To Create (Minimum)

For vertical `<vertical>` (for example `zwcad`), create:

- `connectors/<vertical>/pipelines/read/state.yaml`
- `connectors/<vertical>/pipelines/read/state.py`
- `connectors/<vertical>/pipelines/write/apply-change.yaml`
- `connectors/<vertical>/pipelines/write/apply-change.py`
- tests (prefer existing suites unless there is a strong reason to split files):
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
  - verify pipeline entry appears and counters move after calls.

Initialization is complete only when:

- read and write calls succeed through `icc_read/icc_write`
- policy status includes the new pipeline key (shape: `pipeline::<connector>.<mode>.<pipeline>`)
- tests and lint pass.

## TDD Test Surface (Required)

Do not treat `PipelineEngine` unit tests alone as sufficient proof.

For RED and GREEN phases, tests must exercise MCP-facing tool paths:

- `icc_exec`
- `icc_read`
- `icc_write`

Minimum expectation:

1. At least one failing-then-passing test through `ReplDaemon.call_tool(...)` or JSON-RPC `tools/call` for each path used by the init flow.
2. At least one integration assertion that policy registry changes after `icc_read/icc_write` calls.
3. If L1.5 composition is used, include a failing-then-passing test for composed key updates (`l15::...`).

## Quick Reference

- **Read pipeline id:** `<vertical>.read.<pipeline>`
- **Write pipeline id:** `<vertical>.write.<pipeline>`
- **Composed L1.5 key shape:** `l15::<pipeline_id>::<exec_signature-or-intent_signature>::<script_ref>`
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
- "I added files but did not run `icc_read/icc_write` integration tests."

Any red flag means stop and return to RED.