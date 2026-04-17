# /emerge:cockpit — Open Cockpit Dashboard

The cockpit server is started automatically by the daemon. This command opens it.

Always invoke the admin CLI via the **Emerge plugin root** (not the user's open project). Claude Code expands `${CLAUDE_PLUGIN_ROOT}` to that path when this command runs.

## Steps

1. **Get cockpit URL**:
   When the Emerge HTTP daemon is running (default from `SessionStart`), **Cockpit is on the same port as MCP** — typically **`http://localhost:8789/`** (same host/port as `plugin.json` `url` for `/mcp`, but open the root `/` in a browser).
   If the daemon is not running (or you need a standalone cockpit without MCP):
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" serve --open --port 0
   ```
   Read output to extract `Cockpit running at http://localhost:PORT`.

2. **Print URL to user**:
   Report: "Cockpit running at <URL>" (prefer daemon URL `http://localhost:8789/` when the daemon is up).

3. **Discover connector assets and inject controls**:
   Cockpit's "Controls" panel sources HTML from two places:
   - **Disk-persisted** — `~/.emerge/connectors/<connector>/cockpit/*.html` (with optional `<name>.context.md`). These are auto-surfaced by `GET /api/assets`; no action needed.
   - **Runtime-injected** — session-only HTML pushed via `POST /api/inject-component`. Used when an adapter wants to expose a live control before crystallization.

   Enumerate what's available and report it to the user:
   ```bash
   curl -s http://localhost:8789/api/assets | python3 -m json.tool
   ```
   For each connector in the response, list the notes/components the cockpit will show. If a connector has `cockpit/*.html` but no matching `.context.md`, mention it (authoring hint).

   When CC wants to push a runtime-only control (e.g. an adapter-generated preview), inject it with:
   ```bash
   curl -s -X POST http://localhost:8789/api/inject-component \
     -H 'Content-Type: application/json' \
     -d '{"connector":"<name>","html":"<!-- fragment -->","id":"<slot-id>","replace":false}'
   ```
   `id` makes the injection idempotent (re-injecting updates the same slot). `replace:true` clears all prior slots for that connector. Runtime slots appear as `injected-runtime-N.html` in the Controls list — crystallize them to disk to persist.

   **Controls contract (required):**
   - Controls **must not** call `/api/submit` directly.
   - Controls enqueue actions via `postMessage` to parent shell only (`window.emerge.enqueue(...)` from `/api/cockpit-sdk.js`).
   - Operator reviews queue and submits once from Cockpit's queue panel (single submit path).
   - Valid action types are discoverable from `GET /api/action-types` and use dotted names (`intent.set`, `notes.comment`, `core.tool-call`, etc.).

4. **Start global Monitor** (team lead session):
   ```
   Monitor(command="python3 ${CLAUDE_PLUGIN_ROOT}/scripts/watch_emerge.py",
           description="emerge event stream — global",
           persistent=true)
   ```

5. **Start per-runner Monitors** for each connected runner profile:
   Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" runner-status --pretty` to list runners.
   For each runner profile found:
   ```
   Monitor(command="python3 ${CLAUDE_PLUGIN_ROOT}/scripts/watch_emerge.py --runner-profile {profile}",
           description="emerge event stream — {profile}",
           persistent=true)
   ```

6. **Print policy status**:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" policy-status --pretty
   ```
