# Agents-Team Mode — Phase 2 (Tray Companion) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a system-tray companion app on the runner machine that lets operators proactively open a chat window and send messages to their watcher agent at any time.

**Architecture:** Three new endpoints on the runner HTTP server handle the bidirectional chat queue (`/chat/push` for agent→tray, `/chat/message` for tray→EventBus, `GET /chat/messages` for tray to poll). A new `tray_companion.py` script runs persistently on the runner machine, showing a system-tray icon; clicking it opens a tkinter chat window that polls the server and lets the operator type. `runner_client.py` gets a `chat_push()` method so watcher agents can send messages. Watcher agents listen for `operator_chat` events via their existing EventBus poll.

**Tech Stack:** Python 3.10+, `pystray` (tray icon), `tkinter` (chat window), pytest, existing runner HTTP stack.

**Prerequisite:** Phase 1 complete (per-runner alert routing working).

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `scripts/remote_runner.py` | Modify | Add `/chat/push`, `/chat/message`, `GET /chat/messages` endpoints + in-memory queue |
| `scripts/runner_client.py` | Modify | Add `chat_push(text)` method |
| `scripts/tray_companion.py` | Create | System-tray icon + tkinter chat window, polls `/chat/messages`, posts to `/chat/message` |
| `tests/test_remote_runner.py` | Modify | Test new chat endpoints |
| `tests/test_runner_client.py` | Modify | Test `chat_push()` |
| `commands/cockpit.md` | Modify | Document `operator_chat` event handling in watcher prompt |
| `CLAUDE.md` | Modify | Architecture + invariants for chat endpoints |
| `README.md` | Modify | Component table — tray companion |

---

## Task 1: Runner chat queue + /chat/push endpoint

**Files:**
- Modify: `scripts/remote_runner.py`
- Modify: `tests/test_remote_runner.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_remote_runner.py` (find the existing test class/server fixture and add alongside it):

```python
def test_chat_push_queues_message(runner_server):
    """POST /chat/push enqueues message; GET /chat/messages returns and clears it."""
    import urllib.request, json as _j

    base = runner_server.base_url

    # Push a message
    body = _j.dumps({"text": "Hello from agent"}).encode()
    req = urllib.request.Request(
        f"{base}/chat/push",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as r:
        resp = _j.loads(r.read())
    assert resp["ok"] is True

    # Poll and receive
    with urllib.request.urlopen(f"{base}/chat/messages") as r:
        msgs = _j.loads(r.read())
    assert len(msgs) == 1
    assert msgs[0]["text"] == "Hello from agent"

    # Queue is cleared after read
    with urllib.request.urlopen(f"{base}/chat/messages") as r:
        msgs2 = _j.loads(r.read())
    assert msgs2 == []
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_remote_runner.py::test_chat_push_queues_message -q
```

Expected: FAIL — `/chat/push` returns 404.

- [ ] **Step 3: Add in-memory chat queue to RunnerExecutor**

In `scripts/remote_runner.py`, inside `RunnerExecutor.__init__`, add:

```python
self._chat_queue: list[dict] = []
self._chat_lock = threading.Lock()
```

Add two methods to `RunnerExecutor`:

```python
def push_chat_message(self, text: str) -> None:
    """Enqueue an agent message for the tray companion to pick up."""
    import time as _t
    with self._chat_lock:
        self._chat_queue.append({"text": text, "ts_ms": int(_t.time() * 1000)})

def drain_chat_messages(self) -> list[dict]:
    """Return and clear all pending agent messages."""
    with self._chat_lock:
        msgs = list(self._chat_queue)
        self._chat_queue.clear()
    return msgs
```

- [ ] **Step 4: Wire /chat/push and GET /chat/messages into the HTTP handler**

In `_CockpitHandler.do_POST` (the runner's HTTP handler), before the `/run` block:

```python
if self.path == "/chat/push":
    try:
        content_length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(content_length).decode("utf-8")
        body = json.loads(raw) if raw else {}
        if not isinstance(body, dict):
            raise ValueError("body must be an object")
        text = str(body.get("text", "")).strip()
        if not text:
            raise ValueError("text is required")
        self.executor.push_chat_message(text)
        self._send_json(200, {"ok": True})
    except Exception as exc:
        self._send_json(400, {"ok": False, "error": str(exc)})
    return
```

In `do_GET` (or wherever GET is handled), add:

```python
if self.path == "/chat/messages":
    msgs = self.executor.drain_chat_messages()
    self._send_json(200, msgs)
    return
```

- [ ] **Step 5: Run test to verify it passes**

```bash
python -m pytest tests/test_remote_runner.py::test_chat_push_queues_message -q
```

Expected: PASS.

- [ ] **Step 6: Run full remote runner tests**

```bash
python -m pytest tests/test_remote_runner.py -q
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add scripts/remote_runner.py tests/test_remote_runner.py
git commit -m "feat: runner chat queue — /chat/push and GET /chat/messages endpoints"
```

---

## Task 2: Runner /chat/message endpoint (operator → EventBus)

**Files:**
- Modify: `scripts/remote_runner.py`
- Modify: `tests/test_remote_runner.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_remote_runner.py`:

```python
def test_chat_message_writes_operator_chat_event(runner_server, tmp_path):
    """POST /chat/message writes operator_chat event to EventBus."""
    import urllib.request, json as _j, time as _t

    base = runner_server.base_url
    body = _j.dumps({"text": "帮我做一次完整的 mesh 导出", "machine_id": "workstation-A"}).encode()
    req = urllib.request.Request(
        f"{base}/chat/message",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as r:
        resp = _j.loads(r.read())
    assert resp["ok"] is True

    # Verify event written to EventBus
    event_file = runner_server.state_root.parent / "operator-events" / "workstation-A" / "events.jsonl"
    assert event_file.exists()
    events = [_j.loads(line) for line in event_file.read_text().splitlines() if line.strip()]
    chat_events = [e for e in events if e.get("event_type") == "operator_chat"]
    assert len(chat_events) == 1
    assert chat_events[0]["text"] == "帮我做一次完整的 mesh 导出"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_remote_runner.py::test_chat_message_writes_operator_chat_event -q
```

Expected: FAIL — 404.

- [ ] **Step 3: Add /chat/message handler**

In `do_POST`, add before the `/run` block:

```python
if self.path == "/chat/message":
    try:
        import time as _t
        content_length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(content_length).decode("utf-8")
        body = json.loads(raw) if raw else {}
        if not isinstance(body, dict):
            raise ValueError("body must be an object")
        text = str(body.get("text", "")).strip()
        if not text:
            raise ValueError("text is required")
        machine_id = str(body.get("machine_id", "")).strip()
        if not machine_id:
            import socket as _s
            machine_id = _s.gethostname()
        event = {
            "event_type": "operator_chat",
            "text": text,
            "machine_id": machine_id,
            "ts_ms": int(_t.time() * 1000),
        }
        self.executor.write_operator_event(event)
        self._send_json(200, {"ok": True})
    except Exception as exc:
        self._send_json(400, {"ok": False, "error": str(exc)})
    return
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_remote_runner.py::test_chat_message_writes_operator_chat_event -q
```

Expected: PASS.

- [ ] **Step 5: Run full runner tests**

```bash
python -m pytest tests/test_remote_runner.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/remote_runner.py tests/test_remote_runner.py
git commit -m "feat: runner /chat/message writes operator_chat event to EventBus"
```

---

## Task 3: runner_client.chat_push() method

**Files:**
- Modify: `scripts/runner_client.py`
- Modify: `tests/test_runner_client.py` (or create if missing)

- [ ] **Step 1: Write the failing test**

Find or create `tests/test_runner_client.py`. Add:

```python
def test_chat_push_calls_correct_endpoint(monkeypatch):
    """chat_push() POSTs to /chat/push with the message text."""
    from scripts.runner_client import RunnerClient
    import json as _j

    captured = {}

    class FakeResponse:
        def read(self): return _j.dumps({"ok": True}).encode()
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def fake_open(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = _j.loads(req.data)
        return FakeResponse()

    import scripts.runner_client as rc_mod
    monkeypatch.setattr(rc_mod, "_NO_PROXY_OPENER",
                        type("O", (), {"open": staticmethod(fake_open)})())

    client = RunnerClient(base_url="http://localhost:9999", timeout_s=5.0)
    client.chat_push("Hello from agent")

    assert captured["url"] == "http://localhost:9999/chat/push"
    assert captured["body"]["text"] == "Hello from agent"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_runner_client.py::test_chat_push_calls_correct_endpoint -q
```

Expected: FAIL — `RunnerClient` has no `chat_push` method.

- [ ] **Step 3: Add chat_push() to RunnerClient**

In `scripts/runner_client.py`, after the `notify()` method, add:

```python
def chat_push(self, text: str) -> None:
    """Push agent message to the runner's tray companion chat queue.

    Calls POST /chat/push. Fire-and-forget — errors are silently swallowed
    so a missing tray companion never breaks agent execution.
    """
    payload = {"text": text}
    body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    req = urllib.request.Request(
        url=f"{self.base_url}/chat/push",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _NO_PROXY_OPENER.open(req, timeout=self.timeout_s) as resp:
            resp.read()
    except Exception:
        pass  # tray companion may not be running; never block agent
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_runner_client.py::test_chat_push_calls_correct_endpoint -q
```

Expected: PASS.

- [ ] **Step 5: Run full test suite**

```bash
python -m pytest tests -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/runner_client.py tests/test_runner_client.py
git commit -m "feat: RunnerClient.chat_push() — send agent message to tray companion"
```

---

## Task 4: tray_companion.py — system tray + chat window

**Files:**
- Create: `scripts/tray_companion.py`

No unit tests for the GUI itself (tkinter/pystray require a display); manual smoke test described below.

- [ ] **Step 1: Create scripts/tray_companion.py**

```python
#!/usr/bin/env python3
"""Emerge tray companion — system-tray icon + chat window for operator-initiated dialogue.

Run on the operator's machine (runner machine) alongside the runner HTTP server:

    python3 tray_companion.py --runner-url http://localhost:8787 --machine-id workstation-A

Click the tray icon to open a chat window. Type messages to send them to the
watcher agent via EventBus. Agent replies appear in the chat window.
"""
from __future__ import annotations

import argparse
import json
import socket
import threading
import time
import tkinter as tk
import tkinter.scrolledtext as st
import urllib.request
from pathlib import Path
from typing import Any

_NO_PROXY = urllib.request.ProxyHandler({})
_NO_PROXY_OPENER = urllib.request.build_opener(_NO_PROXY)

POLL_INTERVAL_S = 2.0


def _post(base_url: str, path: str, payload: dict) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    req = urllib.request.Request(
        url=f"{base_url}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _NO_PROXY_OPENER.open(req, timeout=10.0) as r:
        return json.loads(r.read())


def _get(base_url: str, path: str) -> Any:
    with _NO_PROXY_OPENER.open(f"{base_url}{path}", timeout=10.0) as r:
        return json.loads(r.read())


class ChatWindow:
    def __init__(self, runner_url: str, machine_id: str) -> None:
        self._runner_url = runner_url
        self._machine_id = machine_id
        self._root: tk.Tk | None = None
        self._log: st.ScrolledText | None = None
        self._entry: tk.Text | None = None
        self._lock = threading.Lock()
        self._open = False

    def open(self) -> None:
        if self._open:
            if self._root:
                self._root.lift()
            return
        self._open = True
        self._root = tk.Tk()
        self._root.title("emerge — agent chat")
        self._root.geometry("420x520")
        self._root.attributes("-topmost", True)
        self._root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._log = st.ScrolledText(self._root, state="disabled", wrap="word",
                                    font=("", 10), height=24)
        self._log.pack(fill="both", expand=True, padx=8, pady=(8, 4))

        frame = tk.Frame(self._root)
        frame.pack(fill="x", padx=8, pady=(0, 8))
        self._entry = tk.Text(frame, height=3, font=("", 10), relief="solid", bd=1)
        self._entry.pack(side="left", fill="x", expand=True, padx=(0, 4))
        tk.Button(frame, text="发送", width=6,
                  command=self._on_send).pack(side="right")
        self._entry.bind("<Return>", lambda e: (self._on_send(), "break"))

        self._append("system", "connected to runner — type a message")
        self._root.mainloop()
        self._open = False

    def _on_close(self) -> None:
        if self._root:
            self._root.destroy()
        self._open = False

    def _on_send(self) -> None:
        if not self._entry:
            return
        text = self._entry.get("1.0", "end-1c").strip()
        if not text:
            return
        self._entry.delete("1.0", "end")
        self._append("you", text)
        try:
            _post(self._runner_url, "/chat/message",
                  {"text": text, "machine_id": self._machine_id})
        except Exception as exc:
            self._append("error", f"send failed: {exc}")

    def _append(self, sender: str, text: str) -> None:
        if not self._log:
            return
        prefix = {"you": "You", "agent": "Agent", "system": "—", "error": "✗"}.get(sender, sender)
        line = f"[{prefix}] {text}\n"
        self._log.configure(state="normal")
        self._log.insert("end", line)
        self._log.configure(state="disabled")
        self._log.see("end")

    def deliver(self, text: str) -> None:
        """Called from background thread to show an agent message."""
        if not self._open or not self._root:
            return
        self._root.after(0, lambda: self._append("agent", text))


class TrayCompanion:
    def __init__(self, runner_url: str, machine_id: str) -> None:
        self._runner_url = runner_url
        self._machine_id = machine_id
        self._chat = ChatWindow(runner_url, machine_id)
        self._stop = threading.Event()

    def _poll_loop(self) -> None:
        while not self._stop.wait(timeout=POLL_INTERVAL_S):
            try:
                msgs = _get(self._runner_url, "/chat/messages")
                if isinstance(msgs, list):
                    for m in msgs:
                        text = str(m.get("text", "")).strip()
                        if text:
                            self._chat.deliver(text)
            except Exception:
                pass

    def run(self) -> None:
        poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        poll_thread.start()

        try:
            import pystray
            from PIL import Image, ImageDraw

            # Minimal 16×16 icon (green circle)
            img = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.ellipse((2, 2, 14, 14), fill=(34, 197, 94, 255))

            menu = pystray.Menu(
                pystray.MenuItem("Open chat", lambda icon, item: self._open_chat()),
                pystray.MenuItem("Quit", lambda icon, item: self._quit(icon)),
            )
            icon = pystray.Icon("emerge", img, "emerge agent", menu)
            icon.run()
        except ImportError:
            # pystray not available — open chat window directly (dev/fallback mode)
            self._open_chat()

    def _open_chat(self) -> None:
        threading.Thread(target=self._chat.open, daemon=False).start()

    def _quit(self, icon: Any) -> None:
        self._stop.set()
        icon.stop()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Emerge tray companion")
    p.add_argument("--runner-url", default="http://localhost:8787",
                   help="Runner HTTP server base URL")
    p.add_argument("--machine-id", default="",
                   help="Machine ID for EventBus events (defaults to hostname)")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    machine_id = args.machine_id.strip() or socket.gethostname()
    TrayCompanion(runner_url=args.runner_url, machine_id=machine_id).run()
```

- [ ] **Step 2: Smoke test (manual)**

On the runner machine (or locally if a runner is running):

```bash
# Terminal 1: start the runner
python3 scripts/remote_runner.py

# Terminal 2: start tray companion
python3 scripts/tray_companion.py --runner-url http://localhost:8787

# Verify: tray icon appears (or chat window opens in fallback mode)
# Type a message → check runner logs for /chat/message hit
# From another terminal, push a message:
curl -s -X POST http://localhost:8787/chat/push \
  -H "Content-Type: application/json" \
  -d '{"text":"Hello from agent"}' | python3 -m json.tool
# → chat window should display "Hello from agent"
```

- [ ] **Step 3: Commit**

```bash
git add scripts/tray_companion.py
git commit -m "feat: tray_companion.py — system tray + chat window for operator-initiated dialogue"
```

---

## Task 5: Update cockpit.md — operator_chat handling in watcher prompt

**Files:**
- Modify: `commands/cockpit.md` and/or `commands/monitor.md`

- [ ] **Step 1: Add operator_chat section to monitor.md watcher prompt**

In `commands/monitor.md`, inside the watcher agent prompt, add after the stage→action section:

```
On operator_chat event (operator sent a message via tray companion):
- The event arrives as a new entry in the runner's EventBus.
- Read `text` from the event.
- Determine action:
  a. Text matches a known intent → execute pipeline (confirm if irreversible):
       icc_exec(intent_signature="<matched_sig>")
       runner_client.chat_push("<result summary>") via icc_exec script
  b. Text is a question → generate answer, push via chat_push
  c. Text is knowledge/instruction ("下次用参数X") →
       notes-comment to NOTES.md, then chat_push confirmation
- Always respond via runner_client.chat_push() so answer appears in tray window.
```

- [ ] **Step 2: Commit**

```bash
git add commands/monitor.md
git commit -m "docs: monitor — operator_chat event handling in watcher prompt"
```

---

## Task 6: Documentation + version bump

**Files:**
- Modify: `.claude-plugin/plugin.json`
- Modify: `CLAUDE.md`
- Modify: `README.md`

- [ ] **Step 1: Bump version to 0.3.67**

In `.claude-plugin/plugin.json`: `"version": "0.3.67"`.

- [ ] **Step 2: Add tray companion to CLAUDE.md Architecture**

Add after the agents-team entry added in Phase 1:

```
**Tray companion**: `scripts/tray_companion.py` runs on the runner machine (deployed via `runner-deploy`). pystray tray icon + tkinter chat window. Polls `GET /chat/messages` (runner endpoint) every 2s for agent messages. Sends operator messages via `POST /chat/message` → EventBus `operator_chat` event → watcher agent reads and responds. `runner_client.chat_push(text)` is the agent-side send method. Fire-and-forget: errors are swallowed so a missing tray companion never blocks agent execution.
```

- [ ] **Step 3: Add to CLAUDE.md Key Invariants**

```
- **chat_push fire-and-forget invariant**: `RunnerClient.chat_push()` swallows all exceptions. Tray companion is optional infrastructure; its absence must never prevent watcher agents from executing pipelines or processing alerts.
- **operator_chat event shape**: `{event_type: "operator_chat", text: str, machine_id: str, ts_ms: int}`. Written by runner `/chat/message` endpoint. Never filtered by PatternDetector (no `intent_signature`). Watcher agent handles directly.
```

- [ ] **Step 4: Update runner operations section in README.md**

Add tray companion startup to the "Remote runner — operations" section:

```markdown
# Start tray companion on the runner machine (after runner-bootstrap)
python3 scripts/tray_companion.py --runner-url http://localhost:8787 &
# Requires: pystray, Pillow (pip install pystray Pillow)
```

- [ ] **Step 5: Run full test suite**

```bash
python -m pytest tests -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add .claude-plugin/plugin.json CLAUDE.md README.md
git commit -m "chore: bump to 0.3.67; document tray companion"
```

---

## Self-Review

**Spec coverage check:**

| Spec section | Task |
|---|---|
| 5.1 tray_companion.py + pystray + tkinter | Task 4 |
| 5.1 POST /chat/push | Task 1 |
| 5.1 POST /chat/message → EventBus | Task 2 |
| 5.1 GET /chat/messages | Task 1 |
| 5.1 runner_client.chat_push() | Task 3 |
| 5.2 operator→agent flow | Task 2 + Task 5 |
| 5.3 runner-bootstrap/deploy documentation | Task 6 |
| 5.4 operator_chat event format | Task 2 + Task 6 |

**Dependency note**: `pystray` and `Pillow` are new runtime dependencies for the tray companion only. They are not imported in the daemon or runner — only in `tray_companion.py`. No changes to `requirements.txt` or `setup.py` are needed unless the project uses dependency management files. Add to runner machine setup docs only.
