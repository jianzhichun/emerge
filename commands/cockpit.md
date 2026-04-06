---
description: Emerge flywheel cockpit — interactive browser dashboard
---

Open the Emerge cockpit dashboard for the active session.

Always invoke the admin CLI via the **Emerge plugin root** (not the user's open project). Claude Code expands `${CLAUDE_PLUGIN_ROOT}` to that path when this command runs.

Steps:
1. Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" serve --open --port 0` **in the background** (`run_in_background: true`).
   - The server is idempotent: if already running it returns the existing URL immediately.
   - Wait ~1 second, then read the output to extract the URL (`Cockpit running at http://localhost:PORT`).
2. Also print the policy text summary for quick reference:
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
6. **Cockpit监听模式（重要）**：启动后，CC 进入驾驶舱监听循环：
   - 每隔 3 秒检查 `~/.emerge/repl/pending-actions.json` 是否存在
   - 发现后：读取 actions 列表，按序执行每条操作，然后将文件重命名为 `pending-actions.processed.json`
   - 执行规则：
     - `pipeline-set` → `repl_admin.py pipeline-set --pipeline-key <key> --set <field>=<val>`
     - `pipeline-delete` → `repl_admin.py pipeline-delete --pipeline-key <key>`
     - `notes-comment` → 在 `~/.emerge/connectors/<connector>/NOTES.md` 末尾追加 `\n<!-- <timestamp> -->\n<comment>`
     - `notes-edit` → 整体替换 `~/.emerge/connectors/<connector>/NOTES.md`
     - `scenario-run` → 调用 `icc_exec` with `intent_signature=write.<connector>.apply-test`，args 里含 `scenario=<name>`
     - `crystallize-component` → 写入 `~/.emerge/connectors/<connector>/cockpit/<filename>.html` 和 `.context.md`
   - 循环继续，直到用户明确说"关闭驾驶舱"或"stop cockpit"
   - **不要依赖 MCP 通知**——CC 自己主动轮询文件，这是最可靠的方式

7. **关闭驾驶舱**：当用户说关闭/退出时，运行：
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" serve-stop`
