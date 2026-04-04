# Notification Dialogue System Design

**Goal:** Replace fixed-stage popup with a flexible `ui_spec`-driven dialogue that CC orchestrates. Daemon only detects and notifies; CC decides whether and how to engage the operator.

**Architecture:** OperatorMonitor sends a lightweight MCP channel notification to CC. CC reads `policy_stage` and acts: `stable` → crystallize silently; `explore` → call `icc_exec` → `show_notify(ui_spec)` on target runner → dialogue; `canary` → CC judgment. Multi-turn dialogue is sequential `icc_exec` calls. Daemon never triggers popups.

**Tech Stack:** Python stdlib (tkinter), existing `icc_exec`/`POST /notify` path, no new MCP tools.

---

## 1. `show_notify` API

Replace `(stage, message, intent_draft, timeout_s)` with a single `ui_spec` dict.

```python
show_notify(ui_spec: dict) -> dict
```

### ui_spec schema

| Field | Type | Required | Notes |
|---|---|---|---|
| `type` | `"choice"` \| `"input"` \| `"confirm"` \| `"info"` | yes | determines UI layout |
| `body` | str | yes | main message text |
| `title` | str | no | window title, default `"emerge"` |
| `options` | list[str] | for `choice` | button labels, first is default |
| `prefill` | str | for `input` | pre-filled text in editable field |
| `timeout_s` | int | no | `>0` → countdown, auto-select `options[0]` on expiry |

### return value

```json
{"action": "selected" | "confirmed" | "dismissed" | "skip", "value": "<option or typed text>"}
```

- `"skip"` — Tkinter unavailable or unknown type
- `"dismissed"` — window closed without selection
- `"value"` — selected option text (choice) or typed text (input), empty string otherwise

### UI types

**`choice`** — N buttons, one click selects. With `timeout_s`: countdown label + auto-fires `options[0]`.

**`input`** — editable text area prefilled with `prefill`, plus 确认 / 跳过 buttons.

**`confirm`** — two buttons: 确认 / 取消. Shorthand for `choice` with two options.

**`info`** — single 关闭 button. No response needed; `value` is always `""`.

---

## 2. Daemon: `_push_pattern` simplified

`_push_pattern` sends **only** a MCP `notifications/claude/channel` notification. No popup, no dispatcher.

```json
{
  "jsonrpc": "2.0",
  "method": "notifications/claude/channel",
  "params": {
    "serverName": "emerge",
    "content": "[OperatorMonitor] hypermesh.node_create × 8",
    "meta": {
      "source": "operator_monitor",
      "intent_signature": "hypermesh.node_create",
      "policy_stage": "explore",
      "occurrences": 8,
      "window_minutes": 10,
      "machine_ids": ["mycader-1"]
    }
  }
}
```

`NotificationDispatcher` is removed from `start_operator_monitor` and `_push_pattern`. The dispatcher class stays in `notify_dispatcher.py` but is no longer wired into the daemon automatically.

---

## 3. CC dialogue pattern

CC receives the channel notification and reads `meta.policy_stage`:

### stable
```python
# CC calls icc_crystallize — no popup
icc_crystallize(intent_signature="hypermesh.node_create", ...)
```

### explore
```python
# Turn 1: capture intent
result = icc_exec(code="""
from scripts.operator_popup import show_notify
return show_notify(ui_spec={
    "type": "input",
    "body": "你在做什么？",
    "prefill": "在底板均匀分布节点",   # AI pre-fill
})
""", target_profile="mycader-1")

# result = {"action": "confirmed", "value": "沿 Y 轴 50mm 间距创建加强筋节点"}

# Turn 2: confirm takeover
result2 = icc_exec(code="""
from scripts.operator_popup import show_notify
return show_notify(ui_spec={
    "type": "choice",
    "body": "我来接管？",
    "options": ["好", "不用"],
    "timeout_s": 15,
})
""", target_profile="mycader-1")

# If "好": icc_write(tcl_cmd=...) — silent execution
```

### canary
CC judgment based on risk. Low-risk: crystallize directly. High-risk or ambiguous: brief `confirm` dialog with `timeout_s`.

---

## 4. Silence principle

Encoded in `CLAUDE.md` as a rule for CC behavior, not in code.

**Show popup only when:**
- Operator input genuinely changes the outcome (intent unclear, fork in execution)
- Action is high-risk and irreversible (delete components, overwrite model)

**Never show popup for:**
- Execution started / in progress / completed
- Read-only operations (icc_read, state queries)
- Status updates of any kind
- Errors that CC can resolve autonomously

Default is silence. Interrupt only when necessary.

---

## 5. File changes

| File | Change |
|---|---|
| `scripts/operator_popup.py` | Replace `show_notify(stage, message, intent_draft, timeout_s)` with `show_notify(ui_spec: dict)` |
| `scripts/notify_dispatcher.py` | Update `LocalNotifier.notify` and `RemoteNotifier.notify` to accept `ui_spec` |
| `scripts/runner_client.py` | `RunnerClient.notify(ui_spec: dict)` — replace keyword params |
| `scripts/remote_runner.py` | `POST /notify` body: `{ui_spec: {...}}` instead of flat fields |
| `scripts/emerge_daemon.py` | `_push_pattern`: remove dispatcher call, MCP notification only; remove `_notification_dispatcher` from `start_operator_monitor` |
| `CLAUDE.md` | Add silence principle under `## Key Invariants` |
| `tests/test_operator_popup.py` | Update tests for new API |
| `tests/test_notify_dispatcher.py` | Update tests for new API |
| `tests/test_remote_runner_events.py` | Update `/notify` endpoint tests |
| `tests/test_mcp_tools_integration.py` | Update integration tests |
