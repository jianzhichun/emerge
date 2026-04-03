---
description: Show Emerge flywheel policy status dashboard
---

Display the current Emerge policy dashboard for the active session.

Steps:
1. Run `python3 scripts/repl_admin.py policy-status --pretty`.
2. If it fails, run `python3 scripts/repl_admin.py policy-status` and show the JSON result.
3. Summarize:
   - total pipelines
   - how many are `explore`, `canary`, `stable`
   - any pipelines with `consecutive_failures >= 1`
   - current threshold values
