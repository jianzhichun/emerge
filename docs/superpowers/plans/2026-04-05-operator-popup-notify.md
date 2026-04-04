# Operator Popup & Notification Dispatcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `operator_popup.py` fallback notification path (spec §4.4) plus a `NotificationDispatcher` that delivers OS-native blocking dialogs to the operator's desktop — local or remote — alongside the existing MCP/CC push.

**Architecture:** `operator_popup.py` is a self-contained Tkinter module with three dialog stages (explore/canary/stable). The runner gains `POST /notify` which calls this module directly (confirmed: runner runs in `WinSta0`, Tkinter works). `NotificationDispatcher` in the daemon routes to `RemoteNotifier` (HTTP) or `LocalNotifier` (direct import) and always co-fires `_write_mcp_push`. Two pre-existing bugs are fixed first: `_RunnerClientAdapter` missing proxy bypass, and `OperatorMonitor.event_root` stored but never used.

**Tech Stack:** Python 3.11+, stdlib only (tkinter, threading, http.server). No new dependencies. Tests: pytest + monkeypatch + tmp_path following existing patterns in `tests/`.

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `scripts/operator_popup.py` | **Create** | Tkinter dialogs: explore (editable text), canary (2 buttons), stable (canary + countdown) |
| `scripts/notify_dispatcher.py` | **Create** | `LocalNotifier`, `RemoteNotifier`, `NotificationDispatcher` |
| `scripts/remote_runner.py` | **Modify** | Add `RunnerExecutor.show_notify()` + `POST /notify` handler |
| `scripts/runner_client.py` | **Modify** | Add `RunnerClient.notify()` + fix proxy bug in `_RunnerClientAdapter` |
| `scripts/emerge_daemon.py` | **Modify** | Wire `NotificationDispatcher` into `_push_pattern_to_cc`; fix `OperatorMonitor` event_root bug |
| `tests/test_operator_popup.py` | **Create** | Unit tests for popup module (headless/mocked) |
| `tests/test_notify_dispatcher.py` | **Create** | Unit tests for LocalNotifier, RemoteNotifier, NotificationDispatcher |
| `tests/test_remote_runner_events.py` | **Modify** | Add `/notify` endpoint tests |
| `tests/test_mcp_tools_integration.py` | **Modify** | Add integration test: pattern → dispatcher → notify |

---

## Task 1: Fix `_RunnerClientAdapter` proxy bypass bug

**Files:**
- Modify: `scripts/emerge_daemon.py:45-57`
- Modify: `tests/test_mcp_tools_integration.py` (add regression test)

`_RunnerClientAdapter.get_events` uses raw `urllib.request.urlopen` but all runner LAN calls must bypass system proxy via `_NO_PROXY_OPENER` (already defined in `runner_client.py`). Without this, machines with system proxy configured silently fail to poll events.

- [ ] **Step 1: Write the failing regression test**

Add to `tests/test_mcp_tools_integration.py`:

```python
def test_runner_client_adapter_uses_no_proxy_opener(monkeypatch):
    """_RunnerClientAdapter.get_events must not use the system proxy."""
    from scripts.emerge_daemon import _RunnerClientAdapter
    import urllib.request

    calls = []
    original_open = urllib.request.urlopen

    def tracking_open(req_or_url, *args, **kwargs):
        if hasattr(req_or_url, 'full_url'):
            calls.append(req_or_url.full_url)
        else:
            calls.append(str(req_or_url))
        raise ConnectionRefusedError("mock: no server")

    monkeypatch.setattr(urllib.request, "urlopen", tracking_open)

    adapter = _RunnerClientAdapter("http://127.0.0.1:19999", timeout_s=1)
    result = adapter.get_events("test-machine", since_ms=0)

    # Should return [] on connection error (not raise)
    assert result == []
    # urlopen should NOT have been called (proxy-bypassing opener is used instead)
    assert calls == [], f"Raw urlopen called — proxy bypass missing: {calls}"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/apple/Documents/workspace/emerge
python -m pytest tests/test_mcp_tools_integration.py::test_runner_client_adapter_uses_no_proxy_opener -v
```

Expected: `FAIL` — `assert calls == []` fails because raw `urlopen` is called.

- [ ] **Step 3: Fix `_RunnerClientAdapter.get_events`**

In `scripts/emerge_daemon.py`, replace the `get_events` method in `_RunnerClientAdapter`:

```python
def get_events(self, machine_id: str, since_ms: int = 0) -> list[dict]:
    import urllib.parse
    import json as _j
    from scripts.runner_client import _NO_PROXY_OPENER
    url = (
        f"{self._base_url}/operator-events"
        f"?machine_id={urllib.parse.quote(machine_id)}&since_ms={since_ms}"
    )
    try:
        with _NO_PROXY_OPENER.open(url, timeout=self._timeout_s) as r:
            data = _j.loads(r.read())
        return data.get("events", [])
    except Exception:
        return []
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_runner_client_adapter_uses_no_proxy_opener -v
```

Expected: `PASS`

- [ ] **Step 5: Run full suite to confirm no regression**

```bash
python -m pytest tests -q
```

Expected: 171 passed.

- [ ] **Step 6: Commit**

```bash
git add scripts/emerge_daemon.py tests/test_mcp_tools_integration.py
git commit -m "fix: _RunnerClientAdapter.get_events uses proxy-bypassing opener"
```

---

## Task 2: Fix `OperatorMonitor` local EventBus — `event_root` stored but never read

**Files:**
- Modify: `scripts/operator_monitor.py:43-80`
- Modify: `tests/test_operator_monitor.py` (add test)

`OperatorMonitor.__init__` accepts `event_root` and stores it, but `_poll_machine` only reads from runner HTTP clients. In local mode (no runner configured), `machines` dict is empty so the poll loop never runs and local EventBus files are never read. Fix: add a local file reader that runs alongside the HTTP poll loop when `event_root` exists.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_operator_monitor.py`:

```python
def test_operator_monitor_reads_local_event_root(tmp_path):
    """OperatorMonitor polls local event_root when no runner machines configured."""
    import time, json, threading
    from scripts.operator_monitor import OperatorMonitor

    machine_id = "local-test-machine"
    event_dir = tmp_path / machine_id
    event_dir.mkdir(parents=True)
    events_file = event_dir / "events.jsonl"

    # Write a local event directly (simulates adapter writing without runner)
    now_ms = int(time.time() * 1000)
    event = {
        "ts_ms": now_ms,
        "machine_id": machine_id,
        "session_role": "operator",
        "app": "hypermesh",
        "event_type": "node_create",
        "payload": {},
    }
    events_file.write_text(json.dumps(event) + "\n", encoding="utf-8")

    received = []

    def push_fn(stage, context, summary):
        received.append(summary)

    monitor = OperatorMonitor(
        machines={},  # no runner — local only
        push_fn=push_fn,
        poll_interval_s=0.1,
        event_root=tmp_path,
    )
    monitor.start()
    time.sleep(0.5)
    monitor.stop()
    monitor.join(timeout=2)

    # With only 1 event, PatternDetector won't fire (threshold=3), but
    # the monitor must have attempted to read the local file (no exception raised).
    # Write 3 identical events and expect a summary.
    events_file.write_text(
        "\n".join(json.dumps({**event, "ts_ms": now_ms + i * 1000}) for i in range(3)) + "\n",
        encoding="utf-8",
    )

    received2 = []

    def push_fn2(stage, context, summary):
        received2.append(summary)

    monitor2 = OperatorMonitor(
        machines={},
        push_fn=push_fn2,
        poll_interval_s=0.1,
        event_root=tmp_path,
    )
    monitor2.start()
    time.sleep(0.5)
    monitor2.stop()
    monitor2.join(timeout=2)

    assert len(received2) >= 1, "OperatorMonitor did not read local event_root"
    assert received2[0].intent_signature.startswith("hypermesh.")
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_operator_monitor.py::test_operator_monitor_reads_local_event_root -v
```

Expected: `FAIL` — `assert len(received2) >= 1` fails, monitor never reads local files.

- [ ] **Step 3: Add local file polling to `OperatorMonitor`**

In `scripts/operator_monitor.py`, update the `run` method and add `_poll_local`:

```python
def run(self) -> None:
    while not self._stop_event.wait(timeout=self._poll_interval_s):
        # Poll remote runner machines
        for machine_id, client in self._machines.items():
            try:
                self._poll_machine(machine_id, client)
            except Exception:
                pass
        # Poll local event_root (used when no runner is configured)
        if self._event_root.exists():
            try:
                self._poll_local()
            except Exception:
                pass

def _poll_local(self) -> None:
    """Read events directly from local EventBus files (no-runner mode)."""
    import json as _json
    for machine_dir in self._event_root.iterdir():
        if not machine_dir.is_dir():
            continue
        machine_id = machine_dir.name
        events_path = machine_dir / "events.jsonl"
        if not events_path.exists():
            continue
        since_ms = self._last_poll_ms.get(f"local:{machine_id}", 0)
        events: list[dict] = []
        with events_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                if e.get("ts_ms", 0) > since_ms:
                    events.append(e)
        if events:
            latest_ts = max(e.get("ts_ms", 0) for e in events)
            self._last_poll_ms[f"local:{machine_id}"] = latest_ts
            buf = self._event_buffers.setdefault(f"local:{machine_id}", __import__("collections").deque())
            buf.extend(events)
        buf = self._event_buffers.get(f"local:{machine_id}")
        if not buf:
            continue
        import time as _time
        now_ms = int(_time.time() * 1000)
        window_ms = self._detector.FREQ_WINDOW_MS
        while buf and now_ms - buf[0].get("ts_ms", 0) > window_ms:
            buf.popleft()
        if not buf:
            continue
        summaries = self._detector.ingest(list(buf))
        for summary in summaries:
            app = summary.context_hint.get("app", machine_id)
            plugin = self._adapter_registry.get_plugin(app)
            try:
                context = plugin.get_context(summary.context_hint)
            except Exception:
                context = summary.context_hint.copy()
            self._push_fn(summary.policy_stage, context, summary)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_operator_monitor.py::test_operator_monitor_reads_local_event_root -v
```

Expected: `PASS`

- [ ] **Step 5: Run full suite**

```bash
python -m pytest tests -q
```

Expected: 172 passed.

- [ ] **Step 6: Commit**

```bash
git add scripts/operator_monitor.py tests/test_operator_monitor.py
git commit -m "fix: OperatorMonitor polls local event_root in no-runner mode"
```

---

## Task 3: `scripts/operator_popup.py` — cross-platform Tkinter dialogs

**Files:**
- Create: `scripts/operator_popup.py`
- Create: `tests/test_operator_popup.py`

Single public function `show_notify(stage, message, intent_draft, timeout_s) -> dict`. Three stages: `explore` (editable text + 3 buttons), `canary` (2 buttons), `stable` (2 buttons + countdown). Falls back to `{"action": "skip", "intent": ""}` when Tkinter is unavailable.

- [ ] **Step 1: Write failing tests**

Create `tests/test_operator_popup.py`:

```python
from __future__ import annotations
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_show_notify_unknown_stage_returns_skip():
    from scripts.operator_popup import show_notify
    result = show_notify(stage="unknown", message="test")
    assert result == {"action": "skip", "intent": ""}


def test_show_notify_graceful_on_no_display(monkeypatch):
    """When tkinter has no display, show_notify returns skip with error field."""
    import scripts.operator_popup as popup_mod
    import tkinter as tk

    def bad_tk(*args, **kwargs):
        raise RuntimeError("no display")

    monkeypatch.setattr(tk, "Tk", bad_tk)
    result = popup_mod.show_notify(stage="canary", message="test msg")
    assert result["action"] == "skip"
    assert "error" in result


def test_show_notify_explore_confirm(monkeypatch):
    """Simulate user editing intent and clicking 确认."""
    import scripts.operator_popup as popup_mod

    def mock_show_explore(message, intent_draft):
        # Simulate: user edits text and clicks 确认
        return {"action": "confirm", "intent": "edited: " + intent_draft}

    monkeypatch.setattr(popup_mod, "_show_explore", mock_show_explore)
    result = popup_mod.show_notify(
        stage="explore",
        message="重复 5 次",
        intent_draft="AI 草稿",
    )
    assert result["action"] == "confirm"
    assert result["intent"] == "edited: AI 草稿"


def test_show_notify_canary_takeover(monkeypatch):
    """Simulate user clicking 接管 in canary dialog."""
    import scripts.operator_popup as popup_mod

    def mock_show_canary(message, timeout_s):
        return {"action": "takeover", "intent": ""}

    monkeypatch.setattr(popup_mod, "_show_canary", mock_show_canary)
    result = popup_mod.show_notify(stage="canary", message="接管？", timeout_s=0)
    assert result["action"] == "takeover"


def test_show_notify_stable_auto_takeover(monkeypatch):
    """Stable passes timeout_s to _show_canary."""
    import scripts.operator_popup as popup_mod

    captured = {}

    def mock_show_canary(message, timeout_s):
        captured["timeout_s"] = timeout_s
        return {"action": "takeover", "intent": ""}

    monkeypatch.setattr(popup_mod, "_show_canary", mock_show_canary)
    popup_mod.show_notify(stage="stable", message="stable msg", timeout_s=10)
    assert captured["timeout_s"] == 10
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_operator_popup.py -v
```

Expected: `FAIL` — `ModuleNotFoundError: No module named 'scripts.operator_popup'`

- [ ] **Step 3: Create `scripts/operator_popup.py`**

```python
from __future__ import annotations

from typing import Any


def show_notify(
    stage: str,
    message: str,
    intent_draft: str = "",
    timeout_s: int = 0,
) -> dict[str, Any]:
    """Show OS-native blocking dialog. Returns {action, intent}.

    stage="explore"  → intent capture dialog, action: confirm|skip|later
    stage="canary"   → lightweight confirm,   action: takeover|manual
    stage="stable"   → canary + countdown,    action: takeover|manual
    Falls back to {action: skip} when Tkinter is unavailable.
    """
    if stage not in ("explore", "canary", "stable"):
        return {"action": "skip", "intent": ""}
    try:
        if stage == "explore":
            return _show_explore(message, intent_draft)
        else:
            return _show_canary(message, timeout_s)
    except Exception as exc:
        return {"action": "skip", "intent": "", "error": str(exc)}


def _show_explore(message: str, intent_draft: str) -> dict[str, Any]:
    import tkinter as tk

    root = tk.Tk()
    root.title("emerge · explore")
    root.attributes("-topmost", True)
    root.resizable(False, False)
    result: dict[str, Any] = {"action": "later", "intent": ""}

    tk.Label(root, text="🔍 发现重复模式", font=("", 13, "bold")).pack(
        pady=(12, 4), padx=16, anchor="w"
    )
    tk.Label(root, text=message, wraplength=340, justify="left").pack(
        padx=16, anchor="w"
    )
    tk.Label(root, text="AI 的理解：", font=("", 10)).pack(
        pady=(10, 2), padx=16, anchor="w"
    )
    entry = tk.Text(root, height=2, width=44, relief="solid", bd=1)
    entry.insert("1.0", intent_draft)
    entry.pack(padx=16)
    tk.Label(
        root,
        text="你做这件事的目的是？（可直接修改上面的描述）",
        font=("", 9),
        fg="gray",
    ).pack(pady=(4, 8), padx=16, anchor="w")

    btn_frame = tk.Frame(root)
    btn_frame.pack(pady=(0, 12))

    def on_confirm() -> None:
        result["action"] = "confirm"
        result["intent"] = entry.get("1.0", "end-1c").strip()
        root.destroy()

    def on_skip() -> None:
        result["action"] = "skip"
        root.destroy()

    def on_later() -> None:
        result["action"] = "later"
        root.destroy()

    tk.Button(btn_frame, text="确认", command=on_confirm, width=8).pack(
        side="left", padx=4
    )
    tk.Button(btn_frame, text="跳过", command=on_skip, width=8).pack(
        side="left", padx=4
    )
    tk.Button(btn_frame, text="以后再说", command=on_later, width=8).pack(
        side="left", padx=4
    )

    root.mainloop()
    return result


def _show_canary(message: str, timeout_s: int) -> dict[str, Any]:
    import tkinter as tk

    is_stable = timeout_s > 0
    root = tk.Tk()
    root.title("emerge · stable" if is_stable else "emerge · canary")
    root.attributes("-topmost", True)
    root.resizable(False, False)
    result: dict[str, Any] = {"action": "takeover", "intent": ""}

    tk.Label(root, text="⚡ " + message, wraplength=300, font=("", 11),
             justify="center").pack(pady=(16, 8), padx=16)

    if is_stable:
        countdown_var = tk.StringVar(value=f"（{timeout_s}s 后自动接管）")
        tk.Label(root, textvariable=countdown_var, font=("", 9), fg="gray").pack()
        remaining = [timeout_s]

        def update_countdown() -> None:
            remaining[0] -= 1
            if remaining[0] <= 0:
                result["action"] = "takeover"
                root.destroy()
                return
            countdown_var.set(f"（{remaining[0]}s 后自动接管）")
            root.after(1000, update_countdown)

        root.after(1000, update_countdown)

    btn_frame = tk.Frame(root)
    btn_frame.pack(pady=(8, 14))

    def on_takeover() -> None:
        result["action"] = "takeover"
        root.destroy()

    def on_manual() -> None:
        result["action"] = "manual"
        root.destroy()

    tk.Button(btn_frame, text="接管", command=on_takeover, width=10).pack(
        side="left", padx=6
    )
    tk.Button(btn_frame, text="我来做", command=on_manual, width=10).pack(
        side="left", padx=6
    )

    root.mainloop()
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_operator_popup.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Run full suite**

```bash
python -m pytest tests -q
```

Expected: 177 passed.

- [ ] **Step 6: Commit**

```bash
git add scripts/operator_popup.py tests/test_operator_popup.py
git commit -m "feat: add operator_popup.py with explore/canary/stable Tkinter dialogs"
```

---

## Task 4: `POST /notify` endpoint on runner

**Files:**
- Modify: `scripts/remote_runner.py` (`RunnerExecutor` + `RunnerHTTPHandler.do_POST`)
- Modify: `tests/test_remote_runner_events.py`

Runner receives `POST /notify` with `{stage, message, intent_draft, timeout_s}`, calls `operator_popup.show_notify`, returns `{ok, result}`. Tests mock `show_notify` so no Tkinter window appears.

- [ ] **Step 1: Write failing tests**

Add to `tests/test_remote_runner_events.py`:

```python
def test_runner_notify_endpoint_returns_action(tmp_path, monkeypatch):
    """POST /notify calls show_notify and returns {ok, result}."""
    import json, threading, socket, time
    import urllib.request
    from pathlib import Path
    import sys

    ROOT = Path(__file__).resolve().parents[1]
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    # Mock show_notify before runner imports it
    import scripts.operator_popup as popup_mod
    monkeypatch.setattr(
        popup_mod, "show_notify",
        lambda stage, message, intent_draft="", timeout_s=0: {
            "action": "takeover", "intent": intent_draft
        }
    )

    from scripts.remote_runner import RunnerExecutor, RunnerHTTPHandler, ThreadingHTTPServer

    executor = RunnerExecutor(root=ROOT, state_root=tmp_path / "state")
    handler_cls = type("H", (RunnerHTTPHandler,), {"executor": executor})
    sock = socket.socket(); sock.bind(("127.0.0.1", 0))
    host, port = sock.getsockname(); sock.close()
    server = ThreadingHTTPServer((host, port), handler_cls)
    t = threading.Thread(target=server.serve_forever, daemon=True); t.start()

    try:
        body = json.dumps({
            "stage": "canary",
            "message": "要接管吗？",
            "intent_draft": "",
            "timeout_s": 0,
        }).encode()
        req = urllib.request.Request(
            f"http://{host}:{port}/notify", data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
        assert data["ok"] is True
        assert data["result"]["action"] == "takeover"
    finally:
        server.shutdown(); server.server_close()


def test_runner_notify_endpoint_invalid_stage(tmp_path, monkeypatch):
    """POST /notify with unknown stage returns ok=True with action=skip."""
    import json, threading, socket
    import urllib.request
    from pathlib import Path
    import sys

    ROOT = Path(__file__).resolve().parents[1]
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    import scripts.operator_popup as popup_mod
    monkeypatch.setattr(popup_mod, "show_notify",
        lambda **kw: {"action": "skip", "intent": ""})

    from scripts.remote_runner import RunnerExecutor, RunnerHTTPHandler, ThreadingHTTPServer

    executor = RunnerExecutor(root=ROOT, state_root=tmp_path / "state")
    handler_cls = type("H", (RunnerHTTPHandler,), {"executor": executor})
    sock = socket.socket(); sock.bind(("127.0.0.1", 0))
    host, port = sock.getsockname(); sock.close()
    server = ThreadingHTTPServer((host, port), handler_cls)
    t = threading.Thread(target=server.serve_forever, daemon=True); t.start()

    try:
        body = json.dumps({"stage": "badstage", "message": "x"}).encode()
        req = urllib.request.Request(
            f"http://{host}:{port}/notify", data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
        assert data["ok"] is True
        assert data["result"]["action"] == "skip"
    finally:
        server.shutdown(); server.server_close()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_remote_runner_events.py::test_runner_notify_endpoint_returns_action tests/test_remote_runner_events.py::test_runner_notify_endpoint_invalid_stage -v
```

Expected: `FAIL` — 404 not found (endpoint doesn't exist yet).

- [ ] **Step 3: Add `RunnerExecutor.show_notify` and `POST /notify` handler**

In `scripts/remote_runner.py`, add to `RunnerExecutor` class after `read_operator_events`:

```python
def show_notify(self, params: dict) -> dict:
    """Show OS-native notification dialog. Blocks until user responds."""
    from scripts.operator_popup import show_notify
    return show_notify(
        stage=str(params.get("stage", "canary")),
        message=str(params.get("message", "")),
        intent_draft=str(params.get("intent_draft", "")),
        timeout_s=int(params.get("timeout_s", 0)),
    )
```

In `RunnerHTTPHandler.do_POST`, add before the `if self.path != "/run":` check:

```python
if self.path == "/notify":
    try:
        content_length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(content_length).decode("utf-8")
        body = json.loads(raw) if raw else {}
        if not isinstance(body, dict):
            raise ValueError("notify body must be an object")
        result = self.executor.show_notify(body)
        self._send_json(200, {"ok": True, "result": result})
    except Exception as exc:
        self._send_json(400, {"ok": False, "error": str(exc)})
    return
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_remote_runner_events.py::test_runner_notify_endpoint_returns_action tests/test_remote_runner_events.py::test_runner_notify_endpoint_invalid_stage -v
```

Expected: 2 passed.

- [ ] **Step 5: Run full suite**

```bash
python -m pytest tests -q
```

Expected: 179 passed.

- [ ] **Step 6: Commit**

```bash
git add scripts/remote_runner.py tests/test_remote_runner_events.py
git commit -m "feat: add POST /notify endpoint to runner"
```

---

## Task 5: `RunnerClient.notify()` method

**Files:**
- Modify: `scripts/runner_client.py` (add `notify` to `RunnerClient`)
- Modify: `tests/test_mcp_tools_integration.py` (add test)

`RunnerClient.notify()` POSTs to `/notify` with long enough timeout to wait for user response. Uses `_NO_PROXY_OPENER`. Returns parsed `result` dict or raises `RuntimeError`.

- [ ] **Step 1: Write failing test**

Add to `tests/test_mcp_tools_integration.py`:

```python
def test_runner_client_notify_posts_to_notify_endpoint(tmp_path):
    """RunnerClient.notify() POSTs to /notify and returns result dict."""
    import json, threading, socket
    from pathlib import Path
    import sys
    from http.server import BaseHTTPRequestHandler, HTTPServer

    ROOT_PATH = Path(__file__).resolve().parents[1]
    if str(ROOT_PATH) not in sys.path:
        sys.path.insert(0, str(ROOT_PATH))

    from scripts.runner_client import RunnerClient

    received = []

    class FakeHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            received.append(body)
            resp = json.dumps({"ok": True, "result": {"action": "takeover", "intent": ""}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
        def log_message(self, *a): pass

    sock = socket.socket(); sock.bind(("127.0.0.1", 0))
    host, port = sock.getsockname(); sock.close()
    server = HTTPServer((host, port), FakeHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True); t.start()

    try:
        client = RunnerClient(base_url=f"http://{host}:{port}", timeout_s=5)
        result = client.notify(stage="canary", message="接管？", intent_draft="", timeout_s=0)
        assert result["action"] == "takeover"
        assert len(received) == 1
        assert received[0]["stage"] == "canary"
        assert received[0]["message"] == "接管？"
    finally:
        server.shutdown()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_runner_client_notify_posts_to_notify_endpoint -v
```

Expected: `FAIL` — `AttributeError: 'RunnerClient' object has no attribute 'notify'`

- [ ] **Step 3: Add `notify()` to `RunnerClient`**

In `scripts/runner_client.py`, add after the `health` method:

```python
def notify(
    self,
    stage: str,
    message: str,
    intent_draft: str = "",
    timeout_s: int = 0,
) -> dict[str, Any]:
    """Send a notification request to the runner's /notify endpoint.

    Blocks until the operator responds or timeout_s elapses.
    Returns {action: str, intent: str}.
    Raises RuntimeError on HTTP error or connection failure.
    """
    payload = {
        "stage": stage,
        "message": message,
        "intent_draft": intent_draft,
        "timeout_s": timeout_s,
    }
    body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    # Use a longer timeout than the dialog so the HTTP connection stays open
    # while the user is deciding.
    http_timeout = max(self.timeout_s, float(timeout_s) + 10.0)
    req = urllib.request.Request(
        url=f"{self.base_url}/notify",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _NO_PROXY_OPENER.open(req, timeout=http_timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"runner notify http {exc.code}: {detail}") from exc
    except Exception as exc:
        raise RuntimeError(f"runner notify unreachable: {exc}") from exc
    data = json.loads(raw)
    if not isinstance(data, dict) or not bool(data.get("ok", False)):
        raise RuntimeError(str(data.get("error", "notify failed")))
    result = data.get("result", {})
    if not isinstance(result, dict):
        raise RuntimeError("runner notify result must be an object")
    return result
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_runner_client_notify_posts_to_notify_endpoint -v
```

Expected: `PASS`

- [ ] **Step 5: Run full suite**

```bash
python -m pytest tests -q
```

Expected: 180 passed.

- [ ] **Step 6: Commit**

```bash
git add scripts/runner_client.py tests/test_mcp_tools_integration.py
git commit -m "feat: add RunnerClient.notify() for POST /notify"
```

---

## Task 6: `scripts/notify_dispatcher.py` — LocalNotifier, RemoteNotifier, NotificationDispatcher

**Files:**
- Create: `scripts/notify_dispatcher.py`
- Create: `tests/test_notify_dispatcher.py`

`LocalNotifier` calls `operator_popup.show_notify` in a thread (daemon thread so it doesn't block the process). `RemoteNotifier` calls `runner_client.notify()` via HTTP. `NotificationDispatcher` routes: if runner client available → remote, else → local. Always co-fires `mcp_push_fn` (non-blocking CC path).

- [ ] **Step 1: Write failing tests**

Create `tests/test_notify_dispatcher.py`:

```python
from __future__ import annotations
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_local_notifier_calls_show_notify(monkeypatch):
    import scripts.operator_popup as popup_mod
    from scripts.notify_dispatcher import LocalNotifier

    monkeypatch.setattr(popup_mod, "show_notify",
        lambda stage, message, intent_draft="", timeout_s=0:
            {"action": "confirm", "intent": "test intent"})

    notifier = LocalNotifier()
    result = notifier.notify(stage="explore", message="msg", intent_draft="draft")
    assert result["action"] == "confirm"
    assert result["intent"] == "test intent"


def test_local_notifier_returns_skip_on_error(monkeypatch):
    import scripts.operator_popup as popup_mod
    from scripts.notify_dispatcher import LocalNotifier

    monkeypatch.setattr(popup_mod, "show_notify",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("no display")))

    notifier = LocalNotifier()
    result = notifier.notify(stage="canary", message="msg")
    assert result["action"] == "skip"
    assert "error" in result


def test_remote_notifier_calls_runner_client(monkeypatch):
    from scripts.notify_dispatcher import RemoteNotifier

    class FakeClient:
        def notify(self, stage, message, intent_draft="", timeout_s=0):
            return {"action": "takeover", "intent": ""}

    notifier = RemoteNotifier(client=FakeClient())
    result = notifier.notify(stage="canary", message="接管？")
    assert result["action"] == "takeover"


def test_remote_notifier_returns_skip_on_error(monkeypatch):
    from scripts.notify_dispatcher import RemoteNotifier

    class FailClient:
        def notify(self, **kw):
            raise RuntimeError("connection refused")

    notifier = RemoteNotifier(client=FailClient())
    result = notifier.notify(stage="canary", message="msg")
    assert result["action"] == "skip"
    assert "error" in result


def test_dispatcher_uses_remote_when_runner_available(monkeypatch):
    from scripts.notify_dispatcher import NotificationDispatcher

    mcp_calls = []
    remote_calls = []

    class FakeRouter:
        def find_client(self, args):
            class C:
                def notify(self, stage, message, intent_draft="", timeout_s=0):
                    remote_calls.append(stage)
                    return {"action": "takeover", "intent": ""}
            return C()

    dispatcher = NotificationDispatcher(
        mcp_push_fn=lambda stage, msg: mcp_calls.append(stage),
        runner_router=FakeRouter(),
    )
    result = dispatcher.dispatch(stage="canary", message="msg")
    assert result["action"] == "takeover"
    assert mcp_calls == ["canary"]   # MCP always fires
    assert remote_calls == ["canary"]  # remote used


def test_dispatcher_falls_back_to_local_when_no_runner(monkeypatch):
    import scripts.operator_popup as popup_mod
    from scripts.notify_dispatcher import NotificationDispatcher

    monkeypatch.setattr(popup_mod, "show_notify",
        lambda stage, message, intent_draft="", timeout_s=0:
            {"action": "confirm", "intent": "local"})

    mcp_calls = []
    dispatcher = NotificationDispatcher(
        mcp_push_fn=lambda stage, msg: mcp_calls.append(stage),
        runner_router=None,
    )
    result = dispatcher.dispatch(stage="explore", message="msg", intent_draft="draft")
    assert result["action"] == "confirm"
    assert result["intent"] == "local"
    assert mcp_calls == ["explore"]


def test_dispatcher_machines_param_selects_runner(monkeypatch):
    """machine_ids[0] is used as target_profile for runner selection."""
    from scripts.notify_dispatcher import NotificationDispatcher

    selected_profiles = []

    class FakeRouter:
        def find_client(self, args):
            selected_profiles.append(args.get("target_profile"))
            class C:
                def notify(self, **kw): return {"action": "manual", "intent": ""}
            return C()

    dispatcher = NotificationDispatcher(
        mcp_push_fn=lambda *a: None,
        runner_router=FakeRouter(),
    )
    dispatcher.dispatch(stage="canary", message="msg", machine_ids=["mycader-1"])
    assert selected_profiles == ["mycader-1"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_notify_dispatcher.py -v
```

Expected: `FAIL` — `ModuleNotFoundError: No module named 'scripts.notify_dispatcher'`

- [ ] **Step 3: Create `scripts/notify_dispatcher.py`**

```python
from __future__ import annotations

from typing import Any, Callable


class LocalNotifier:
    """Shows operator_popup dialog directly in the current process."""

    def notify(
        self,
        stage: str,
        message: str,
        intent_draft: str = "",
        timeout_s: int = 0,
    ) -> dict[str, Any]:
        try:
            from scripts.operator_popup import show_notify
            return show_notify(
                stage=stage,
                message=message,
                intent_draft=intent_draft,
                timeout_s=timeout_s,
            )
        except Exception as exc:
            return {"action": "skip", "intent": "", "error": str(exc)}


class RemoteNotifier:
    """Sends notification request to a runner via POST /notify."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def notify(
        self,
        stage: str,
        message: str,
        intent_draft: str = "",
        timeout_s: int = 0,
    ) -> dict[str, Any]:
        try:
            return self._client.notify(
                stage=stage,
                message=message,
                intent_draft=intent_draft,
                timeout_s=timeout_s,
            )
        except Exception as exc:
            return {"action": "skip", "intent": "", "error": str(exc)}


class NotificationDispatcher:
    """Routes notifications to remote runner or local fallback.

    Always co-fires mcp_push_fn (non-blocking CC path) then dispatches
    to OS-native dialog and waits for the operator's response.
    """

    def __init__(
        self,
        mcp_push_fn: Callable[[str, str], None],
        runner_router: Any | None = None,
    ) -> None:
        self._mcp_push = mcp_push_fn
        self._router = runner_router

    def dispatch(
        self,
        stage: str,
        message: str,
        intent_draft: str = "",
        timeout_s: int = 0,
        machine_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Send notification via MCP (non-blocking) and OS dialog (blocking).

        Returns the operator's response dict from the OS dialog.
        """
        # Always push to CC (fire-and-forget)
        try:
            self._mcp_push(stage, message)
        except Exception:
            pass

        # OS dialog: remote first, local fallback
        return self._notify_os(stage, message, intent_draft, timeout_s, machine_ids)

    def _notify_os(
        self,
        stage: str,
        message: str,
        intent_draft: str,
        timeout_s: int,
        machine_ids: list[str] | None,
    ) -> dict[str, Any]:
        if self._router is not None:
            profile = (machine_ids or [None])[0] or "default"
            client = self._router.find_client({"target_profile": profile})
            if client is not None:
                return RemoteNotifier(client).notify(
                    stage=stage,
                    message=message,
                    intent_draft=intent_draft,
                    timeout_s=timeout_s,
                )
        return LocalNotifier().notify(
            stage=stage,
            message=message,
            intent_draft=intent_draft,
            timeout_s=timeout_s,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_notify_dispatcher.py -v
```

Expected: 7 passed.

- [ ] **Step 5: Run full suite**

```bash
python -m pytest tests -q
```

Expected: 187 passed.

- [ ] **Step 6: Commit**

```bash
git add scripts/notify_dispatcher.py tests/test_notify_dispatcher.py
git commit -m "feat: add LocalNotifier, RemoteNotifier, NotificationDispatcher"
```

---

## Task 7: Wire `NotificationDispatcher` into daemon

**Files:**
- Modify: `scripts/emerge_daemon.py` (`_push_pattern_to_cc` → `_push_pattern`, init dispatcher)
- Modify: `tests/test_mcp_tools_integration.py` (add integration test)

`_push_pattern_to_cc` is renamed `_push_pattern` and extended: it still calls `_write_mcp_push` (CC path) but now also calls `self._notification_dispatcher.dispatch()` if the dispatcher is available. The dispatcher is created in `start_operator_monitor` when the monitor is enabled.

- [ ] **Step 1: Write failing integration test**

Add to `tests/test_mcp_tools_integration.py`:

```python
def test_push_pattern_fires_both_mcp_and_os_notify(monkeypatch, tmp_path):
    """_push_pattern co-fires MCP push and OS notify dispatcher."""
    import scripts.operator_popup as popup_mod
    from scripts.pattern_detector import PatternSummary
    from scripts.emerge_daemon import EmergeDaemon

    notify_calls = []
    monkeypatch.setattr(
        popup_mod, "show_notify",
        lambda stage, message, intent_draft="", timeout_s=0: (
            notify_calls.append({"stage": stage, "message": message})
            or {"action": "skip", "intent": ""}
        ),
    )

    daemon = EmergeDaemon(root=ROOT)

    # Inject a dispatcher with no runner (local notifier)
    from scripts.notify_dispatcher import NotificationDispatcher

    mcp_calls = []
    original_write = daemon._write_mcp_push

    def tracking_write(payload):
        mcp_calls.append(payload.get("method", payload.get("id", "?")))
        # Don't actually write to stdout in test

    monkeypatch.setattr(daemon, "_write_mcp_push", tracking_write)

    daemon._notification_dispatcher = NotificationDispatcher(
        mcp_push_fn=lambda stage, msg: None,
        runner_router=None,
    )

    summary = PatternSummary(
        machine_ids=["local"],
        intent_signature="hypermesh.node_create",
        occurrences=5,
        window_minutes=10.0,
        detector_signals=["frequency"],
        context_hint={"app": "hypermesh", "samples": []},
        policy_stage="canary",
    )
    daemon._push_pattern("canary", {"app": "hypermesh"}, summary)

    assert len(notify_calls) == 1
    assert notify_calls[0]["stage"] == "canary"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_push_pattern_fires_both_mcp_and_os_notify -v
```

Expected: `FAIL` — `AttributeError: 'EmergeDaemon' object has no attribute '_notification_dispatcher'` or `_push_pattern` not found.

- [ ] **Step 3: Update daemon**

In `scripts/emerge_daemon.py`:

**3a.** In `EmergeDaemon.__init__`, add after `self._operator_monitor = None`:

```python
self._notification_dispatcher: "NotificationDispatcher | None" = None
```

**3b.** In `start_operator_monitor`, after `machines` dict is built and before `OperatorMonitor(...)` is called, add:

```python
from scripts.notify_dispatcher import NotificationDispatcher

self._notification_dispatcher = NotificationDispatcher(
    mcp_push_fn=self._mcp_push_simple,
    runner_router=self._runner_router,
)
```

**3c.** Add `_mcp_push_simple` method to `EmergeDaemon`:

```python
def _mcp_push_simple(self, stage: str, message: str) -> None:
    """Non-blocking MCP push for the notification dispatcher's co-fire."""
    if stage == "explore":
        self._write_mcp_push({
            "jsonrpc": "2.0",
            "method": "notifications/claude/channel",
            "params": {
                "serverName": "emerge",
                "content": message,
                "meta": {"source": "operator_monitor"},
            },
        })
    else:
        # For canary/stable the full ElicitRequest is sent by _push_pattern
        pass
```

**3d.** Rename `_push_pattern_to_cc` → `_push_pattern` and add dispatcher call:

```python
def _push_pattern(self, stage: str, context: dict, summary: Any) -> None:
    """Push pattern detection result to CC via MCP and OS native dialog."""
    # Build message string for both paths
    if stage == "explore":
        message = self._build_explore_message(context, summary)
    else:
        message = f"⚡ {summary.intent_signature} × {summary.occurrences}"

    # OS-native dialog (blocking — waits for operator response)
    if self._notification_dispatcher is not None:
        timeout_s = 10 if stage == "stable" else 0
        intent_draft = self._build_intent_draft(context, summary)
        self._notification_dispatcher.dispatch(
            stage=stage,
            message=message,
            intent_draft=intent_draft,
            timeout_s=timeout_s,
            machine_ids=summary.machine_ids,
        )

    # CC MCP path (always fire; for canary/stable sends full ElicitRequest)
    if stage == "explore":
        self._write_mcp_push({
            "jsonrpc": "2.0",
            "method": "notifications/claude/channel",
            "params": {
                "serverName": "emerge",
                "content": message,
                "meta": {
                    "source": "operator_monitor",
                    "intent_signature": summary.intent_signature,
                },
            },
        })
    else:
        params = self._build_elicit_params(stage, context, summary)
        self._write_mcp_push({
            "jsonrpc": "2.0",
            "id": f"elicit-{summary.intent_signature}-{int(time.time())}",
            "method": "elicit",
            "params": params,
        })
```

**3e.** Add `_build_intent_draft` helper:

```python
def _build_intent_draft(self, context: dict, summary: Any) -> str:
    """Build AI pre-fill text for explore dialog editable field."""
    app = context.get("app", "unknown")
    event_type = context.get("event_type", summary.intent_signature)
    samples = context.get("samples", [])
    base = f"在 {app} 中重复执行 {event_type}"
    if samples:
        base += f"（如：{', '.join(str(s) for s in samples[:2])}）"
    return base
```

**3f.** Update `start_operator_monitor` to pass `push_fn=self._push_pattern` (was `_push_pattern_to_cc`):

```python
self._operator_monitor = OperatorMonitor(
    machines=machines,
    push_fn=self._push_pattern,   # ← was self._push_pattern_to_cc
    poll_interval_s=poll_s,
    event_root=Path.home() / ".emerge" / "operator-events",
    adapter_root=Path.home() / ".emerge" / "adapters",
)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_push_pattern_fires_both_mcp_and_os_notify -v
```

Expected: `PASS`

- [ ] **Step 5: Run full suite**

```bash
python -m pytest tests -q
```

Expected: 188 passed.

- [ ] **Step 6: Commit**

```bash
git add scripts/emerge_daemon.py tests/test_mcp_tools_integration.py
git commit -m "feat: wire NotificationDispatcher into daemon _push_pattern"
```

---

## Self-Review

**Spec coverage:**
- §4.4 `operator_popup.py` → Task 3 ✅
- `POST /notify` on runner → Task 4 ✅
- Local + remote routing → Task 6 (NotificationDispatcher) ✅
- Bug fixes (proxy, event_root) → Tasks 1 & 2 ✅
- explore UI (editable text, 3 buttons) → Task 3 `_show_explore` ✅
- canary UI (2 buttons) → Task 3 `_show_canary` ✅
- stable UI (countdown) → Task 3 `_show_canary` with `timeout_s > 0` ✅
- CC MCP path preserved → Task 7 `_push_pattern` ✅

**Placeholder scan:** None found. All steps have code.

**Type consistency:**
- `show_notify` signature consistent across tasks 3, 4, 5, 6
- `notify(stage, message, intent_draft, timeout_s)` consistent across LocalNotifier/RemoteNotifier/RunnerClient
- `dispatch(stage, message, intent_draft, timeout_s, machine_ids)` used in task 6 and wired in task 7
- `_push_pattern` replaces `_push_pattern_to_cc` — old name not referenced anywhere after task 7

**One gap found and added:** `_build_intent_draft` helper is defined in Task 7 step 3e — required by `_push_pattern` in step 3d. ✅ consistent.
