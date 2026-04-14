# System Tray Operator Dialogue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a persistent system tray icon to the emerge runner (Windows/Mac) so operators can send free-form messages to the watcher agent, with the watcher replying via a non-blocking toast notification.

**Architecture:** Tray icon lives in `RunnerExecutor._start_tray()` (pystray, background thread). Operator input is forwarded to daemon via the existing `POST /runner/event` → `events-{profile}.jsonl` pipeline as an `operator_message` event. Watcher replies by calling `runner_notify` with `{type: "toast"}`, which the daemon sends via SSE and the runner renders as a non-blocking tkinter popup.

**Tech Stack:** `pystray` (tray icon, cross-platform), `Pillow` (tray icon image), `tkinter` (stdlib, input bubble + toast), existing runner HTTP + daemon SSE channels.

---

## File Structure

| File | Change |
|---|---|
| `scripts/operator_popup.py` | Add `_render_toast()`, add toast branch in `show_notify()`, add `show_input_bubble()` |
| `scripts/remote_runner.py` | `_forward_event_to_daemon()` returns bool; add `_post_operator_message()`, `_start_tray()`; wire tray into `run_server()`; `RunnerSSEClient._dispatch_command` skips `_post_result` for toast |
| `scripts/daemon_http.py` | `request_popup()` short-circuits for toast; `_on_runner_event()` preserves `operator_message` type |
| `scripts/emerge_daemon.py` | Update `runner_notify` `ui_spec` description to document toast type |
| `CLAUDE.md` | Add toast variant to runner_notify bullet |
| `tests/test_operator_popup.py` | Add toast + input bubble tests |
| `tests/test_remote_runner.py` | Add dispatch, _post_operator_message, _start_tray tests |

---

### Task 1: Toast support — `operator_popup.py` + `RunnerSSEClient` + `daemon_http`

**Files:**
- Modify: `scripts/operator_popup.py:20,38`
- Modify: `scripts/remote_runner.py:399-408` (`RunnerSSEClient._dispatch_command`)
- Modify: `scripts/daemon_http.py:256-286` (`request_popup`)
- Modify: `scripts/daemon_http.py:183-188` (`_on_runner_event`)
- Test: `tests/test_operator_popup.py`

- [ ] **Step 1: Write failing tests for toast**

Add to `tests/test_operator_popup.py`:

```python
def test_show_notify_toast_returns_dismissed(monkeypatch):
    import scripts.operator_popup as popup_mod
    monkeypatch.setattr(popup_mod, "_render_toast",
        lambda *, body, timeout_s: {"action": "dismissed", "value": ""})
    result = popup_mod.show_notify({"type": "toast", "body": "完成", "timeout_s": 3})
    assert result == {"action": "dismissed", "value": ""}


def test_show_notify_toast_default_timeout(monkeypatch):
    import scripts.operator_popup as popup_mod
    captured: dict = {}
    monkeypatch.setattr(popup_mod, "_render_toast",
        lambda *, body, timeout_s: (captured.update({"timeout_s": timeout_s})
                                    or {"action": "dismissed", "value": ""}))
    popup_mod.show_notify({"type": "toast", "body": "x"})
    assert captured["timeout_s"] == 5


def test_dispatch_command_toast_skips_post_result():
    from scripts.remote_runner import RunnerSSEClient
    post_result_calls: list = []
    client = RunnerSSEClient.__new__(RunnerSSEClient)
    client._show_notify = lambda spec: {"action": "dismissed", "value": ""}
    client._post_result = lambda popup_id, result: post_result_calls.append(popup_id)
    client._dispatch_command({
        "type": "notify",
        "popup_id": "abc",
        "ui_spec": {"type": "toast", "body": "test"},
    })
    assert post_result_calls == []


def test_dispatch_command_non_toast_posts_result():
    from scripts.remote_runner import RunnerSSEClient
    post_result_calls: list = []
    client = RunnerSSEClient.__new__(RunnerSSEClient)
    client._show_notify = lambda spec: {"action": "selected", "value": "好"}
    client._post_result = lambda popup_id, result: post_result_calls.append(popup_id)
    client._dispatch_command({
        "type": "notify",
        "popup_id": "xyz",
        "ui_spec": {"type": "choice", "body": "接管？", "options": ["好"]},
    })
    assert post_result_calls == ["xyz"]
```

- [ ] **Step 2: Run tests to verify they fail**

```
python -m pytest tests/test_operator_popup.py::test_show_notify_toast_returns_dismissed tests/test_operator_popup.py::test_dispatch_command_toast_skips_post_result -v
```

Expected: FAIL — `_render_toast` not found, toast type not routed.

- [ ] **Step 3: Add toast rendering to `scripts/operator_popup.py`**

Change the type allowlist (line 21):
```python
    if ui_type not in ("choice", "input", "confirm", "info", "toast"):
```

Add toast branch before the `# info` comment (after line 37 `if ui_type == "confirm":`):
```python
        if ui_type == "toast":
            body = str(ui_spec.get("body", ""))
            timeout_s = max(1, int(ui_spec.get("timeout_s", 5)))
            return _render_toast(body=body, timeout_s=timeout_s)
```

Add `_render_toast` after the `_render_info` function at the end of the file:
```python
def _render_toast(*, body: str, timeout_s: int) -> dict[str, Any]:
    """Non-interactive toast bubble that auto-dismisses after timeout_s seconds."""
    import tkinter as tk
    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.update_idletasks()
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    w, h = 300, 64
    root.geometry(f"{w}x{h}+{sw - w - 20}+{sh - h - 60}")
    tk.Label(root, text=body, wraplength=280, font=("", 10), justify="left").pack(
        pady=8, padx=12, anchor="w"
    )
    root.after(timeout_s * 1000, root.destroy)
    root.mainloop()
    return {"action": "dismissed", "value": ""}
```

- [ ] **Step 4: Skip `_post_result` for toast in `RunnerSSEClient._dispatch_command` (`scripts/remote_runner.py`)**

Change lines 404-408:
```python
    def _dispatch_command(self, cmd: dict) -> None:
        cmd_type = cmd.get("type")
        if cmd_type == "notify":
            popup_id = str(cmd.get("popup_id", ""))
            ui_spec = cmd.get("ui_spec", {})
            try:
                result = self._show_notify(ui_spec)
            except Exception:
                result = {"value": None}
            if ui_spec.get("type") != "toast":
                self._post_result(popup_id, result)
```

- [ ] **Step 5: Short-circuit `request_popup` for toast in `scripts/daemon_http.py`**

Add at the start of `request_popup` (before `popup_id = uuid.uuid4().hex`):
```python
    def request_popup(self, runner_profile: str, ui_spec: dict, timeout_s: float = 30.0) -> dict:
        """Send popup to runner via SSE, wait for result. Blocks calling thread.
        For type='toast', fires SSE and returns immediately without waiting."""
        if ui_spec.get("type") == "toast":
            command = json.dumps({"type": "notify", "popup_id": "", "ui_spec": ui_spec})
            with self._runners_lock:
                wfile = self._runner_sse_clients.get(runner_profile)
            if wfile is None:
                return {"ok": False, "error": "runner_not_connected"}
            try:
                wfile.write(f"data: {command}\n\n".encode())
                wfile.flush()
                return {"ok": True}
            except OSError:
                with self._runners_lock:
                    self._runner_sse_clients.pop(runner_profile, None)
                return {"ok": False, "error": "runner_disconnected"}
        # --- existing blocking logic below unchanged ---
        popup_id = uuid.uuid4().hex
        ...
```

- [ ] **Step 6: Preserve `operator_message` type in `_on_runner_event` (`scripts/daemon_http.py`)**

Change lines 183-188:
```python
            _orig_type = payload.get("type", "")
            _written_type = _orig_type if _orig_type == "operator_message" else "runner_event"
            self._append_event(self._state_root / f"events-{runner_profile}.jsonl", {
                "type": _written_type,
                "ts_ms": ts_ms,
                "runner_profile": runner_profile,
                **{k: v for k, v in payload.items()
                   if k not in ("runner_profile", "type")},
            })
```

- [ ] **Step 7: Run tests to verify they pass**

```
python -m pytest tests/test_operator_popup.py -v
```

Expected: All tests PASS (including 4 new ones).

- [ ] **Step 8: Run full suite to check for regressions**

```
python -m pytest tests -q
```

Expected: All tests pass.

- [ ] **Step 9: Commit**

```bash
git add scripts/operator_popup.py scripts/remote_runner.py scripts/daemon_http.py tests/test_operator_popup.py
git commit -m "feat: toast ui_spec type — fire-and-forget non-blocking popup"
```

---

### Task 2: Input bubble — `operator_popup.show_input_bubble()`

**Files:**
- Modify: `scripts/operator_popup.py` (append after `_render_toast`)
- Test: `tests/test_operator_popup.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_operator_popup.py`:

```python
def test_show_input_bubble_calls_on_submit_with_stripped_text(monkeypatch):
    """show_input_bubble calls on_submit(text) with stripped non-empty text."""
    import scripts.operator_popup as popup_mod
    import tkinter as tk

    submitted: list = []
    captured_cmds: dict = {}
    entry_val = "  hello  "

    class _MockEntry:
        def pack(self, **kw): pass
        def get(self): return entry_val
        def focus_set(self): pass
        def bind(self, *a, **kw): pass

    class _MockRoot:
        def title(self, *a): pass
        def attributes(self, *a, **kw): pass
        def resizable(self, *a): pass
        def destroy(self): pass
        def mainloop(self):
            captured_cmds["send"]()  # simulate Send button click

    def _mock_button(parent, *, text, command, width=None):
        b = type("_B", (), {"pack": staticmethod(lambda **kw: None)})()
        if text == "发送":
            captured_cmds["send"] = command
        return b

    monkeypatch.setattr(tk, "Tk", _MockRoot)
    monkeypatch.setattr(tk, "Label",
        lambda *a, **kw: type("_L", (), {"pack": staticmethod(lambda **kw: None)})())
    monkeypatch.setattr(tk, "Entry", lambda *a, **kw: _MockEntry())
    monkeypatch.setattr(tk, "Frame",
        lambda *a, **kw: type("_F", (), {"pack": staticmethod(lambda **kw: None)})())
    monkeypatch.setattr(tk, "Button", _mock_button)

    popup_mod.show_input_bubble(lambda text: submitted.append(text))
    assert submitted == ["hello"]


def test_show_input_bubble_skips_empty_text(monkeypatch):
    """show_input_bubble does NOT call on_submit when entry is blank."""
    import scripts.operator_popup as popup_mod
    import tkinter as tk

    submitted: list = []
    captured_cmds: dict = {}

    class _MockEntry:
        def pack(self, **kw): pass
        def get(self): return "   "  # blank
        def focus_set(self): pass
        def bind(self, *a, **kw): pass

    class _MockRoot:
        def title(self, *a): pass
        def attributes(self, *a, **kw): pass
        def resizable(self, *a): pass
        def destroy(self): pass
        def mainloop(self):
            captured_cmds["send"]()

    def _mock_button(parent, *, text, command, width=None):
        b = type("_B", (), {"pack": staticmethod(lambda **kw: None)})()
        if text == "发送":
            captured_cmds["send"] = command
        return b

    monkeypatch.setattr(tk, "Tk", _MockRoot)
    monkeypatch.setattr(tk, "Label",
        lambda *a, **kw: type("_L", (), {"pack": staticmethod(lambda **kw: None)})())
    monkeypatch.setattr(tk, "Entry", lambda *a, **kw: _MockEntry())
    monkeypatch.setattr(tk, "Frame",
        lambda *a, **kw: type("_F", (), {"pack": staticmethod(lambda **kw: None)})())
    monkeypatch.setattr(tk, "Button", _mock_button)

    popup_mod.show_input_bubble(lambda text: submitted.append(text))
    assert submitted == []
```

- [ ] **Step 2: Run tests to verify they fail**

```
python -m pytest tests/test_operator_popup.py::test_show_input_bubble_calls_on_submit_with_stripped_text -v
```

Expected: FAIL — `show_input_bubble` not found.

- [ ] **Step 3: Add `show_input_bubble` to `scripts/operator_popup.py`**

Append after `_render_toast`:

```python
def show_input_bubble(on_submit: "Callable[[str], None]") -> None:
    """Open a minimal tkinter input bubble.

    Calls on_submit(text) with the stripped text when the operator clicks Send
    or presses Enter.  Silently closes on Cancel or empty input.
    """
    import tkinter as tk
    root = tk.Tk()
    root.title("emerge")
    root.attributes("-topmost", True)
    root.resizable(False, False)
    tk.Label(root, text="发送消息给 watcher:", font=("", 10)).pack(
        pady=(10, 4), padx=12, anchor="w"
    )
    entry = tk.Entry(root, width=40, relief="solid", bd=1)
    entry.pack(padx=12, pady=(0, 6))
    entry.focus_set()
    btn_frame = tk.Frame(root)
    btn_frame.pack(pady=(0, 10))

    def _on_send() -> None:
        text = entry.get().strip()
        root.destroy()
        if text:
            on_submit(text)

    entry.bind("<Return>", lambda _e: _on_send())
    tk.Button(btn_frame, text="发送", command=_on_send, width=8).pack(side="left", padx=4)
    tk.Button(btn_frame, text="取消", command=root.destroy, width=8).pack(side="left", padx=4)
    root.mainloop()
```

Also add `Callable` to the top-level import. `operator_popup.py` currently has `from typing import Any` — update to:
```python
from typing import Any, Callable
```

- [ ] **Step 4: Run tests to verify they pass**

```
python -m pytest tests/test_operator_popup.py -v
```

Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/operator_popup.py tests/test_operator_popup.py
git commit -m "feat: show_input_bubble — operator tray text entry"
```

---

### Task 3: Operator message pipeline — `_post_operator_message` + `_forward_event_to_daemon` bool return

**Files:**
- Modify: `scripts/remote_runner.py:90-103` (`_forward_event_to_daemon`)
- Modify: `scripts/remote_runner.py` (add `_post_operator_message` after `_forward_event_to_daemon`)
- Test: `tests/test_remote_runner.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_remote_runner.py`:

```python
def test_forward_event_to_daemon_returns_false_on_failure(tmp_path):
    """_forward_event_to_daemon returns False when the daemon is unreachable."""
    executor = RunnerExecutor(root=ROOT, state_root=tmp_path / "state")
    executor._team_lead_url = "http://127.0.0.1:19999"  # nothing listening
    executor._runner_profile = "test-profile"
    result = executor._forward_event_to_daemon({"type": "test"})
    assert result is False


def test_post_operator_message_sends_correct_payload(tmp_path):
    """_post_operator_message calls _forward_event_to_daemon with required fields."""
    executor = RunnerExecutor(root=ROOT, state_root=tmp_path / "state")
    executor._team_lead_url = "http://localhost:9999"
    executor._runner_profile = "mycader-1"
    captured: list = []
    executor._forward_event_to_daemon = lambda event: (captured.append(event), True)[1]
    executor._post_operator_message("暂停 pipeline")
    assert len(captured) == 1
    ev = captured[0]
    assert ev["type"] == "operator_message"
    assert ev["text"] == "暂停 pipeline"
    assert ev["profile"] == "mycader-1"
    assert isinstance(ev["ts_ms"], int)
    assert "machine_id" in ev


def test_post_operator_message_shows_error_toast_on_failure(tmp_path, monkeypatch):
    """_post_operator_message shows error toast when daemon is unreachable."""
    import scripts.operator_popup as popup_mod
    toast_bodies: list = []
    monkeypatch.setattr(popup_mod, "_render_toast",
        lambda *, body, timeout_s: (toast_bodies.append(body), {"action": "dismissed", "value": ""})[1])
    executor = RunnerExecutor(root=ROOT, state_root=tmp_path / "state")
    executor._team_lead_url = "http://localhost:9999"
    executor._runner_profile = "mycader-1"
    executor._forward_event_to_daemon = lambda event: False
    executor._post_operator_message("test message")
    assert len(toast_bodies) == 1
    assert "失败" in toast_bodies[0]
```

- [ ] **Step 2: Run tests to verify they fail**

```
python -m pytest tests/test_remote_runner.py::test_post_operator_message_sends_correct_payload -v
```

Expected: FAIL — `_post_operator_message` not defined.

- [ ] **Step 3: Refactor `_forward_event_to_daemon` to return bool**

Change `scripts/remote_runner.py` lines 90-103:

```python
    def _forward_event_to_daemon(self, event: dict) -> bool:
        """Forward event to team lead daemon. Best-effort, never blocks operator.

        Returns True on success, False on connection failure.
        """
        import urllib.request as _ur
        import urllib.error as _ue
        import json as _j
        url = f"{self._team_lead_url}/runner/event"
        payload = {**event, "runner_profile": self._runner_profile}
        body = _j.dumps(payload, ensure_ascii=True).encode()
        req = _ur.Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            with _ur.urlopen(req, timeout=3):
                pass
            return True
        except (_ue.URLError, OSError):
            return False  # best-effort, never block operator
```

- [ ] **Step 4: Add `_post_operator_message` to `RunnerExecutor`**

Add after `_forward_event_to_daemon` in `scripts/remote_runner.py`:

```python
    def _post_operator_message(self, text: str) -> None:
        """Forward operator tray message to daemon as an operator_message event.

        Shows a non-blocking error toast if the daemon is unreachable.
        """
        import socket as _sock
        import time as _time
        try:
            machine_id = _sock.gethostname()
        except OSError:
            machine_id = "unknown"
        event = {
            "type": "operator_message",
            "text": text,
            "profile": self._runner_profile,
            "machine_id": machine_id,
            "ts_ms": int(_time.time() * 1000),
        }
        ok = bool(
            self._team_lead_url
            and self._runner_profile
            and self._forward_event_to_daemon(event)
        )
        if not ok:
            try:
                from scripts.operator_popup import show_notify
                show_notify({"type": "toast", "body": "发送失败，daemon 未连接", "timeout_s": 4})
            except Exception:
                pass
```

- [ ] **Step 5: Run tests to verify they pass**

```
python -m pytest tests/test_remote_runner.py::test_forward_event_to_daemon_returns_false_on_failure tests/test_remote_runner.py::test_post_operator_message_sends_correct_payload tests/test_remote_runner.py::test_post_operator_message_shows_error_toast_on_failure -v
```

Expected: All 3 PASS.

- [ ] **Step 6: Run full suite**

```
python -m pytest tests -q
```

Expected: All tests pass.

- [ ] **Step 7: Commit**

```bash
git add scripts/remote_runner.py tests/test_remote_runner.py
git commit -m "feat: _post_operator_message — tray→daemon operator_message event"
```

---

### Task 4: System tray icon — `_start_tray` + wire into `run_server`

**Files:**
- Modify: `scripts/remote_runner.py` (add `_start_tray` to `RunnerExecutor`, update `run_server`)
- Test: `tests/test_remote_runner.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_remote_runner.py`:

```python
def test_start_tray_skips_when_pystray_unavailable(tmp_path, monkeypatch):
    """_start_tray must return without error when pystray is not installed."""
    import sys
    monkeypatch.setitem(sys.modules, "pystray", None)
    executor = RunnerExecutor(root=ROOT, state_root=tmp_path / "state")
    executor._start_tray()  # must not raise


def test_start_tray_skips_when_pillow_unavailable(tmp_path, monkeypatch):
    """_start_tray must return without error when Pillow (PIL) is not installed."""
    import sys
    monkeypatch.setitem(sys.modules, "pystray", None)
    monkeypatch.setitem(sys.modules, "PIL", None)
    monkeypatch.setitem(sys.modules, "PIL.Image", None)
    monkeypatch.setitem(sys.modules, "PIL.ImageDraw", None)
    executor = RunnerExecutor(root=ROOT, state_root=tmp_path / "state")
    executor._start_tray()  # must not raise


def test_start_tray_runs_icon_when_pystray_available(tmp_path, monkeypatch):
    """_start_tray calls icon.run_detached() when pystray is importable."""
    import sys
    import types

    # Build minimal pystray mock
    run_detached_called = []

    class _MockIcon:
        def __init__(self, name, image, title, menu): pass
        def run_detached(self): run_detached_called.append(True)
        def stop(self): pass

    class _MockMenuItem:
        def __init__(self, label, action): pass

    class _MockMenu:
        def __init__(self, *items): pass

    pystray_mock = types.ModuleType("pystray")
    pystray_mock.Icon = _MockIcon
    pystray_mock.MenuItem = _MockMenuItem
    pystray_mock.Menu = _MockMenu
    monkeypatch.setitem(sys.modules, "pystray", pystray_mock)

    # Minimal PIL mock
    class _MockImage:
        @staticmethod
        def new(*a, **kw): return _MockImage()
    class _MockImageDraw:
        @staticmethod
        def Draw(img): return _MockImageDraw()
        def text(self, *a, **kw): pass
    pil_mock = types.ModuleType("PIL")
    pil_image = types.ModuleType("PIL.Image")
    pil_image.new = _MockImage.new
    pil_draw = types.ModuleType("PIL.ImageDraw")
    pil_draw.Draw = _MockImageDraw.Draw
    pil_mock.Image = pil_image
    monkeypatch.setitem(sys.modules, "PIL", pil_mock)
    monkeypatch.setitem(sys.modules, "PIL.Image", pil_image)
    monkeypatch.setitem(sys.modules, "PIL.ImageDraw", pil_draw)

    executor = RunnerExecutor(root=ROOT, state_root=tmp_path / "state")
    executor._start_tray()
    assert run_detached_called == [True]
```

- [ ] **Step 2: Run tests to verify they fail**

```
python -m pytest tests/test_remote_runner.py::test_start_tray_skips_when_pystray_unavailable -v
```

Expected: FAIL — `_start_tray` not defined.

- [ ] **Step 3: Add `_start_tray` to `RunnerExecutor` in `scripts/remote_runner.py`**

Add after `_post_operator_message`:

```python
    def _start_tray(self) -> None:
        """Start system tray icon in a background thread.

        No-op (with a log warning) if pystray or Pillow are not installed,
        so the runner can still operate headlessly without these deps.
        """
        try:
            import pystray
            from PIL import Image, ImageDraw
        except ImportError:
            logging.warning("pystray/Pillow not installed — tray icon disabled")
            return
        try:
            img = Image.new("RGB", (64, 64), color=(30, 30, 30))
            draw = ImageDraw.Draw(img)
            draw.text((20, 16), "E", fill=(255, 255, 255))
        except Exception:
            img = Image.new("RGB", (64, 64), color=(30, 30, 30))

        def _on_send_message(icon: Any, item: Any) -> None:
            from scripts.operator_popup import show_input_bubble
            threading.Thread(
                target=show_input_bubble,
                args=(self._post_operator_message,),
                daemon=True,
            ).start()

        menu = pystray.Menu(
            pystray.MenuItem("发送消息", _on_send_message),
            pystray.MenuItem("退出", lambda icon, item: icon.stop()),
        )
        icon = pystray.Icon("emerge", img, "emerge runner", menu)
        try:
            icon.run_detached()
        except (NotImplementedError, AttributeError):
            # run_detached() not available on this backend — fall back to daemon thread
            threading.Thread(target=icon.run, daemon=True, name="EmergeTrayIcon").start()
```

- [ ] **Step 4: Wire `_start_tray` into `run_server`**

In `run_server()` (around line 461), add `executor._start_tray()` after `_start_sse_client(executor)`:

```python
def run_server(host: str, port: int, *, root: Path | None = None, state_root: Path | None = None) -> None:
    _setup_logging()
    logging.info("emerge-remote-runner starting host=%s port=%d pid=%d", host, port, os.getpid())
    executor = RunnerExecutor(root=root, state_root=state_root)
    _start_sse_client(executor)
    executor._start_tray()
    handler_cls = type(
        "BoundRunnerHTTPHandler",
        (RunnerHTTPHandler,),
        {"executor": executor},
    )
    server = ThreadingHTTPServer((host, port), handler_cls)
    try:
        server.serve_forever()
    finally:
        server.server_close()
```

- [ ] **Step 5: Run tests to verify they pass**

```
python -m pytest tests/test_remote_runner.py::test_start_tray_skips_when_pystray_unavailable tests/test_remote_runner.py::test_start_tray_skips_when_pillow_unavailable tests/test_remote_runner.py::test_start_tray_runs_icon_when_pystray_available -v
```

Expected: All 3 PASS.

- [ ] **Step 6: Run full suite**

```
python -m pytest tests -q
```

Expected: All tests pass.

- [ ] **Step 7: Commit**

```bash
git add scripts/remote_runner.py tests/test_remote_runner.py
git commit -m "feat: _start_tray — pystray system tray icon on runner machine"
```

---

### Task 5: Schema doc update + CLAUDE.md

**Files:**
- Modify: `scripts/emerge_daemon.py:1744`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update `runner_notify` `ui_spec` description in `scripts/emerge_daemon.py`**

Change line 1744:
```python
                                        "description": "Popup spec: {type, title, body, options?, timeout_s?}",
```
To:
```python
                                        "description": (
                                            "Popup spec. type: choice|input|confirm|info|toast. "
                                            "toast is fire-and-forget (no popup-result posted). "
                                            "Other types block until operator responds. "
                                            "Fields: title, body, options (choice), timeout_s."
                                        ),
```

- [ ] **Step 2: Update `runner_notify` bullet in `CLAUDE.md`**

Find the line:
```
- **runner_notify MCP tool**: sends popup commands to a runner via daemon SSE, blocks waiting for `/runner/popup-result` callback (correlation ID: `popup_id`). Requires HTTP daemon mode (`_http_server` is not None).
```

Replace with:
```
- **runner_notify MCP tool**: sends popup commands to a runner via daemon SSE. For `type=toast` (fire-and-forget), `request_popup` returns `{ok: True}` immediately without waiting and the runner does not post a popup-result. For all other types, blocks waiting for `/runner/popup-result` callback (correlation ID: `popup_id`). Requires HTTP daemon mode (`_http_server` is not None).
```

- [ ] **Step 3: Run full test suite one final time**

```
python -m pytest tests -q
```

Expected: All tests pass.

- [ ] **Step 4: Commit**

```bash
git add scripts/emerge_daemon.py CLAUDE.md
git commit -m "docs: document runner_notify toast type in schema and CLAUDE.md"
```
