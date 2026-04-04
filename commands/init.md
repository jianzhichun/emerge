---
description: Initialize a vertical flywheel from user natural language
---

Interpret the user's `/emerge:init ...` message as natural-language bootstrap intent.

Execution rules:

1. Use the `initializing-vertical-flywheel` skill as the primary workflow.
2. Treat user text as the source of context; do not require parameter-style declarations.
3. Follow TDD evidence order:
  - RED baseline (what is missing now)
  - GREEN minimum bootstrap
  - REFACTOR guardrails
4. If remote execution is required, bootstrap remote runner first:
  - run bootstrap command from local plugin root:
    - `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" runner-bootstrap --ssh-target "<user@host>" --target-profile "<target_profile>" --runner-url "http://<target>:8787"`
  - this command performs: remote deploy (default), python check, runner start, health probe, local runner-map persist
  - behavior is idempotent: if runner is already healthy and version matches, bootstrap reuses existing runner
  - run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" runner-status --pretty`
  - if unreachable, do not continue pipeline initialization blindly
5. If required context is missing, ask only minimal clarifying questions.
6. Report:
  - status: `init_ok` / `degraded` / `blocked`
  - created/updated assets
  - verification evidence
  - next recommended action

Status rules:
- `init_ok`: runner reachable (if needed), assets created, `icc_read`/`icc_write` smoke checks pass.
- `degraded`: partial success (assets created but verification failed or policy degraded).
- `blocked`: missing prerequisites (runner unreachable, missing host context, or required toolchain absent).