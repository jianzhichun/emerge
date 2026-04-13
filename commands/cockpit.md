---
description: Emerge flywheel cockpit — interactive browser dashboard
---

Open the Emerge cockpit dashboard for the active session.

Always invoke the admin CLI via the **Emerge plugin root** (not the user's open project). Claude Code expands `${CLAUDE_PLUGIN_ROOT}` to that path when this command runs.

Steps:

1. **Start the server** (long-running command; run in background):
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" serve --open --port 0`
   - Use the Bash tool with `run_in_background: true` (do not use shell `&`).
   - Idempotent: if an instance is already running for the same project, it reuses the existing URL.
   - Read startup output to extract URL (`Cockpit running at http://localhost:PORT`).
   - Keep this process alive until explicit close (`serve-stop`).

2. **Print status summary**:
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" policy-status --pretty`
   Report to the user: URL, total pipeline count (explore/canary/stable), any pipelines with consecutive_failures.
   Also check reflection cache status for observability:
   `curl -s "http://localhost:<PORT>/api/control-plane/reflection-cache" | jq`
   If cache is missing/stale and operator wants deep reflection, run:
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/build_reflection_cache.py"`

3. **Sense vertical assets and inject controls** (CC-driven, framework-agnostic) — **do this before step 4**:
   - Read `~/.emerge/connectors/<connector>/NOTES.md` and any `scenarios/*.yaml` files for each connector.
   - The **Pipelines tab already shows** pipeline cards with promote/rollback/delete actions — **do NOT duplicate that in Controls**.
   - **Controls tab is for vertical-specific capabilities NOT covered by the Pipelines tab:**
     - Scenario cards from `scenarios/*.yaml` (with a "Run" button per scenario)
     - Diagnostic quick-actions: ping, connection health check, reset COM session, port check, etc.
     - Domain-specific tools: open a specific file, clear mesh, restart a service, etc.
     - Key notes snippet (first 5–10 lines of NOTES.md) as context for the operator
   - **Always inject** for every connector that has pipelines or NOTES.md. A minimal panel with just notes + one diagnostic button is fine — the goal is to surface vertical context, not to replicate what Pipelines tab shows.
   - For each panel worth injecting, POST to `http://localhost:<PORT>/api/inject-component`:
     ```json
     {"connector": "<name>", "id": "<name>-main", "replace": true, "html": "<full HTML doc>"}
     ```

   **Interactive buttons** — the injected iframe is same-origin; use `window.parent.cockpit` API:
   - **queueAction** (adds to queue, user confirms): `window.parent.cockpit.queueAction({type:'tool-call', call:{tool:'mcp__plugin_emerge_emerge__icc_exec', arguments:{intent_signature:'<sig>', script:''}}})`
   - **submitNow** (fires immediately): `window.parent.cockpit.submitNow([{type:'tool-call', call:{...}}])`
   - Pipeline status changes belong in the **Pipelines tab**, not here. Only use `pipeline-set` actions in Controls if the button represents a meaningful high-level workflow (e.g., "Promote all stable-ready pipelines").

   Only skip injection if a connector has zero pipelines AND no NOTES.md.

4. **Event-driven dispatch** — start Monitors for cockpit submissions and operator alerts:

   **Monitor 1 — cockpit actions:**
   ```
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/watch_pending.py"
   ```
   Set `persistent: true` and `description: "cockpit action watcher"`.

   The script watches `pending-actions.json` (written by cockpit `/api/submit`).
   When the operator submits, each action list prints to stdout and streams into
   this conversation as a Monitor notification — no polling, no sleep loops.

   When a `[Cockpit]` notification arrives, execute actions sequentially and deterministically:
     - `pipeline-set` → `repl_admin.py pipeline-set --pipeline-key <key> --set <field>=<value>` (one `--set` per field)
     - `pipeline-delete` → `repl_admin.py pipeline-delete --pipeline-key <key>`
     - `notes-comment` → append `\n\n<!-- <ISO timestamp> -->\n<comment>` to `~/.emerge/connectors/<connector>/NOTES.md`
     - `notes-edit` → overwrite `~/.emerge/connectors/<connector>/NOTES.md` entirely
     - `tool-call` → execute exactly `call.tool` + `call.arguments` (deterministic, no free-form reinterpretation)
     - `crystallize-component` → write to `~/.emerge/connectors/<connector>/cockpit/<filename>.html` and `<filename>.context.md`
   Briefly report results after processing.

   **Monitor 2 — operator pattern alerts** (launch regardless; only fires when `EMERGE_OPERATOR_MONITOR=1`):
   ```
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/watch_patterns.py"
   ```
   Set `persistent: true` and `description: "operator pattern alert watcher"`.

   The script watches `pattern-alerts.json` (written by the daemon's OperatorMonitor
   when a recurring pattern is detected). When an alert arrives, evaluate whether to
   engage the operator or crystallize directly. Alerts include `stage`, `intent_signature`,
   `occurrences`, `window_minutes`, and `machine_ids`.

   > **Per-runner agents-team mode**: when spawning a watcher agent for a specific
   > runner profile (e.g. `mycader-1`), launch Monitor 2 as:
   > ```
   > python3 ".../watch_patterns.py --runner-profile mycader-1"
   > ```
   > description: "operator pattern alert watcher — mycader-1"
   > Each watcher receives only alerts for its assigned runner.

   **Stage → Action protocol** (for watcher agents handling pattern alerts):

   | `stage` | Action |
   |---|---|
   | `explore` | Silent — record intent only, no popup |
   | `canary` | `runner_client.notify({"type":"choice","title":"emerge — 可以接管了","body":f"[{intent_signature}] 已见 {occurrences} 次，接管此次操作？","options":["接管","跳过","停止学习"],"timeout_s":15})` |
   | `stable` | `icc_exec(intent_signature=...)` silently; optional info notify after |

   **AI-initiated popups** (any stage, when agent is uncertain or wants to distill knowledge):
   ```python
   runner_client.notify({
       "type": "input",
       "title": "emerge — 需要确认",
       "body": "<question>",
   })
   # action=confirmed, value=<operator answer>
   # → append answer to NOTES.md via notes-comment action
   ```

   **Silence principle**: only interrupt for authorization (canary takeover) or genuine
   ambiguity. Never popup for: execution in progress/completed, read-only queries,
   errors CC can resolve autonomously.

   **Fallback (CC < 2.1.98 / no Monitor tool):** the `UserPromptSubmit` hook also drains
   `pending-actions.processed.json` into `additionalContext` on the next user message.

5. **Close the cockpit**: when the user says close/exit:
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" serve-stop`
