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
4. If required context is missing, ask only minimal clarifying questions.
5. Report:
  - status: `init_ok` / `degraded` / `blocked`
  - created/updated assets
  - verification evidence
  - next recommended action