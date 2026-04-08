---
description: Memory Hub — configure sync, show status, and guide conflict resolution
---

Manage the Memory Hub: first-time setup via natural language, or show sync status and resolve conflicts.

## First-time setup (when not yet configured)

If `icc_hub(action="list")` shows `configured: false`, run the setup flow:

1. **Collect information from the user** (ask one question at a time):
   - Remote URL — the git remote on their self-hosted server, e.g. `user@quasar:repos/emerge-hub.git`
   - Branch name — default `emerge-hub` (ask only if they want a custom name)
   - Author identity — `Name <email>`, used for git commits
   - Which connectors to sync — call `icc_hub(action="list")` to show what's available locally; ask the user to pick

2. **Initialize the hub**:
   ```
   icc_hub(action="configure",
     remote="<url>",
     author="<name> <<email>>",
     selected_verticals=["<connector1>", ...],
     branch="emerge-hub")           # omit if using default
   ```
   - On success, the daemon saves config and initialises the git worktree.
   - Report the result (`action`: "created" / "cloned" / "already_exists").

3. **Start the sync agent** — tell the user to run this once in a terminal (and keep it running):
   ```bash
   python scripts/emerge_sync.py run
   ```
   Tip: they can background it with `nohup python scripts/emerge_sync.py run > ~/.emerge/sync.log 2>&1 &`

4. **Verify** — call `icc_hub(action="status")` and confirm `configured: true`, no pending conflicts.

---

## Ongoing status and conflict resolution

1. **Check hub status** — call `icc_hub(action="status")`.
   - Report: remote, selected connectors, queue depth, `pending_conflicts`, `awaiting_application`.

2. **Pending conflicts** — if `pending_conflicts > 0`:
   For each conflict in `conflicts`:
   - Show: `connector`, `file`, `ours_ts_ms` vs `theirs_ts_ms`, `ours_success_rate` if present.
   - Ask the operator which version to keep:
     - **ours** — keep local (no file change)
     - **theirs** — overwrite with remote version (applied on next sync cycle)
     - **skip** — leave file as-is, mark resolved
   - Call `icc_hub(action="resolve", conflict_id="<id>", resolution="<choice>")` for each conflict.
   - After all resolved: report `awaiting_application` count; sync agent applies on its next cycle,
     or the user can flush immediately: `python scripts/emerge_sync.py sync`.

3. **Awaiting application** — if `awaiting_application > 0` and no pending conflicts:
   - Inform the user resolutions are queued; suggest `python scripts/emerge_sync.py sync` to apply now.

4. **Sync queue depth** — if `queue_depth > 0`:
   - Pending push/pull events; sync agent processes them every 10 s.
   - To flush immediately: `python scripts/emerge_sync.py sync`.

5. **All clear** — if everything is 0: confirm hub is fully in sync and list selected connectors.
