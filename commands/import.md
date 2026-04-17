---
description: Import a connector asset package from a zip file
---

Import a connector asset package (produced by `connector-export`) into `~/.emerge/connectors/` and merge its intent registry entries into the active session.

Always invoke the admin CLI via the **Emerge plugin root**. Claude Code expands `${CLAUDE_PLUGIN_ROOT}` to that path.

Steps:
1. Parse the user's message to extract the package path and whether `--overwrite` is requested.
   - If no package path is given, ask for it before proceeding.
2. Run:
   ```
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" connector-import --pkg <path> [--overwrite]
   ```
3. Report the result:
   - On success: show connector name, number of files extracted, intent registry entries merged vs skipped.
   - On conflict error (connector already exists, no `--overwrite`): inform the user and offer to re-run with `--overwrite`.
   - On other error: show the error message.
