# /emerge:monitor — Agents-Team Monitor Mode

Spawn a per-runner watcher agent for every configured runner. Each watcher
monitors its runner's EventBus for patterns and applies the stage→action popup
protocol autonomously.

Always invoke the admin CLI via the **Emerge plugin root**.

## Steps

1. **Read configured runner profiles**:
   ```
   python3 "/Users/apple/.claude/plugins/cache/emerge/emerge/0.3.65/scripts/repl_admin.py" runner-status --pretty
   ```
   Extract the list of profile names (e.g. `mycader-1`, `mycader-2`).
   If no runners are configured, inform the user and stop.

2. **Create the monitor team**:
   ```python
   TeamCreate(team_name="emerge-monitors", description="Per-runner pattern watchers")
   ```

3. **Spawn one watcher per runner profile** (replace `{profile}` for each):
   ```python
   Agent(
       subagent_type="general-purpose",
       team_name="emerge-monitors",
       name="{profile}-watcher",
       prompt="""
   You are an emerge vertical monitor agent for runner: {profile}.

   Setup:
   1. Start Monitor:
      command: "python3 /Users/apple/.claude/plugins/cache/emerge/emerge/0.3.65/scripts/watch_patterns.py --runner-profile {profile}"
      description: "pattern alert watcher — {profile}"
      persistent: true
   2. You are now idle, waiting for pattern alerts.

   On [OperatorMonitor] alert notification:
   - Read `stage` and `intent_signature` from the alert.
   - Apply the stage→action protocol:
     - stage=explore  → silent; record intent in notes if new
     - stage=canary   → call POST /notify on the runner:
         ui_spec: {"type": "choice", "title": "emerge — 可以接管了",
                   "body": "[{intent_signature}] 已见 {occurrences} 次，接管？",
                   "options": ["接管", "跳过", "停止学习"], "timeout_s": 15}
         接管 → icc_exec(intent_signature=...)
         停止学习 → pipeline freeze via repl_admin pipeline-set --set frozen=true
     - stage=stable   → icc_exec(intent_signature=...) silently

   AI uncertainty or knowledge distillation questions → POST /notify with type=input:
     ui_spec: {"type": "input", "title": "emerge — 需要确认", "body": "<question>"}
     Store answer in NOTES.md via notes-comment cockpit action.

   - SendMessage(team_lead, summary of action taken)
   - Return to idle.

   On shutdown_request from team lead:
   - Stop your Monitor.
   - Exit cleanly.
   """
   )
   ```
   Repeat Agent spawn for each profile found in step 1.

4. **Confirm team is running**:
   Report to operator: team `emerge-monitors` created with N watchers.
   List each watcher name and its assigned runner profile.

5. **Shutdown** (when operator says stop/exit monitors):
   ```python
   SendMessage(to="all", message={"type": "shutdown_request"})
   # Wait ~5s for confirmations, then:
   TeamDelete()
   ```

## Dynamic addition

When a new runner is bootstrapped mid-session:
```python
Agent(team_name="emerge-monitors", name="{new_profile}-watcher", prompt=<same as above with new_profile>)
```
No need to recreate the team.

## Notes

- Each watcher's Monitor fires as a notification in the watcher's own conversation.
- The team lead does not process pattern alerts directly — that is the watcher's job.
- Knowledge distillation answers (from input popups) are written to
  `~/.emerge/connectors/{connector}/NOTES.md` via `notes-comment` cockpit action.
- Silence principle: only interrupt for authorization (canary takeover) or genuine
  ambiguity. Never popup for execution in progress/completed, read-only queries,
  or errors CC can resolve autonomously.
