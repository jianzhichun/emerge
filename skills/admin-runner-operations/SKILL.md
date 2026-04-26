---
name: admin-runner-operations
description: Use when installing, deploying, checking, or batch-updating Emerge runners from admin commands or cockpit operator workflows.
---

# Admin Runner Operations

## Purpose

Keep Python runner admin code as transport primitives. Use this skill for the workflow: choosing targets, interpreting health, sequencing deploys, and reporting recovery steps.

## Workflow

1. Check runner status before changing anything with `python3 scripts/repl_admin.py runner-status --pretty` or the matching `icc_*` admin tool.
2. For new machines, generate a bootstrap URL with `python3 scripts/repl_admin.py runner-install-url --target-profile <profile>`.
3. Deploy scripts one profile at a time unless the operator explicitly asks for batch rollout.
4. After deploy, re-check health and summarize only profiles whose state changed or still need action.

## Failure Handling

- Offline runner: report last seen time and avoid repeated deploy attempts.
- Auth/config error: regenerate install URL rather than editing runner state by hand.
- HTTP timeout: retry once, then leave a concrete command for manual follow-up.
- Mixed batch result: treat successful profiles as complete; do not roll back all runners.

## Output Style

Use compact bullets grouped by profile. Include exact commands only for the next actionable step.
