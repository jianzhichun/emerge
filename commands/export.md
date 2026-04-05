---
description: Export a connector asset package (pipelines + registry) to a zip file
---

Export a named connector from `~/.emerge/connectors/<name>/` along with its pipeline registry entries into a portable zip file.

Always invoke the admin CLI via the **Emerge plugin root**. Claude Code expands `${CLAUDE_PLUGIN_ROOT}` to that path.

Steps:
1. Parse the user's message to extract the connector name and optional output path.
   - If no connector name is given, ask for it before proceeding.
   - Default output path: `<connector>-emerge-pkg.zip` in the current directory.
2. Run:
   ```
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" connector-export --connector <name> --out <out_path>
   ```
3. Report the result:
   - On success: show connector name, output file path, number of files, number of pipeline registry entries included.
   - On error (e.g. connector not found): show the error message and suggest running `connector://` resource to list available connectors.
