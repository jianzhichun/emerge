---
description: Batch-check and deploy Emerge runner scripts while preserving per-profile failure isolation
---

# Admin Batch Update Runners

Use `skills/admin-runner-operations` before running this command flow.

1. Inspect target health:
   ```bash
   python3 scripts/repl_admin.py runner-status --pretty
   ```
2. Select profiles that are online or explicitly requested.
3. Deploy each target independently:
   ```bash
   python3 scripts/repl_admin.py runner-deploy --target-profile <profile>
   ```
4. Re-run status and report changed profiles, skipped profiles, and any retry command.

Do not hide partial failures behind a single batch success message.
