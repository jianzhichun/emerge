---
description: Emerge flywheel cockpit — interactive browser dashboard
---

Open the Emerge cockpit dashboard for the active session.

Always invoke the admin CLI via the **Emerge plugin root** (not the user's open project). Claude Code expands `${CLAUDE_PLUGIN_ROOT}` to that path when this command runs.

Steps:

1. **Start the server** (background, `run_in_background: true`):
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" serve --open --port 0`
   - Idempotent: if an instance is already running, it returns the existing URL.
   - Wait ~1 second, then read output to extract the URL (`Cockpit running at http://localhost:PORT`).

2. **Print status summary**:
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" policy-status --pretty`
   Report to the user: URL, total pipeline count (explore/canary/stable), any pipelines with consecutive_failures.

3. **Sense vertical assets and inject controls** (CC-driven, framework-agnostic):
   - Read each connector's assets under `~/.emerge/connectors/` (`NOTES.md`, `scenarios/*.yaml`, `pipelines/`, `cockpit/*.html`, etc.)
   - Based on your understanding of the vertical context, decide which assets are worth surfacing as Cockpit controls (e.g. scenario cards, quick actions)
   - Inject generated HTML components via `POST http://localhost:<PORT>/api/inject-component`
   - If no valuable vertical assets are found, do nothing — the framework does not scan or render scenarios on its own

4. **Enter the dispatch loop** (core):

   Repeat the following steps until the user explicitly says "close cockpit":

   a. Call (blocking, up to 10 min timeout):
      `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" wait-for-submit`
      - Blocks until the user clicks Submit in the browser; returns `{"ok": true, "actions": [...], "action_count": N}`
      - If it returns `{"ok": false, "timeout": true}`: no submission within 10 min — call again (cockpit is still open)

   b. Process received actions sequentially:
      - `pipeline-set` → `repl_admin.py pipeline-set --pipeline-key <key> --set <field>=<value>` (one --set per field)
      - `pipeline-delete` → `repl_admin.py pipeline-delete --pipeline-key <key>`
      - `notes-comment` → append `\n\n<!-- <ISO timestamp> -->\n<comment>` to `~/.emerge/connectors/<connector>/NOTES.md`
      - `notes-edit` → overwrite `~/.emerge/connectors/<connector>/NOTES.md` entirely
      - `tool-call` → **deterministic call only**: execute exactly `call.tool` + `call.arguments` (`icc_read`/`icc_write`); do not rewrite as free-form reasoning
        - If `auto.mode=auto` and `flywheel.synthesis_ready=true`, append a crystallization suggestion after execution (do not block the result)
      - `crystallize-component` → write to `~/.emerge/connectors/<connector>/cockpit/<filename>.html` and `<filename>.context.md`

   c. After execution, briefly report results in the terminal, then return to step a to await the next submission.

5. **Close the cockpit**: when the user says close/exit:
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" serve-stop`
