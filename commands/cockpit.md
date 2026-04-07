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

3. **Sense vertical assets and inject controls** (CC-driven, framework-agnostic) — **do this before step 4**:
   - Run `policy-status --pretty` output is already in hand from step 2; also read `~/.emerge/connectors/<connector>/NOTES.md` and any `scenarios/*.yaml` or `cockpit/*.html` files for each connector.
   - **Always inject a panel for every connector that has pipelines** — do not skip even if no explicit `cockpit/*.html` exists. Generate a compact HTML control panel that includes:
     - A quick-actions section: one button per crystallized pipeline (read/write/debug), styled by status (explore=gray, canary=yellow, stable=green)
     - A notes snippet: first 10 lines of NOTES.md if present
     - Any scenario cards from `scenarios/*.yaml` if present
   - For each connector panel, call `POST http://localhost:<PORT>/api/inject-component` with JSON body `{"connector": "<name>", "html": "<full HTML document>", "replace": true}`. The `replace: true` clears any stale injection from a previous session before adding the new one.
   - Injected fragments appear under the connector **Controls** tab as `injected-runtime-0.html`, … (session-only; use `crystallize-component` to persist under `cockpit/*.html`). The UI refreshes assets on the same interval as policy (~5s), or the user can reload the page.
   - Only skip if a connector has zero pipelines and no NOTES.md.

4. **Enter the dispatch loop** (core, background-driven):

   a. Launch `wait-for-submit` in the **background** (`run_in_background: true`, timeout 600000ms):
      `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" wait-for-submit`
      - Tell the user the cockpit is ready and you'll handle submissions automatically; they are free to ask other questions in the meantime.
      - You will be **notified automatically** when the command completes — do NOT poll or sleep.

   b. When notified of completion, **always read the output file** (`cat <output-file-path>`) before doing anything else — the task-notification only contains a summary, not the actions. Parse the JSON:
      - `{"ok": true, "actions": [...]}` → process actions (step c)
      - `{"ok": false, "timeout": true}` → re-launch wait-for-submit in background (back to step a); no user message needed
      - If the user said "close cockpit" before the notification arrived, skip processing and go to step 5 instead.

   c. Process received actions sequentially:
      - `pipeline-set` → `repl_admin.py pipeline-set --pipeline-key <key> --set <field>=<value>` (one --set per field)
      - `pipeline-delete` → `repl_admin.py pipeline-delete --pipeline-key <key>`
      - `notes-comment` → append `\n\n<!-- <ISO timestamp> -->\n<comment>` to `~/.emerge/connectors/<connector>/NOTES.md`
      - `notes-edit` → overwrite `~/.emerge/connectors/<connector>/NOTES.md` entirely
      - `tool-call` → **deterministic call only**: execute exactly `call.tool` + `call.arguments` (`icc_read`/`icc_write`); do not rewrite as free-form reasoning
        - If `auto.mode=auto` and `flywheel.synthesis_ready=true`, append a crystallization suggestion after execution (do not block the result)
      - `crystallize-component` → write to `~/.emerge/connectors/<connector>/cockpit/<filename>.html` and `<filename>.context.md`

   d. After processing, briefly report results, then immediately re-launch wait-for-submit in background (back to step a) — unless the user said "close cockpit".

5. **Close the cockpit**: when the user says close/exit:
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" serve-stop`
