# Notification ui_spec Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the fixed-stage popup API with a `ui_spec`-driven dialogue that CC orchestrates; daemon sends only a lightweight MCP channel notification for all stages.

**Architecture:** `show_notify(ui_spec: dict)` becomes a flexible renderer with four types (`choice`, `input`, `confirm`, `info`). `NotificationDispatcher` drops `mcp_push_fn` (daemon handles MCP; dispatcher is a routing-only utility). `_push_pattern` in the daemon sends one unified channel notification carrying `policy_stage` in `meta`, removing all elicit/dispatcher calls. No new files.

**Tech Stack:** Python 3.11+ stdlib (tkinter, json). Tests: pytest + monkeypatch.

---

## File Map

| File | Action | What changes |
|---|---|---|
| `scripts/operator_popup.py` | **Rewrite** | `show_notify(ui_spec)` + `_render_choice/input/confirm/info` |
| `scripts/notify_dispatcher.py` | **Modify** | All `notify` sigs → `ui_spec: dict`; drop `mcp_push_fn` from `NotificationDispatcher` |
| `scripts/runner_client.py` | **Modify** | `notify(ui_spec: dict)` — POST body `{"ui_spec": {...}}` |
| `scripts/remote_runner.py` | **Modify** | `show_notify` extracts `ui_spec` from body |
| `scripts/emerge_daemon.py` | **Modify** | `_push_pattern` → channel-only; remove dispatcher, elicit, `_mcp_push_simple`, `_build_intent_draft`, `_build_elicit_params` |
| `CLAUDE.md` | **Modify** | Add silence principle to Key Invariants |
| `tests/test_operator_popup.py` | **Rewrite** | 5 → 8 tests for new API |
| `tests/test_notify_dispatcher.py` | **Rewrite** | 7 → 7 tests for new API (no `mcp_calls`) |
| `tests/test_remote_runner_events.py` | **Modify** | 2 notify tests updated for `ui_spec` body |
| `tests/test_mcp_tools_integration.py` | **Modify** | Replace 2 notify/push_pattern tests |

---

## Task 1: Rewrite `operator_popup.py` — `show_notify(ui_spec)`

**Files:**
- Rewrite: `scripts/operator_popup.py`
- Rewrite: `tests/test_operator_popup.py`

- [ ] **Step 1: Write failing tests (replace entire test file)**

Replace `tests/test_operator_popup.py` with:

```python
from __future__ import annotations
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_show_notify_unknown_type_returns_skip():
    from scripts.operator_popup import show_notify
    assert show_notify({"type": "unknown", "body": "x"}) == {"action": "skip", "value": ""}


def test_show_notify_missing_type_or_empty_options_returns_skip():
    from scripts.operator_popup import show_notify
    # missing type
    assert show_notify({"body": "x"}) == {"action": "skip", "value": ""}
    # choice with no options
    assert show_notify({"type": "choice", "body": "x", "options": []}) == {"action": "skip", "value": ""}


def test_show_notify_graceful_on_no_display(monkeypatch):
    import scripts.operator_popup as popup_mod
    import tkinter as tk

    monkeypatch.setattr(tk, "Tk", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("no display")))
    result = popup_mod.show_notify({"type": "choice", "body": "test", "options": ["A"]})
    assert result["action"] == "skip"
    assert "error" in result


def test_show_notify_choice_selected(monkeypatch):
    import scripts.operator_popup as popup_mod

    monkeypatch.setattr(popup_mod, "_render_choice",
        lambda *, body, options, title, timeout_s: {"action": "selected", "value": options[0]})
    result = popup_mod.show_notify({"type": "choice", "body": "接管？", "options": ["好", "不用"]})
    assert result == {"action": "selected", "value": "好"}


def test_show_notify_choice_with_timeout(monkeypatch):
    import scripts.operator_popup as popup_mod
    captured = {}
    monkeypatch.setattr(popup_mod, "_render_choice",
        lambda *, body, options, title, timeout_s: (
            captured.update({"timeout_s": timeout_s}) or {"action": "selected", "value": options[0]}
        ))
    popup_mod.show_notify({"type": "choice", "body": "x", "options": ["A"], "timeout_s": 10})
    assert captured["timeout_s"] == 10


def test_show_notify_input_confirmed(monkeypatch):
    import scripts.operator_popup as popup_mod

    monkeypatch.setattr(popup_mod, "_render_input",
        lambda *, body, prefill, title: {"action": "confirmed", "value": "edited: " + prefill})
    result = popup_mod.show_notify({"type": "input", "body": "你在做什么？", "prefill": "草稿"})
    assert result == {"action": "confirmed", "value": "edited: 草稿"}


def test_show_notify_confirm(monkeypatch):
    import scripts.operator_popup as popup_mod

    monkeypatch.setattr(popup_mod, "_render_confirm",
        lambda *, body, title: {"action": "confirmed", "value": ""})
    result = popup_mod.show_notify({"type": "confirm", "body": "确认？"})
    assert result == {"action": "confirmed", "value": ""}


def test_show_notify_info(monkeypatch):
    import scripts.operator_popup as popup_mod

    monkeypatch.setattr(popup_mod, "_render_info",
        lambda *, body, title: {"action": "dismissed", "value": ""})
    result = popup_mod.show_notify({"type": "info", "body": "完成"})
    assert result == {"action": "dismissed", "value": ""}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/apple/Documents/workspace/emerge
python -m pytest tests/test_operator_popup.py -v
```

Expected: `FAIL` — `assert ... == {"action": "skip", "value": ""}` (old API returns `{"action": "skip", "intent": ""}`)

- [ ] **Step 3: Rewrite `scripts/operator_popup.py`**

```python
from __future__ import annotations

from typing import Any


def show_notify(ui_spec: dict) -> dict[str, Any]:
    """Show OS-native blocking dialog driven by ui_spec.

    ui_spec fields:
      type     : "choice" | "input" | "confirm" | "info"
      body     : str  — main message text
      title    : str  — window title (default "emerge")
      options  : list[str]  — button labels (required for type="choice")
      prefill  : str  — pre-filled text (type="input")
      timeout_s: int  — >0 auto-selects options[0] after countdown (default 0)

    Returns:
      {"action": "selected"|"confirmed"|"dismissed"|"skip", "value": str}
    """
    ui_type = ui_spec.get("type", "")
    if ui_type not in ("choice", "input", "confirm", "info"):
        return {"action": "skip", "value": ""}
    try:
        title = str(ui_spec.get("title", "emerge"))
        body = str(ui_spec.get("body", ""))
        timeout_s = int(ui_spec.get("timeout_s", 0))
        if ui_type == "choice":
            options = [str(o) for o in ui_spec.get("options", [])]
            if not options:
                return {"action": "skip", "value": ""}
            return _render_choice(body=body, options=options, title=title, timeout_s=timeout_s)
        if ui_type == "input":
            prefill = str(ui_spec.get("prefill", ""))
            return _render_input(body=body, prefill=prefill, title=title)
        if ui_type == "confirm":
            return _render_confirm(body=body, title=title)
        # info
        return _render_info(body=body, title=title)
    except Exception as exc:
        return {"action": "skip", "value": "", "error": str(exc)}


def _render_choice(*, body: str, options: list[str], title: str, timeout_s: int) -> dict[str, Any]:
    import tkinter as tk

    root = tk.Tk()
    root.title(title)
    root.attributes("-topmost", True)
    root.resizable(False, False)
    result: dict[str, Any] = {"action": "dismissed", "value": ""}

    tk.Label(root, text=body, wraplength=300, font=("", 11), justify="center").pack(
        pady=(16, 8), padx=16
    )

    if timeout_s > 0:
        countdown_var = tk.StringVar(value=f"（{timeout_s}s 后自动选择 {options[0]}）")
        tk.Label(root, textvariable=countdown_var, font=("", 9), fg="gray").pack()
        remaining = [timeout_s]

        def update_countdown() -> None:
            remaining[0] -= 1
            if remaining[0] <= 0:
                result["action"] = "selected"
                result["value"] = options[0]
                root.destroy()
                return
            countdown_var.set(f"（{remaining[0]}s 后自动选择 {options[0]}）")
            root.after(1000, update_countdown)

        root.after(1000, update_countdown)

    btn_frame = tk.Frame(root)
    btn_frame.pack(pady=(8, 14))

    for opt in options:
        def make_handler(o: str) -> Any:
            def handler() -> None:
                result["action"] = "selected"
                result["value"] = o
                root.destroy()
            return handler

        tk.Button(btn_frame, text=opt, command=make_handler(opt), width=10).pack(
            side="left", padx=6
        )

    root.mainloop()
    return result


def _render_input(*, body: str, prefill: str, title: str) -> dict[str, Any]:
    import tkinter as tk

    root = tk.Tk()
    root.title(title)
    root.attributes("-topmost", True)
    root.resizable(False, False)
    result: dict[str, Any] = {"action": "dismissed", "value": ""}

    tk.Label(root, text=body, wraplength=340, justify="left").pack(
        pady=(12, 4), padx=16, anchor="w"
    )
    entry = tk.Text(root, height=2, width=44, relief="solid", bd=1)
    entry.insert("1.0", prefill)
    entry.pack(padx=16, pady=(4, 8))

    btn_frame = tk.Frame(root)
    btn_frame.pack(pady=(0, 12))

    def on_confirm() -> None:
        result["action"] = "confirmed"
        result["value"] = entry.get("1.0", "end-1c").strip()
        root.destroy()

    def on_dismiss() -> None:
        result["action"] = "dismissed"
        root.destroy()

    tk.Button(btn_frame, text="确认", command=on_confirm, width=8).pack(side="left", padx=4)
    tk.Button(btn_frame, text="跳过", command=on_dismiss, width=8).pack(side="left", padx=4)
    root.mainloop()
    return result


def _render_confirm(*, body: str, title: str) -> dict[str, Any]:
    import tkinter as tk

    root = tk.Tk()
    root.title(title)
    root.attributes("-topmost", True)
    root.resizable(False, False)
    result: dict[str, Any] = {"action": "dismissed", "value": ""}

    tk.Label(root, text=body, wraplength=300, font=("", 11), justify="center").pack(
        pady=(16, 8), padx=16
    )
    btn_frame = tk.Frame(root)
    btn_frame.pack(pady=(8, 14))

    def on_confirm() -> None:
        result["action"] = "confirmed"
        root.destroy()

    def on_dismiss() -> None:
        root.destroy()

    tk.Button(btn_frame, text="确认", command=on_confirm, width=10).pack(side="left", padx=6)
    tk.Button(btn_frame, text="取消", command=on_dismiss, width=10).pack(side="left", padx=6)
    root.mainloop()
    return result


def _render_info(*, body: str, title: str) -> dict[str, Any]:
    import tkinter as tk

    root = tk.Tk()
    root.title(title)
    root.attributes("-topmost", True)
    root.resizable(False, False)
    tk.Label(root, text=body, wraplength=300, font=("", 11), justify="center").pack(
        pady=(16, 8), padx=16
    )
    tk.Button(root, text="关闭", command=root.destroy, width=10).pack(pady=(0, 14))
    root.mainloop()
    return {"action": "dismissed", "value": ""}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_operator_popup.py -v
```

Expected: 8 passed.

- [ ] **Step 5: Run full suite**

```bash
python -m pytest tests -q
```

Expected: 191 passed (3 more than 188: removed 5, added 8).

- [ ] **Step 6: Commit**

```bash
git add scripts/operator_popup.py tests/test_operator_popup.py
git commit -m "feat: operator_popup show_notify accepts ui_spec dict"
```

---

## Task 2: Update `notify_dispatcher.py` — drop `mcp_push_fn`, use `ui_spec`

**Files:**
- Modify: `scripts/notify_dispatcher.py`
- Rewrite: `tests/test_notify_dispatcher.py`

`NotificationDispatcher` no longer co-fires MCP (daemon owns that). It only routes the OS dialog call.

- [ ] **Step 1: Write failing tests (replace entire test file)**

Replace `tests/test_notify_dispatcher.py` with:

```python
from __future__ import annotations
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_CHOICE_SPEC = {"type": "choice", "body": "接管？", "options": ["好", "不用"]}
_INPUT_SPEC = {"type": "input", "body": "你在做什么？", "prefill": "草稿"}


def test_local_notifier_calls_show_notify(monkeypatch):
    import scripts.operator_popup as popup_mod
    from scripts.notify_dispatcher import LocalNotifier

    monkeypatch.setattr(popup_mod, "show_notify",
        lambda spec: {"action": "selected", "value": spec["options"][0]})
    result = LocalNotifier().notify(_CHOICE_SPEC)
    assert result == {"action": "selected", "value": "好"}


def test_local_notifier_returns_skip_on_error(monkeypatch):
    import scripts.operator_popup as popup_mod
    from scripts.notify_dispatcher import LocalNotifier

    def raise_err(spec):
        raise RuntimeError("no display")

    monkeypatch.setattr(popup_mod, "show_notify", raise_err)
    result = LocalNotifier().notify(_CHOICE_SPEC)
    assert result["action"] == "skip"
    assert "error" in result


def test_remote_notifier_calls_runner_client():
    from scripts.notify_dispatcher import RemoteNotifier

    class FakeClient:
        def notify(self, ui_spec):
            return {"action": "selected", "value": ui_spec["options"][0]}

    result = RemoteNotifier(client=FakeClient()).notify(_CHOICE_SPEC)
    assert result == {"action": "selected", "value": "好"}


def test_remote_notifier_returns_skip_on_error():
    from scripts.notify_dispatcher import RemoteNotifier

    class FailClient:
        def notify(self, ui_spec):
            raise RuntimeError("connection refused")

    result = RemoteNotifier(client=FailClient()).notify(_CHOICE_SPEC)
    assert result["action"] == "skip"
    assert "error" in result


def test_dispatcher_uses_remote_when_runner_available(monkeypatch):
    from scripts.notify_dispatcher import NotificationDispatcher

    remote_calls = []

    class FakeRouter:
        def find_client(self, args):
            class C:
                def notify(self, ui_spec):
                    remote_calls.append(ui_spec["type"])
                    return {"action": "selected", "value": "好"}
            return C()

    dispatcher = NotificationDispatcher(runner_router=FakeRouter())
    result = dispatcher.dispatch(_CHOICE_SPEC)
    assert result == {"action": "selected", "value": "好"}
    assert remote_calls == ["choice"]


def test_dispatcher_falls_back_to_local_when_no_runner(monkeypatch):
    import scripts.operator_popup as popup_mod
    from scripts.notify_dispatcher import NotificationDispatcher

    monkeypatch.setattr(popup_mod, "show_notify",
        lambda spec: {"action": "confirmed", "value": "local"})

    result = NotificationDispatcher(runner_router=None).dispatch(_INPUT_SPEC)
    assert result == {"action": "confirmed", "value": "local"}


def test_dispatcher_machine_ids_selects_runner_profile(monkeypatch):
    from scripts.notify_dispatcher import NotificationDispatcher

    selected_profiles = []

    class FakeRouter:
        def find_client(self, args):
            selected_profiles.append(args.get("target_profile"))
            class C:
                def notify(self, ui_spec): return {"action": "selected", "value": ""}
            return C()

    NotificationDispatcher(runner_router=FakeRouter()).dispatch(
        _CHOICE_SPEC, machine_ids=["mycader-1"]
    )
    assert selected_profiles == ["mycader-1"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_notify_dispatcher.py -v
```

Expected: `FAIL` — `TypeError` on old `notify(stage=..., message=...)` signature.

- [ ] **Step 3: Rewrite `scripts/notify_dispatcher.py`**

```python
from __future__ import annotations

from typing import Any


class LocalNotifier:
    """Shows operator_popup dialog directly in the current process."""

    def notify(self, ui_spec: dict) -> dict[str, Any]:
        try:
            from scripts.operator_popup import show_notify
            return show_notify(ui_spec)
        except Exception as exc:
            return {"action": "skip", "value": "", "error": str(exc)}


class RemoteNotifier:
    """Sends notification request to a runner via POST /notify."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def notify(self, ui_spec: dict) -> dict[str, Any]:
        try:
            return self._client.notify(ui_spec)
        except Exception as exc:
            return {"action": "skip", "value": "", "error": str(exc)}


class NotificationDispatcher:
    """Routes OS-native dialog to remote runner or local fallback.

    MCP push is the daemon's responsibility; this class handles only the
    OS dialog routing. CC calls this via icc_exec when it decides to engage
    the operator.
    """

    def __init__(self, runner_router: Any | None = None) -> None:
        self._router = runner_router

    def dispatch(
        self,
        ui_spec: dict,
        machine_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Route ui_spec dialog to remote runner or local Tkinter.

        Returns operator response: {action, value}.
        """
        if self._router is not None:
            profile = (machine_ids or [None])[0] or "default"
            client = self._router.find_client({"target_profile": profile})
            if client is not None:
                return RemoteNotifier(client).notify(ui_spec)
        return LocalNotifier().notify(ui_spec)
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

Expected: 191 passed.

- [ ] **Step 6: Commit**

```bash
git add scripts/notify_dispatcher.py tests/test_notify_dispatcher.py
git commit -m "refactor: NotificationDispatcher takes ui_spec, drops mcp_push_fn"
```

---

## Task 3: Update `RunnerClient.notify` — `ui_spec` POST body

**Files:**
- Modify: `scripts/runner_client.py` (the `notify` method, lines 99–142)
- Modify: `tests/test_mcp_tools_integration.py` (replace `test_runner_client_notify_posts_to_notify_endpoint`)

- [ ] **Step 1: Write failing test**

In `tests/test_mcp_tools_integration.py`, replace `test_runner_client_notify_posts_to_notify_endpoint` with:

```python
def test_runner_client_notify_posts_ui_spec(tmp_path):
    """RunnerClient.notify(ui_spec) POSTs {"ui_spec": {...}} and returns result dict."""
    import json as _json, threading, socket
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from scripts.runner_client import RunnerClient

    received = []

    class FakeHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = _json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append(body)
            resp = _json.dumps({"ok": True, "result": {"action": "selected", "value": "好"}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
        def log_message(self, *a): pass

    sock = socket.socket(); sock.bind(("127.0.0.1", 0))
    host, port = sock.getsockname(); sock.close()
    server = HTTPServer((host, port), FakeHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    try:
        client = RunnerClient(base_url=f"http://{host}:{port}", timeout_s=5)
        spec = {"type": "choice", "body": "接管？", "options": ["好", "不用"]}
        result = client.notify(spec)
        assert result == {"action": "selected", "value": "好"}
        assert len(received) == 1
        assert received[0] == {"ui_spec": spec}
    finally:
        server.shutdown()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_runner_client_notify_posts_ui_spec -v
```

Expected: `FAIL` — `TypeError: notify() takes 1 positional argument` (old signature has keyword args).

- [ ] **Step 3: Replace `notify` method in `scripts/runner_client.py`**

Replace the `notify` method (lines 99–142):

```python
def notify(self, ui_spec: dict) -> dict[str, Any]:
    """Send a notification request to the runner's /notify endpoint.

    Blocks until the operator responds. POST body: {"ui_spec": {...}}.
    Returns {action, value}. Raises RuntimeError on HTTP error.
    """
    payload = {"ui_spec": ui_spec}
    body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    timeout_s = int(ui_spec.get("timeout_s", 0))
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
python -m pytest tests/test_mcp_tools_integration.py::test_runner_client_notify_posts_ui_spec -v
```

Expected: `PASS`

- [ ] **Step 5: Run full suite**

```bash
python -m pytest tests -q
```

Expected: 191 passed.

- [ ] **Step 6: Commit**

```bash
git add scripts/runner_client.py tests/test_mcp_tools_integration.py
git commit -m "refactor: RunnerClient.notify takes ui_spec dict"
```

---

## Task 4: Update `remote_runner.py` — `POST /notify` body is `{"ui_spec": {...}}`

**Files:**
- Modify: `scripts/remote_runner.py` (`RunnerExecutor.show_notify`)
- Modify: `tests/test_remote_runner_events.py` (replace 2 notify tests)

- [ ] **Step 1: Write failing tests**

In `tests/test_remote_runner_events.py`, replace `test_runner_notify_endpoint_returns_action` and `test_runner_notify_endpoint_invalid_stage` with:

```python
def test_runner_notify_endpoint_returns_action(tmp_path, monkeypatch):
    """POST /notify with {ui_spec} calls show_notify and returns {ok, result}."""
    import scripts.operator_popup as popup_mod
    monkeypatch.setattr(popup_mod, "show_notify",
        lambda spec: {"action": "selected", "value": spec["options"][0]})

    with _RunnerServer(tmp_path / "state") as server:
        body = json.dumps({
            "ui_spec": {"type": "choice", "body": "接管？", "options": ["好", "不用"]}
        }).encode()
        req = urllib.request.Request(
            f"{server.url}/notify", data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
        assert data["ok"] is True
        assert data["result"] == {"action": "selected", "value": "好"}


def test_runner_notify_endpoint_unknown_type(tmp_path, monkeypatch):
    """POST /notify with unknown ui_spec type returns ok=True with action=skip."""
    import scripts.operator_popup as popup_mod
    monkeypatch.setattr(popup_mod, "show_notify",
        lambda spec: {"action": "skip", "value": ""})

    with _RunnerServer(tmp_path / "state") as server:
        body = json.dumps({"ui_spec": {"type": "badtype", "body": "x"}}).encode()
        req = urllib.request.Request(
            f"{server.url}/notify", data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
        assert data["ok"] is True
        assert data["result"]["action"] == "skip"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_remote_runner_events.py::test_runner_notify_endpoint_returns_action tests/test_remote_runner_events.py::test_runner_notify_endpoint_unknown_type -v
```

Expected: `FAIL` — old `show_notify` called with keyword args, `assert {"action": "selected"...} == {"action": "takeover"...}`.

- [ ] **Step 3: Update `RunnerExecutor.show_notify` in `scripts/remote_runner.py`**

Replace:

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

With:

```python
def show_notify(self, params: dict) -> dict:
    """Show OS-native notification dialog. Blocks until user responds.

    Expects params = {"ui_spec": {...}}. Passes ui_spec to show_notify.
    """
    from scripts.operator_popup import show_notify
    ui_spec = params.get("ui_spec", {})
    if not isinstance(ui_spec, dict):
        ui_spec = {}
    return show_notify(ui_spec)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_remote_runner_events.py::test_runner_notify_endpoint_returns_action tests/test_remote_runner_events.py::test_runner_notify_endpoint_unknown_type -v
```

Expected: 2 passed.

- [ ] **Step 5: Run full suite**

```bash
python -m pytest tests -q
```

Expected: 191 passed.

- [ ] **Step 6: Commit**

```bash
git add scripts/remote_runner.py tests/test_remote_runner_events.py
git commit -m "refactor: POST /notify expects ui_spec body"
```

---

## Task 5: Simplify `emerge_daemon.py` — channel-only `_push_pattern`

**Files:**
- Modify: `scripts/emerge_daemon.py`
- Modify: `tests/test_mcp_tools_integration.py` (replace `test_push_pattern_fires_both_mcp_and_os_notify`)

Remove: `_notification_dispatcher` field, dispatcher init in `start_operator_monitor`, `_mcp_push_simple`, `_build_intent_draft`, `_build_elicit_params`. Simplify `_push_pattern` to send one `notifications/claude/channel` for all stages with `policy_stage` in meta.

- [ ] **Step 1: Write failing test**

In `tests/test_mcp_tools_integration.py`, replace `test_push_pattern_fires_both_mcp_and_os_notify` with:

```python
def test_push_pattern_sends_channel_notification_for_all_stages(monkeypatch, tmp_path):
    """_push_pattern sends a single channel notification carrying policy_stage in meta."""
    from scripts.pattern_detector import PatternSummary
    from scripts.emerge_daemon import EmergeDaemon

    daemon = EmergeDaemon(root=ROOT)
    mcp_calls = []
    monkeypatch.setattr(daemon, "_write_mcp_push", lambda payload: mcp_calls.append(payload))

    for stage in ("explore", "canary", "stable"):
        mcp_calls.clear()
        summary = PatternSummary(
            machine_ids=["local"],
            intent_signature="hypermesh.node_create",
            occurrences=5,
            window_minutes=10.0,
            detector_signals=["frequency"],
            context_hint={"app": "hypermesh", "samples": []},
            policy_stage=stage,
        )
        daemon._push_pattern(stage, {"app": "hypermesh"}, summary)

        assert len(mcp_calls) == 1, f"stage={stage}: expected 1 MCP call, got {len(mcp_calls)}"
        payload = mcp_calls[0]
        assert payload["method"] == "notifications/claude/channel", f"stage={stage}: wrong method"
        meta = payload["params"]["meta"]
        assert meta["policy_stage"] == stage
        assert meta["intent_signature"] == "hypermesh.node_create"
        assert "machine_ids" in meta
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_push_pattern_sends_channel_notification_for_all_stages -v
```

Expected: `FAIL` — `assert len(mcp_calls) == 1` fails for canary/stable (currently sends elicit request, not channel).

- [ ] **Step 3: Update `emerge_daemon.py`**

**3a.** Remove `self._notification_dispatcher` line from `__init__` (line ~86):

```python
# Remove this line:
self._notification_dispatcher: "NotificationDispatcher | None" = None
```

**3b.** In `start_operator_monitor`, remove the 7 lines that create `NotificationDispatcher` and change `push_fn`:

```python
# Remove these lines (keep everything else in start_operator_monitor):
from scripts.notify_dispatcher import NotificationDispatcher

self._notification_dispatcher = NotificationDispatcher(
    mcp_push_fn=self._mcp_push_simple,
    runner_router=self._runner_router,
)
```

And change `push_fn=self._push_pattern` (already correct from previous task).

**3c.** Replace `_push_pattern` with simplified version:

```python
def _push_pattern(self, stage: str, context: dict, summary: Any) -> None:
    """Push pattern detection result to CC via MCP channel notification.

    CC reads policy_stage from meta and decides whether to engage the operator
    (via icc_exec → show_notify) or crystallize directly. Daemon never pops up.
    """
    message = self._build_explore_message(context, summary)
    self._write_mcp_push({
        "jsonrpc": "2.0",
        "method": "notifications/claude/channel",
        "params": {
            "serverName": "emerge",
            "content": message,
            "meta": {
                "source": "operator_monitor",
                "intent_signature": summary.intent_signature,
                "policy_stage": stage,
                "occurrences": summary.occurrences,
                "window_minutes": summary.window_minutes,
                "machine_ids": summary.machine_ids,
            },
        },
    })
```

**3d.** Delete these three methods entirely:

```python
# Delete: _mcp_push_simple
# Delete: _build_intent_draft
# Delete: _build_elicit_params
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_push_pattern_sends_channel_notification_for_all_stages -v
```

Expected: `PASS`

- [ ] **Step 5: Run full suite**

```bash
python -m pytest tests -q
```

Expected: 191 passed.

- [ ] **Step 6: Commit**

```bash
git add scripts/emerge_daemon.py tests/test_mcp_tools_integration.py
git commit -m "refactor: _push_pattern sends channel-only notification; CC drives dialogue"
```

---

## Task 6: Add silence principle to `CLAUDE.md`

**Files:**
- Modify: `CLAUDE.md`

No tests needed — this is documentation enforced by CC behaviour.

- [ ] **Step 1: Add to `CLAUDE.md` under `## Key Invariants`**

Add this block after the last bullet in `## Key Invariants`:

```markdown
- **Silence principle (operator interruption):** Show a popup (`show_notify`) only when the operator's input genuinely changes the outcome — intent is unclear, or the action is irreversible and high-risk. Never show a popup for: execution started/in-progress/completed, read-only operations (`icc_read`, state queries), status updates, or errors CC can resolve autonomously. Default is silence; interrupt only when necessary.
```

- [ ] **Step 2: Verify `CLAUDE.md` renders correctly**

```bash
grep -A 4 "Silence principle" CLAUDE.md
```

Expected: the four lines appear under Key Invariants.

- [ ] **Step 3: Run full suite one final time**

```bash
python -m pytest tests -q
```

Expected: 191 passed.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: add silence principle to CLAUDE.md Key Invariants"
```

---

## Self-Review

**Spec coverage:**
- §1 `ui_spec` API (`type`, `body`, `options`, `prefill`, `timeout_s`, return values) → Task 1 ✅
- §2 Daemon `_push_pattern` channel-only, all stages → Task 5 ✅
- §3 CC dialogue pattern (documented in spec; daemon side wired; CC behaviour via silence principle) → Tasks 5 + 6 ✅
- §4 Silence principle in `CLAUDE.md` → Task 6 ✅
- §5 File changes: all 10 files listed → Tasks 1–6 cover all ✅

**Placeholder scan:** None found. All steps have full code.

**Type consistency:**
- `show_notify(ui_spec: dict)` — used in Tasks 1, 2, 3, 4 ✅
- `notify(ui_spec: dict)` on `LocalNotifier`, `RemoteNotifier`, `RunnerClient` — consistent ✅
- `dispatch(ui_spec, machine_ids)` on `NotificationDispatcher` — consistent with Task 2 ✅
- POST body `{"ui_spec": {...}}` — Task 3 client sends it, Task 4 server reads it ✅
- `_push_pattern` return: void — unchanged ✅
- `PatternSummary` fields `machine_ids`, `occurrences`, `window_minutes` — used in Task 5, match `scripts/pattern_detector.py` definition ✅
