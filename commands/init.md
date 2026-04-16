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
4. If remote execution is required, set up the runner first (operator self-install — no SSH from CC):
  - generate install commands from local plugin root:
    - `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" runner-install-url --target-profile "<target_profile>" --pretty`
  - send the printed `curl ... | bash` or `irm ... | iex` to the operator; they run it on the target machine (installs deps, writes `~/.emerge/runner-config.json`, starts runner)
  - optionally use Cockpit → Monitors tab → Add Runner to copy the same URLs
  - run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" runner-status --pretty`
  - if unreachable, do not continue pipeline initialization blindly
5. If required context is missing, ask only minimal clarifying questions.
6. Report:
  - status: `init_ok` / `degraded` / `blocked`
  - created/updated assets
  - verification evidence
  - next recommended action
7. Connector notes handling (must match current architecture):
  - keep `~/.emerge/connectors/<connector>/NOTES.md` concise and operational (no long dumps)
  - SessionStart only exposes a compact connector index
  - full NOTES are injected on-demand by `PreToolUse` on the first approved `icc_exec` / `icc_span_open` / `icc_crystallize` call per connector

Status rules:
- `init_ok`: runner reachable (if needed), assets created, `icc_exec` smoke checks pass.
- `degraded`: partial success (assets created but verification failed or policy degraded).
- `blocked`: missing prerequisites (runner unreachable, missing host context, or required toolchain absent).