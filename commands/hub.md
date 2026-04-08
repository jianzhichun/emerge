---
description: Memory Hub status — show pending conflicts, sync queue depth, and guide conflict resolution
---

Show the current Memory Hub state and guide the operator through any pending merge conflicts.

Steps:

1. **Check hub status** — call `icc_hub(action="status")`.
   - If `configured` is false: tell the user to run `python3 scripts/emerge_sync.py setup` in a terminal and stop.
   - Report: remote URL, selected connectors, queue depth, `pending_conflicts`, `awaiting_application`.

2. **Pending conflicts** — if `pending_conflicts > 0`:
   For each conflict in `conflicts`:
   - Show: `connector`, `file`, `ours_ts_ms` vs `theirs_ts_ms`, `ours_success_rate` if present.
   - Ask the operator which version to keep:
     - **ours** — keep the local version (no file change, hub retains current HEAD)
     - **theirs** — overwrite with the remote version (fetched from `origin/<branch>` on next sync cycle)
     - **skip** — leave the file as-is, mark resolved without applying either side
   - Call `icc_hub(action="resolve", conflict_id="<id>", resolution="<choice>")` for each resolved conflict.
   - After all resolved: report `awaiting_application` count and tell the user the sync agent will apply resolutions on its next cycle (or manually: `python3 scripts/emerge_sync.py sync`).

3. **Awaiting application** — if `awaiting_application > 0` and `pending_conflicts == 0`:
   - Inform the user that `awaiting_application` conflicts have been resolved and are queued for the next sync cycle.
   - Suggest running `python3 scripts/emerge_sync.py sync` to apply immediately.

4. **Sync queue depth** — if `queue_depth > 0`:
   - Mention the number of pending push/pull events; the sync agent will process them on its next wake (every 10 s).
   - If the user wants to flush immediately: `python3 scripts/emerge_sync.py sync`.

5. **All clear** — if `pending_conflicts == 0` and `awaiting_application == 0` and `queue_depth == 0`:
   - Report the hub is fully in sync. List selected connectors.
