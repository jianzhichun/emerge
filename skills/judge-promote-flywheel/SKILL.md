---
name: judge-promote-flywheel
description: Use when Emerge emits evidence_applied facts and Claude must review intent evidence before asking mechanism tools for an explicit stage transition.
---

# Judge Promote Flywheel

Use this when `evidence_applied` is visible. The registry stores facts; Claude evaluates whether they are enough for a requested transition.

## Workflow

1. Read the intent's current registry entry and recent transition history.
2. Inspect success rate, verify rate, human fixes, bridge failures, and recent outcomes.
3. Compare the evidence with connector-local risk notes.
4. If promotion is justified, call the explicit stage mechanism tool provided by the runtime.
5. If not justified, leave state unchanged and explain the missing evidence.

## Rules

- Never edit `intents.json` directly.
- Treat silent-empty bridge regressions as safety failures.
- Reads can tolerate smaller blast radius than irreversible writes, but evidence still must be real.
- This skill is guidance only until the runtime exposes an approved explicit stage transition tool.

## Output

Return the recommended target stage or `no_change`, evidence reviewed, and the safety rationale.
