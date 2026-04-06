---
description: Emerge flywheel cockpit — interactive browser dashboard
---

Open the Emerge cockpit dashboard for the active session.

Always invoke the admin CLI via the **Emerge plugin root** (not the user's open project). Claude Code expands `${CLAUDE_PLUGIN_ROOT}` to that path when this command runs.

Steps:
1. Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" serve --open --port 0`.
   - This starts the HTTP server on a free port and opens the browser automatically.
   - Print the dashboard URL when it is displayed.
2. While the server starts, also print the policy text summary for quick reference:
   Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" policy-status --pretty`.
3. Tell the user:
   - The dashboard URL (printed by the serve command)
   - Brief text summary (from policy-status --pretty):
     - total pipelines, explore/canary/stable counts
     - any pipelines with `consecutive_failures >= 1`
4. After `/cockpit` starts, CC reads current emerge assets:
   - Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" policy-status` (JSON)
   - For each connector in `~/.emerge/connectors/`, read `NOTES.md` and list `scenarios/*.yaml` files
   - For each connector, check `cockpit/` for crystallized components
5. Generate dynamic HTML panels for connectors that have rich assets (scenarios, missing notes coverage, etc.) and inject them via `POST http://localhost:<PORT>/api/inject-component`.
6. When the dashboard receives a submission (pending-actions.json appears), CC will be notified via MCP channel notification and should execute the actions using subagents:
   - `pipeline-set` / `pipeline-delete` → run `repl_admin.py pipeline-set`/`pipeline-delete`
   - `notes-comment` → append the comment with timestamp to the connector's `NOTES.md`
   - `notes-edit` → replace the connector's `NOTES.md` full content
   - `scenario-run` → call `icc_exec` with `intent_signature=write.<connector>.apply-test` and `scenario=<name>`
   - `crystallize-component` → write HTML to `~/.emerge/connectors/<connector>/cockpit/<filename>.html` and context to `<filename>.context.md`
