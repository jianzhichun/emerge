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

3. **Start global Monitor** (team lead session):
   ```
   Monitor(command="python3 ${CLAUDE_PLUGIN_ROOT}/scripts/watch_emerge.py",
           description="emerge event stream — global",
           persistent=true)
   ```

4. **Start per-runner Monitors** for each connected runner profile:
   Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" runner-status --pretty` to list runners.
   For each runner profile found:
   ```
   Monitor(command="python3 ${CLAUDE_PLUGIN_ROOT}/scripts/watch_emerge.py --runner-profile {profile}",
           description="emerge event stream — {profile}",
           persistent=true)
   ```

5. **Print policy status**:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" policy-status --pretty
   ```
