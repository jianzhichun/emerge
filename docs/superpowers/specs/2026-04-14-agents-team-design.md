# Emerge Agents-Team Mode Design

**Date**: 2026-04-14  
**Status**: Approved  
**Scope**: Per-runner monitoring agents, popup/distillation protocol, tray companion

---

## Overview

Agents-team mode lets a main CC session (team lead) spawn one monitoring agent per runner. Each watcher observes its runner's EventBus, detills operator knowledge via popups and chat, and executes pipeline takeovers on approval. The tray companion on the runner machine provides a persistent channel for operator-initiated conversation.

---

## 1. Architecture

```
team lead CC session
  TeamCreate("emerge-monitors")
  ├── spawn "{profile}-watcher"   (one per configured runner)
  │     Monitor: pattern-alerts-{profile}.json
  │     on alert → popup/exec protocol
  │     on operator_chat event → respond/execute
  │     SendMessage(team, result)
  │
  └── cockpit: watch_pending Monitor (unchanged)

runner machine
  ├── remote_runner.py  (HTTP server: /run /notify /operator-event /chat/push /chat/message)
  └── tray_companion.py (system tray + tkinter chat window)
```

**Core principles:**
- team lead orchestrates and aggregates; does not handle alerts directly
- Each watcher is isolated to its runner's alert file
- Popup and execution happen inside the watcher; results reported via SendMessage
- Tray companion is operator-initiated; popups are agent-initiated

---

## 2. Data Model Changes

### 2.1 machine_id → runner_profile mapping

`OperatorMonitor._poll_machine(machine_id, client)` builds and persists a mapping on first successful poll:

```json
// ~/.emerge/runner-machine-map.json
{
  "workstation-A": "mycader-1",
  "workstation-B": "mycader-2"
}
```

### 2.2 Per-runner alert files

`_push_pattern` queries the map and writes to a runner-scoped file:

```
~/.emerge/repl/pattern-alerts-mycader-1.json
~/.emerge/repl/pattern-alerts-mycader-2.json
```

Alert payload gains two fields:

```json
{
  "runner_profile": "mycader-1",
  "machine_id": "workstation-A",
  "stage": "canary",
  "intent_signature": "hypermesh.mesh.batch",
  "meta": { "occurrences": 6, "window_minutes": 12, "machine_ids": ["workstation-A"] }
}
```

**Backward compatibility**: when `runner_profile` is unknown (local-only setup), falls back to `pattern-alerts.json`.

### 2.3 watch_patterns.py parameterized

```bash
python3 watch_patterns.py --runner-profile mycader-1
# watches pattern-alerts-mycader-1.json

python3 watch_patterns.py
# falls back to pattern-alerts.json (existing behaviour)
```

---

## 3. Agents-Team Orchestration

### 3.1 team lead startup

Triggered by `/emerge:monitor` or cockpit action:

```python
TeamCreate("emerge-monitors")

for profile in runner_profiles:
    Agent(
        subagent_type="general-purpose",
        team_name="emerge-monitors",
        name=f"{profile}-watcher",
        prompt=f"""
You are an emerge vertical monitor agent for runner: {profile}.

1. Start Monitor: watch_patterns.py --runner-profile {profile}
2. On pattern alert: apply the stage→action protocol below.
3. Poll EventBus for operator_chat events; respond or execute.
4. Report results to team lead via SendMessage.
5. On shutdown_request from team lead: exit cleanly.

Stage→action protocol:
  explore → silent observation only, record intent
  canary  → runner_client.notify(choice + timeout_s=15)
             options: ["接管", "跳过", "停止学习"]
             接管 → icc_exec; 跳过 → pass; 停止学习 → pipeline freeze
  stable  → icc_exec silently; optional info notify after

AI uncertainty → runner_client.notify(input/confirm) at any stage
Knowledge distillation → runner_client.notify(input) to capture operator reasoning
"""
    )
```

### 3.2 Watcher lifecycle

```
spawn
  └── start Monitor (pattern-alerts-{profile}.json)
  └── idle, waiting for alert or operator_chat

on alert:
  └── apply stage→action
  └── execute if approved
  └── SendMessage(team lead, summary)
  └── idle again

on shutdown_request:
  └── stop Monitor
  └── exit
```

### 3.3 Dynamic member addition

New runner added via `runner-bootstrap` → team lead spawns additional watcher without rebuilding the team:

```python
Agent(team_name="emerge-monitors", name=f"{new_profile}-watcher", ...)
```

### 3.4 Shutdown

```python
SendMessage(to="all", message={"type": "shutdown_request"})
# wait for idle confirmations
TeamDelete()
```

---

## 4. Popup / Stage-to-Action Protocol

### Trigger matrix

| Trigger | Popup type | Notes |
|---|---|---|
| stage=explore | None — silent observation | No action available yet |
| stage=canary | choice + timeout_s=15 | Takeover authorization |
| stage=stable | None — silent execute | Optional info after |
| AI has question | input or confirm | Any stage |
| Knowledge distillation | input | Capture operator reasoning |
| Irreversible action | confirm | Before any destructive exec |

### Canary popup

```python
runner_client.notify({
    "type": "choice",
    "title": "emerge — 可以接管了",
    "body": f"[{intent_signature}] 已见 {occurrences} 次，接管此次操作？",
    "options": ["接管", "跳过", "停止学习"],
    "timeout_s": 15,
})
# value="接管"    → icc_exec(intent_signature=...)
# value="跳过"    → pass
# value="停止学习" → pipeline freeze via repl_admin
```

### Knowledge distillation popup (examples)

```python
# First time a new pattern is observed
runner_client.notify({
    "type": "input",
    "title": "emerge — 帮我理解这个操作",
    "body": f"我观察到你在做 [{intent_signature}]。\n这个步骤的目的是什么？有什么注意事项？",
})

# After AI auto-execution that operator corrected
runner_client.notify({
    "type": "input",
    "title": "emerge — 我哪里做错了？",
    "body": "你修改了我的执行结果，下次应该怎么处理？",
})

# Parameter choice reasoning
runner_client.notify({
    "type": "input",
    "title": "emerge — 参数依据",
    "body": f"这个参数 [{param_name}] 你反复调整，选择依据是什么？",
})
```

All operator answers are appended to `~/.emerge/connectors/{connector}/NOTES.md` via `notes-comment` action.

### Silence principle alignment

Show popup only when operator input genuinely changes the outcome:
- Canary takeover = authorization needed → popup
- AI question = intent unclear → popup
- Execution in progress / completed → no popup
- Read-only / status queries → no popup
- Errors CC can resolve autonomously → no popup

---

## 5. Tray Companion

### 5.1 Components

**`scripts/tray_companion.py`** — persistent process on runner machine:
- `pystray`: system tray icon (Windows/Mac)
- `tkinter`: chat window opened on tray click
- Polls `GET /chat/messages` on local runner for incoming agent messages
- `POST /chat/message` to send operator messages

**`remote_runner.py`** — two new endpoints:

```
POST /chat/push
  body: {"text": "..."}
  → queues message for tray window
  → returns {ok: true}

POST /chat/message
  body: {"text": "..."}
  → writes operator_chat event to EventBus
  → returns {ok: true}

GET /chat/messages
  → returns and clears pending messages [{text, ts_ms}]
```

**`runner_client.py`** — new method:

```python
def chat_push(self, text: str) -> None:
    """Push agent message to operator's tray chat window."""
    ...
```

### 5.2 EventBus event format (operator-initiated)

```json
{
  "event_type": "operator_chat",
  "text": "帮我做一次完整的 mesh 导出",
  "machine_id": "workstation-A",
  "ts_ms": 1234567890
}
```

### 5.3 Watcher response to operator_chat

```
operator: "帮我做一次完整的 mesh 导出"
  → stable pipeline exists → icc_exec(intent_signature="hypermesh.export.full")
  → chat_push("已完成 mesh 导出，输出: model_v3.fem")

operator: "这次网格尺寸用 0.3"
  → irreversible with param → notify(confirm)
  → confirmed → icc_exec(params={mesh_size: 0.3})
  → chat_push("已完成")

operator: "刚才那个操作是什么原理？"
  → knowledge Q&A, no execution needed
  → chat_push(explanation)
  → optionally notes-comment if answer is worth persisting
```

**Response priority:**
1. Matches stable pipeline → execute (confirm first if irreversible)
2. Matches canary pipeline → notify(choice) → execute if approved
3. Knowledge Q&A / no matching pipeline → text reply via chat_push

### 5.4 Deployment

`tray_companion.py` is pushed to the runner machine via `runner-deploy`. `runner-bootstrap` documentation updated with startup command:

```bash
python3 tray_companion.py --runner-url http://localhost:8787 &
```

Tray companion connects to the local runner HTTP server only (`localhost`). No direct CC connectivity required.

---

## 6. Implementation Phases

### Phase 1 — Agents-Team MVP

Deliverables:
- `_push_pattern`: per-runner alert files + `runner_profile` field
- `runner-machine-map.json`: machine_id → profile mapping
- `watch_patterns.py`: `--runner-profile` parameter
- cockpit skill update: agents-team spawn protocol + stage→action protocol
- `/emerge:monitor` skill or cockpit button

Result: team lead + per-runner watchers + popup-based interaction fully working.

### Phase 2 — Tray Companion

Deliverables:
- `scripts/tray_companion.py`
- `remote_runner.py`: `/chat/push`, `/chat/message`, `GET /chat/messages`
- `runner_client.py`: `chat_push()`
- `runner-bootstrap` / `runner-deploy` documentation update

Result: operator-initiated chat fully working.

---

## 7. Files Changed

| File | Change |
|---|---|
| `scripts/emerge_daemon.py` | `_push_pattern` → per-runner file + runner_profile field |
| `scripts/operator_monitor.py` | build/persist `runner-machine-map.json` |
| `scripts/watch_patterns.py` | `--runner-profile` arg |
| `scripts/remote_runner.py` | `/chat/push`, `/chat/message`, `GET /chat/messages` |
| `scripts/runner_client.py` | `chat_push()` method |
| `scripts/tray_companion.py` | new file |
| `skills/emerge-cockpit/SKILL.md` | agents-team spawn + stage→action protocol |
| `README.md` | component table, runner operations section |
| `CLAUDE.md` | architecture section, key invariants |
