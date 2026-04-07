---
description: Emerge flywheel cockpit — interactive browser dashboard
---

Open the Emerge cockpit dashboard for the active session.

Always invoke the admin CLI via the **Emerge plugin root** (not the user's open project). Claude Code expands `${CLAUDE_PLUGIN_ROOT}` to that path when this command runs.

Steps:

1. **Start the server** (run foreground — it self-daemonizes and returns quickly):
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" serve --open --port 0`
   - Idempotent: if an instance is already running, it returns the existing URL.
   - Wait ~1 second, then read output to extract the URL (`Cockpit running at http://localhost:PORT`).

2. **Print status summary**:
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" policy-status --pretty`
   Report to the user: URL, total pipeline count (explore/canary/stable), any pipelines with consecutive_failures.

3. **Sense vertical assets and inject controls** (CC-driven, framework-agnostic) — **do this before step 4**:
   - `policy-status --pretty` output is already in hand from step 2; also read `~/.emerge/connectors/<connector>/NOTES.md` and any `scenarios/*.yaml` or `cockpit/*.html` files for each connector.
   - **Always inject a panel for every connector that has pipelines** — do not skip even if no explicit `cockpit/*.html` exists.
   - For each connector, POST to `http://localhost:<PORT>/api/inject-component`:
     ```json
     {"connector": "<name>", "id": "<name>-main", "replace": true, "html": "<full HTML doc>"}
     ```
     (`replace:true` + named `id` = clear stale injections from prior session; re-injecting with same id updates in-place)

   **Every pipeline button MUST have a working `onclick`.** The injected iframe is same-origin; use `window.parent.cockpit` API:
   - **queueAction** (adds to pending queue, user clicks Submit): `window.parent.cockpit.queueAction({...})`
   - **submitNow** (fires immediately, no confirm): `window.parent.cockpit.submitNow([{...}])`

   **Action payload reference** — use these exact shapes:

   | Button purpose | onclick payload |
   |---|---|
   | Promote pipeline to canary | `window.parent.cockpit.queueAction({type:'pipeline-set',key:'<conn>.<mode>.<name>',set:{status:'canary'}})` |
   | Promote to stable | `window.parent.cockpit.queueAction({type:'pipeline-set',key:'<conn>.<mode>.<name>',set:{status:'stable'}})` |
   | Rollback to explore | `window.parent.cockpit.queueAction({type:'pipeline-set',key:'<conn>.<mode>.<name>',set:{status:'explore'}})` |
   | Run pipeline (icc_exec) | `window.parent.cockpit.submitNow([{type:'tool-call',call:{tool:'mcp__plugin_emerge_emerge__icc_exec',arguments:{intent_signature:'<sig>',script:''}}}])` |
   | Delete pipeline | `window.parent.cockpit.queueAction({type:'pipeline-delete',key:'<conn>.<mode>.<name>'})` |

   **Minimum panel structure:**
   - One button per pipeline, styled by current status (`explore`=gray, `canary`=yellow, `stable`=green). Each button must have onclick.
   - Clicking a pipeline button should `queueAction` a promotion to the next tier (explore→canary, canary→stable), or show a small inline dropdown with options.
   - Notes snippet (first 10 lines of NOTES.md) if present.
   - Scenario cards (from `scenarios/*.yaml`) if present.

   Only skip injection if a connector has zero pipelines AND no NOTES.md.

4. **Enter the dispatch loop** (core, background-driven):

   a. Launch `wait-for-submit` using the **Bash tool with `run_in_background: true` parameter** (timeout 600000ms):
      `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" wait-for-submit`
      - **CRITICAL**: Set the Bash tool's `run_in_background` parameter to `true` — do NOT use shell `&`. Only the tool parameter triggers the automatic completion notification.
      - Tell the user the cockpit is ready and you'll handle submissions automatically; they are free to ask other questions in the meantime.
      - You will be **notified automatically** when the command completes — do NOT poll or sleep.

   b. When notified of completion, **always read the output file** (`cat <output-file-path>`) before doing anything else — the task-notification only contains a summary, not the actions. Parse the JSON:
      - `{"ok": false, "timeout": true}` → re-launch wait-for-submit in background (back to step a); no user message needed
      - If the user said "close cockpit" before the notification arrived, skip processing and go to step 5 instead.
      - `{"ok": true, "actions": [...]}` → go to step c

   c. **Re-arm FIRST, then process** — this order is critical:
      1. **Immediately re-launch wait-for-submit in background** (back to step a pattern) so the frontend can accept the next submission while you process the current one.
      2. Then process received actions sequentially:
         - `pipeline-set` → `repl_admin.py pipeline-set --pipeline-key <key> --set <field>=<value>` (one --set per field)
         - `pipeline-delete` → `repl_admin.py pipeline-delete --pipeline-key <key>`
         - `notes-comment` → append `\n\n<!-- <ISO timestamp> -->\n<comment>` to `~/.emerge/connectors/<connector>/NOTES.md`
         - `notes-edit` → overwrite `~/.emerge/connectors/<connector>/NOTES.md` entirely
         - `tool-call` → **deterministic call only**: execute exactly `call.tool` + `call.arguments` (`icc_read`/`icc_write`); do not rewrite as free-form reasoning
           - If `auto.mode=auto` and `flywheel.synthesis_ready=true`, append a crystallization suggestion after execution (do not block the result)
         - `crystallize-component` → write to `~/.emerge/connectors/<connector>/cockpit/<filename>.html` and `<filename>.context.md`
      3. Briefly report results. The next wait-for-submit is already running — no need to re-arm again.

5. **Close the cockpit**: when the user says close/exit:
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" serve-stop`
