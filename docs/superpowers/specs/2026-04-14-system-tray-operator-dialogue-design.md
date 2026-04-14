# System Tray Operator Dialogue — Design Spec

## Goal

Add a persistent system tray icon to the emerge runner process (Windows system tray / Mac menu bar) that lets the operator proactively send free-form text messages to the watcher agent. The watcher treats operator messages as natural language instructions, acts autonomously (reply and/or execute runner actions), and returns a non-blocking toast notification with the result.

## Constraints

- Both Windows and Mac must be supported.
- UI model: lightweight bubble (one Q&A, no history retained).
- Input: free-form text only; watcher decides how to handle it.
- No new processes on the runner machine — tray logic lives in `remote_runner.py`.
- No new file-based channels — reuses the existing `POST /runner/event` → `events-{profile}.jsonl` pipeline.

---

## Architecture

### Message flow

```
Operator clicks tray icon
  → show_input_bubble() opens tkinter input bubble
  → Operator types message + submits
  → _post_operator_message(text)
      → POST /runner/event  {type: "operator_message", text, profile, machine_id, ts_ms}
  → daemon._on_runner_event() writes to events-{profile}.jsonl  (no special handling needed)
  → watcher Monitor (watch_emerge.py) fires with operator_message event
  → watcher processes as natural language instruction
  → watcher calls runner_notify(runner_profile, {type: "toast", body: "..."})
  → daemon SSE → runner show_notify()
  → tkinter toast bubble appears, auto-dismisses after timeout_s
```

### What is NOT changed

- `watch_emerge.py` — zero changes; `operator_message` flows through as a normal event.
- `hooks/` — no changes.
- Cockpit, daemon event routing, watcher Monitor logic — no changes.

---

## Components

### 1. `scripts/remote_runner.py`

**`_start_tray()`**

Starts a `pystray` system tray icon in a background daemon thread when the runner initialises. Icon uses a simple 16×16 PIL `Image` (solid colour with "E" label). Right-click menu: "Send message" + "Quit". On headless Linux (no display), the function logs a warning and returns without error — runner continues normally.

**`show_input_bubble(on_submit: Callable[[str], None])`**

Opens a minimal `tkinter.Toplevel` window (always-on-top, no title bar decorations):
- Single-line `Entry` widget for operator text.
- "Send" button + Enter key binding call `on_submit(text)` then close the window.
- "Cancel" / window-close discards input silently.

**`_post_operator_message(text: str)`**

Calls the runner's existing `_forward_event_to_daemon()` method with:
```json
{
  "type": "operator_message",
  "text": "<operator input>",
  "profile": "<runner_profile>",
  "machine_id": "<machine_id>",
  "ts_ms": <epoch_ms>
}
```
On connection failure: `_forward_event_to_daemon` is already best-effort (swallows exceptions); `_post_operator_message` adds a fallback call to open a small error toast ("发送失败，daemon 未连接") so the operator knows it didn't go through.

**`show_notify()` — toast extension**

Current implementation blocks waiting for `popup-result`. Add a branch for `ui_spec["type"] == "toast"`:
- Open a non-interactive `tkinter.Toplevel` with `body` text.
- Auto-close after `timeout_s` (default 5) via `after()`.
- Does **not** post to `/runner/popup-result`.

### 2. `scripts/daemon_http.py`

`_on_runner_event` already writes all incoming runner events to `events-{profile}.jsonl` generically — `operator_message` events pass through with no code change needed.

Optional one-liner addition: `self._cockpit.broadcast({"operator_message": True})` to let cockpit reflect operator activity in the Monitors tab (low priority).

### 3. `scripts/emerge_daemon.py`

Update `runner_notify` MCP tool schema description to document the `toast` ui_spec type:

```python
# ui_spec variants:
# {"type": "choice", "title": "...", "body": "...", "options": [...], "timeout_s": N}
# {"type": "input",  "title": "...", "body": "..."}
# {"type": "toast",  "body": "...", "timeout_s": N}   ← new
```

No logic changes in the daemon — `runner_notify` already sends the `show_notify` SSE command generically.

---

## Event Schema

### `operator_message` event (in `events-{profile}.jsonl`)

```json
{
  "type": "operator_message",
  "text": "暂停这个 pipeline",
  "profile": "mycader-1",
  "machine_id": "WIN-ABC123",
  "ts_ms": 1713100000000
}
```

### `toast` ui_spec (passed to `runner_notify`)

```json
{
  "type": "toast",
  "body": "已暂停 mycader.write.submit_form",
  "timeout_s": 5
}
```

---

## Error Handling

| Scenario | Behaviour |
|---|---|
| No display (headless Linux) | `_start_tray()` logs warning, skips silently |
| Operator closes bubble without submitting | No event sent |
| `_post_operator_message` fails (daemon offline) | Error toast shown on screen, no crash |
| Watcher ignores operator_message | No reply toast; operator sees no feedback (acceptable for MVP) |
| pystray import fails | `ImportError` caught at startup, tray feature disabled, runner continues |

---

## Dependencies

| Package | Purpose | Already present? |
|---|---|---|
| `pystray` | System tray icon, cross-platform | No — add to runner requirements |
| `Pillow` | PIL Image for tray icon | Usually present; add to requirements if absent |
| `tkinter` | Input bubble + toast windows | Stdlib on CPython (Windows/Mac) |

Runner machines install deps via `runner-deploy` → `pip install` step.

---

## Testing

### `tests/test_remote_runner.py` additions

- `test_post_operator_message_sends_correct_payload` — mock `_post_event`, verify payload fields.
- `test_post_operator_message_handles_connection_error` — `_forward_event_to_daemon` raises, verify no exception propagates and error toast is triggered.
- `test_show_notify_toast_does_not_post_popup_result` — mock tkinter, verify `popup-result` endpoint not called.
- `test_start_tray_skips_gracefully_when_pystray_unavailable` — mock `import pystray` to raise `ImportError`, verify runner init proceeds.

### `tests/test_mcp_tools_integration.py` additions

- `test_operator_message_event_written_to_events_file` — simulate `POST /runner/event` with `operator_message` payload, verify event appears in `events-{profile}.jsonl`.

---

## Out of Scope (MVP)

- Message history / conversation log.
- Operator-initiated message from cockpit Web UI.
- Linux desktop environment support (headless skip is sufficient for now).
- Authentication / message signing between operator and daemon.
