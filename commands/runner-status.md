---
description: Show remote runner health status
---

Display the configured remote runner connectivity and health.

Steps:
1. Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" runner-status --pretty`.
2. If that fails, run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" runner-status` and show JSON.
3. Summarize:
   - whether `EMERGE_RUNNER_URL` is configured
   - whether runner is reachable
   - health fields returned by `/health`
