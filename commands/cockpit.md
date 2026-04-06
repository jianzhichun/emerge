---
description: Emerge flywheel cockpit — interactive browser dashboard
---

Open the Emerge cockpit dashboard for the active session.

Always invoke the admin CLI via the **Emerge plugin root** (not the user's open project). Claude Code expands `${CLAUDE_PLUGIN_ROOT}` to that path when this command runs.

Steps:

1. **启动服务器**（后台运行，`run_in_background: true`）：
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" serve --open --port 0`
   - 幂等：已有实例时直接返回已有 URL。
   - 等 1 秒后读取输出，提取 URL（`Cockpit running at http://localhost:PORT`）。

2. **打印状态摘要**：
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" policy-status --pretty`
   告知用户：URL、pipeline 总数/explore/canary/stable、有 consecutive_failures 的 pipeline。

3. **读取 connector 资产**：
   - 扫描 `~/.emerge/connectors/`，读各 connector 的 `NOTES.md`、`scenarios/*.yaml`、`cockpit/*.html`
   - 为有丰富资产的 connector 生成 HTML 组件，inject 到 `POST http://localhost:<PORT>/api/inject-component`

4. **进入 dispatch 循环**（核心）：

   反复执行以下步骤，直到用户明确说"关闭驾驶舱"：

   a. 调用（阻塞，最长等 10 分钟）：
      `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" wait-for-submit`
      - 该命令阻塞直到用户在浏览器点击 Submit，返回 `{"ok": true, "actions": [...], "action_count": N}`
      - 若返回 `{"ok": false, "timeout": true}`：说明 10 分钟内无提交，重新调用（驾驶舱还开着）

   b. 收到 actions 后，按序执行每条：
      - `pipeline-set` → `repl_admin.py pipeline-set --pipeline-key <key> --set <field>=<value>`（每个 field 一个 --set）
      - `pipeline-delete` → `repl_admin.py pipeline-delete --pipeline-key <key>`
      - `notes-comment` → 在 `~/.emerge/connectors/<connector>/NOTES.md` 末尾追加 `\n\n<!-- <ISO timestamp> -->\n<comment>`
      - `notes-edit` → 整体覆写 `~/.emerge/connectors/<connector>/NOTES.md`
      - `scenario-run` → `icc_exec` with `intent_signature=write.<connector>.apply-test`，在 args 中加入 `scenario=<name>` 及其他参数
      - `crystallize-component` → 写入 `~/.emerge/connectors/<connector>/cockpit/<filename>.html` 和 `<filename>.context.md`

   c. 执行完毕后，在终端简要汇报结果，然后回到步骤 a 继续等待下一次提交。

5. **关闭驾驶舱**：用户说关闭/退出时：
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" serve-stop`
