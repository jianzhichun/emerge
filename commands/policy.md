---
description: Show Emerge flywheel policy status dashboard
---

Display the current Emerge policy dashboard for the active session.

Always invoke the admin CLI via the **Emerge plugin root** (not the user's open project). Claude Code expands `${CLAUDE_PLUGIN_ROOT}` to that path when this command runs.

Steps:
1. Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" policy-status --pretty`.
2. If it fails, run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" policy-status` and show the JSON result.
3. Summarize:
   - total pipelines
   - how many are `explore`, `canary`, `stable`
   - any pipelines with `consecutive_failures >= 1`
   - current threshold values
