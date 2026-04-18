# RichInputWidget Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add file/image attachment support to emerge's tkinter input UI via a reusable `RichInputWidget` and a new `POST /runner/upload` daemon endpoint.

**Architecture:** `RichInputWidget` in `operator_popup.py` replaces both `_render_input` and `show_input_bubble`; files are uploaded to daemon via `POST /runner/upload` (multipart) and stored at `state_root/uploads/{uuid}/{filename}`; `watch_emerge.py` formats attachment paths into operator_message output so CC can `Read` them directly.

**Tech Stack:** Python 3.14, tkinter (stdlib), threading (stdlib), email (stdlib, for multipart parsing), mimetypes (stdlib), uuid (stdlib)

---

## File Map

| File | Change |
|---|---|
| `scripts/daemon_http.py` | Add `elif path == "/runner/upload":` handler in `do_POST`; add `_parse_multipart` helper |
| `scripts/operator_popup.py` | Add `Attachment` TypedDict, `_upload_file` helper, `RichInputWidget` class; update `_render_input` and `show_input_bubble` |
| `scripts/watch_emerge.py` | Update `operator_message` branch in `_format_event` to append attachment lines |
| `scripts/remote_runner.py` | Update `_post_operator_message(text, attachments)` signature; pass `upload_url` to widget |
| `tests/test_runner_upload.py` | New: HTTP-level tests for `/runner/upload` endpoint |
| `tests/test_watch_emerge.py` | Add: test `operator_message` with attachments formats correctly |
| `tests/test_operator_popup_upload.py` | New: unit tests for `_upload_file` helper (no tkinter needed) |

---

## Task 1: `POST /runner/upload` endpoint

**Files:**
- Modify: `scripts/daemon_http.py` (in `do_POST`, before final `else` at line ~668)
- Create: `tests/test_runner_upload.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_runner_upload.py`:

```python
from __future__ import annotations
import io, json, threading, time, urllib.request, uuid
from pathlib import Path
import pytest


def _make_server(tmp_path):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.daemon_http import DaemonHTTPServer

    class _StubDaemon:
        def handle_jsonrpc(self, req):
            return {"jsonrpc": "2.0", "id": req.get("id"), "result": {}}

    srv = DaemonHTTPServer(
        daemon=_StubDaemon(), port=0,
        pid_path=tmp_path / "d.pid",
        event_root=tmp_path / "operator-events",
        state_root=tmp_path / "repl",
    )
    srv.start()
    time.sleep(0.1)
    return srv


def _post_multipart(port, path, filename, file_bytes, mime="image/png", runner_profile=""):
    """POST multipart/form-data with a single 'file' field."""
    boundary = "boundary123"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode() + file_bytes + (
        f"\r\n--{boundary}\r\n"
        f'Content-Disposition: form-data; name="runner_profile"\r\n\r\n'
        f"{runner_profile}"
        f"\r\n--{boundary}--\r\n"
    ).encode()
    req = urllib.request.Request(
        f"http://localhost:{port}{path}",
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def test_upload_stores_file_and_returns_path(tmp_path):
    srv = _make_server(tmp_path)
    try:
        resp = _post_multipart(srv.port, "/runner/upload", "error.png", b"PNGDATA")
        assert "file_id" in resp
        assert "path" in resp
        assert "mime" in resp
        assert Path(resp["path"]).exists()
        assert Path(resp["path"]).read_bytes() == b"PNGDATA"
        assert resp["mime"] == "image/png"
    finally:
        srv.stop()


def test_upload_sanitizes_filename(tmp_path):
    srv = _make_server(tmp_path)
    try:
        resp = _post_multipart(srv.port, "/runner/upload", "../../etc/passwd", b"DATA")
        stored = Path(resp["path"])
        # path traversal stripped — stored filename must be just "passwd" or similar
        assert stored.parent.parent == tmp_path / "repl" / "uploads" / resp["file_id"]
        assert ".." not in resp["path"]
    finally:
        srv.stop()


def test_upload_missing_file_returns_400(tmp_path):
    srv = _make_server(tmp_path)
    try:
        boundary = "boundary123"
        body = f"--{boundary}\r\nContent-Disposition: form-data; name=\"other\"\r\n\r\nvalue\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            f"http://localhost:{srv.port}/runner/upload",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            assert False, "expected HTTP error"
        except urllib.error.HTTPError as e:
            assert e.code == 400
    finally:
        srv.stop()


def test_upload_rejects_oversized_file(tmp_path, monkeypatch):
    monkeypatch.setenv("EMERGE_UPLOAD_MAX_BYTES", "10")
    srv = _make_server(tmp_path)
    try:
        req_data = _build_multipart_body("big.bin", b"X" * 11)
        req = urllib.request.Request(
            f"http://localhost:{srv.port}/runner/upload",
            data=req_data["body"],
            headers=req_data["headers"],
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            assert False, "expected HTTP error"
        except urllib.error.HTTPError as e:
            assert e.code == 413
    finally:
        srv.stop()


def _build_multipart_body(filename, file_bytes, mime="application/octet-stream"):
    boundary = "bnd"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode() + file_bytes + f"\r\n--{boundary}--\r\n".encode()
    return {
        "body": body,
        "headers": {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
    }
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_runner_upload.py -q
```

Expected: `FAILED` — `404` or `AttributeError` (endpoint doesn't exist yet).

- [ ] **Step 3: Add `_parse_multipart` helper to `daemon_http.py`**

Add this function near the top of the module (after imports, before class definitions):

```python
def _parse_multipart(content_type: str, body: bytes) -> dict:
    """Parse multipart/form-data body. Returns {field_name: (data, filename, mime)}."""
    import email as _email
    import email.policy as _ep
    raw = f"MIME-Version: 1.0\r\nContent-Type: {content_type}\r\n\r\n".encode() + body
    msg = _email.message_from_bytes(raw, policy=_ep.compat32)
    parts = {}
    payload = msg.get_payload()
    if not isinstance(payload, list):
        return parts
    for part in payload:
        if not hasattr(part, "get_param"):
            continue
        name = part.get_param("name", header="content-disposition")
        filename = part.get_param("filename", header="content-disposition")
        data = part.get_payload(decode=True) or b""
        if name:
            parts[name] = (data, filename, part.get_content_type())
    return parts
```

- [ ] **Step 4: Add `/runner/upload` handler in `do_POST`**

In `daemon_http.py`, inside `do_POST`, add before the final `else` (before line `self._send_json(404, ...)`):

```python
            elif path == "/runner/upload":
                import mimetypes as _mt
                import uuid as _uuid
                content_type = self.headers.get("Content-Type", "")
                if "multipart/form-data" not in content_type:
                    self._send_json(400, {"error": "multipart/form-data required"})
                    return
                parts = _parse_multipart(content_type, body)
                if "file" not in parts:
                    self._send_json(400, {"error": "no file provided"})
                    return
                file_data, filename, mime = parts["file"]
                max_bytes = int(os.environ.get("EMERGE_UPLOAD_MAX_BYTES", str(50 * 1024 * 1024)))
                if len(file_data) > max_bytes:
                    self.send_response(413)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": "file too large"}).encode())
                    return
                safe_name = Path(filename or "upload").name or "upload"
                file_id = str(_uuid.uuid4())
                upload_dir = srv._state_root / "uploads" / file_id
                upload_dir.mkdir(parents=True, exist_ok=True)
                dest = upload_dir / safe_name
                dest.write_bytes(file_data)
                if not mime or mime == "application/octet-stream":
                    guessed, _ = _mt.guess_type(safe_name)
                    mime = guessed or "application/octet-stream"
                self._send_json(200, {"file_id": file_id, "path": str(dest), "mime": mime})
```

Note: `os` is already imported in `daemon_http.py`. Verify with `grep "^import os" scripts/daemon_http.py`; if missing add it.

- [ ] **Step 5: Run tests to verify they pass**

```bash
python -m pytest tests/test_runner_upload.py -q
```

Expected: all 4 tests `PASSED`.

- [ ] **Step 6: Commit**

```bash
git add scripts/daemon_http.py tests/test_runner_upload.py
git commit -m "feat(daemon): add POST /runner/upload endpoint"
```

---

## Task 2: `_upload_file` helper + `RichInputWidget` class

**Files:**
- Modify: `scripts/operator_popup.py`
- Create: `tests/test_operator_popup_upload.py`

- [ ] **Step 1: Write failing tests for `_upload_file`**

Create `tests/test_operator_popup_upload.py`:

```python
from __future__ import annotations
import json, threading, time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import pytest
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _make_mock_upload_server(tmp_path, *, fail=False, too_large=False):
    """Minimal HTTP server that mimics /runner/upload response."""
    results = []

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            if too_large:
                self.send_response(413)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "file too large"}).encode())
                return
            if fail:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "no file"}).encode())
                return
            fake_path = str(tmp_path / "uploads" / "abc" / "test.png")
            resp = {"file_id": "abc", "path": fake_path, "mime": "image/png"}
            body = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            results.append(resp)

    srv = HTTPServer(("localhost", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, results


def test_upload_file_success(tmp_path):
    from scripts.operator_popup import _upload_file
    src = tmp_path / "test.png"
    src.write_bytes(b"PNGDATA")

    mock_srv, results = _make_mock_upload_server(tmp_path)
    url = f"http://localhost:{mock_srv.server_address[1]}/runner/upload"
    att = _upload_file(url, src)
    mock_srv.shutdown()

    assert att["name"] == "test.png"
    assert att["mime"] == "image/png"
    assert "path" in att


def test_upload_file_http_error_raises(tmp_path):
    from scripts.operator_popup import _upload_file
    src = tmp_path / "test.png"
    src.write_bytes(b"DATA")

    mock_srv, _ = _make_mock_upload_server(tmp_path, fail=True)
    url = f"http://localhost:{mock_srv.server_address[1]}/runner/upload"
    with pytest.raises(RuntimeError, match="upload failed"):
        _upload_file(url, src)
    mock_srv.shutdown()


def test_upload_file_413_raises(tmp_path):
    from scripts.operator_popup import _upload_file
    src = tmp_path / "big.bin"
    src.write_bytes(b"X" * 100)

    mock_srv, _ = _make_mock_upload_server(tmp_path, too_large=True)
    url = f"http://localhost:{mock_srv.server_address[1]}/runner/upload"
    with pytest.raises(RuntimeError, match="file too large"):
        _upload_file(url, src)
    mock_srv.shutdown()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_operator_popup_upload.py -q
```

Expected: `FAILED` — `ImportError: cannot import name '_upload_file'`.

- [ ] **Step 3: Add `Attachment` TypedDict and `_upload_file` to `operator_popup.py`**

Add after the existing imports at the top of `scripts/operator_popup.py`:

```python
from __future__ import annotations

import json as _json
import mimetypes as _mt
import urllib.error as _ue
import urllib.request as _ur
from pathlib import Path
from typing import Any, Callable, TypedDict


class Attachment(TypedDict):
    path: str
    mime: str
    name: str


def _upload_file(upload_url: str, filepath: Path) -> Attachment:
    """Upload a file to daemon via multipart POST. Returns Attachment on success, raises RuntimeError on failure."""
    mime, _ = _mt.guess_type(filepath.name)
    mime = mime or "application/octet-stream"
    boundary = "emergeboundary"
    file_bytes = filepath.read_bytes()
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filepath.name}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode() + file_bytes + f"\r\n--{boundary}--\r\n".encode()
    req = _ur.Request(
        upload_url,
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
    )
    try:
        with _ur.urlopen(req, timeout=30) as r:
            resp = _json.loads(r.read())
        return Attachment(path=resp["path"], mime=resp.get("mime", mime), name=filepath.name)
    except _ue.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        try:
            msg = _json.loads(body_text).get("error", body_text)
        except Exception:
            msg = body_text
        if exc.code == 413:
            raise RuntimeError(f"file too large: {filepath.name}") from exc
        raise RuntimeError(f"upload failed ({exc.code}): {msg}") from exc
    except OSError as exc:
        raise RuntimeError(f"upload failed: {exc}") from exc
```

- [ ] **Step 4: Run tests to verify `_upload_file` tests pass**

```bash
python -m pytest tests/test_operator_popup_upload.py -q
```

Expected: all 3 tests `PASSED`.

- [ ] **Step 5: Add `RichInputWidget` class to `operator_popup.py`**

Add after `_upload_file`:

```python
class RichInputWidget:
    """Claude-Code-style input widget with text area, attachment chips, and file upload.

    Layout:
        ┌────────────────────────────────────┐
        │ multi-line Text area (4 rows)      │
        ├────────────────────────────────────┤
        │ [📎 file.py ×] [🖼 img.png ×]     │  chips row
        ├────────────────────────────────────┤
        │ [📁 文件] [🖼 图片]    [发送 ↵]   │  toolbar
        └────────────────────────────────────┘

    Args:
        parent:      tk.Tk root window (caller creates it)
        on_submit:   called with (text: str, attachments: list[Attachment]) on send
        upload_url:  http://<daemon>/runner/upload
        title:       window title
    """

    def __init__(
        self,
        parent: Any,
        on_submit: Callable[[str, list[Attachment]], None],
        upload_url: str,
        title: str = "emerge",
    ) -> None:
        import tkinter as tk
        self._root = parent
        self._on_submit = on_submit
        self._upload_url = upload_url
        self._attachments: list[Attachment] = []
        self._pending: int = 0  # count of in-flight uploads

        parent.title(title)
        parent.attributes("-topmost", True)
        parent.resizable(True, False)
        parent.update_idletasks()
        sw = parent.winfo_screenwidth()
        sh = parent.winfo_screenheight()
        w, h = 480, 160
        parent.geometry(f"{w}x{h}+{int(sw / 2 - w / 2)}+{int(sh / 2 - h / 2)}")

        # Text area
        self._text = tk.Text(parent, height=4, width=56, relief="solid", bd=1, wrap="word")
        self._text.pack(padx=10, pady=(10, 4), fill="x")
        self._text.focus_set()

        # Chips frame (attachment list)
        self._chips_frame = tk.Frame(parent)
        self._chips_frame.pack(padx=10, fill="x")

        # Toolbar
        toolbar = tk.Frame(parent)
        toolbar.pack(padx=10, pady=(4, 10), fill="x")

        tk.Button(toolbar, text="📁 文件", command=self._pick_file, width=8).pack(side="left", padx=(0, 4))
        tk.Button(toolbar, text="🖼 图片", command=self._pick_image, width=8).pack(side="left")

        self._send_btn = tk.Button(toolbar, text="发送 ↵", command=self._on_send, width=8)
        self._send_btn.pack(side="right")

        # Keyboard shortcuts
        parent.bind("<Control-Return>", lambda _e: self._on_send())
        parent.bind("<Command-Return>", lambda _e: self._on_send())

        # Drag-and-drop (macOS/Linux tkdnd optional — graceful degradation)
        try:
            parent.drop_target_register("DND_Files")  # type: ignore[attr-defined]
            parent.dnd_bind("<<Drop>>", self._on_drop)  # type: ignore[attr-defined]
        except Exception:
            pass

    # ── file pickers ────────────────────────────────────────────────────────

    def _pick_file(self) -> None:
        import tkinter.filedialog as _fd
        paths = _fd.askopenfilenames(parent=self._root, title="选择文件")
        for p in paths:
            self._add_attachment(Path(p))

    def _pick_image(self) -> None:
        import tkinter.filedialog as _fd
        paths = _fd.askopenfilenames(
            parent=self._root, title="选择图片",
            filetypes=[("图片", "*.png *.jpg *.jpeg *.gif *.bmp *.webp"), ("所有文件", "*")],
        )
        for p in paths:
            self._add_attachment(Path(p))

    def _on_drop(self, event: Any) -> None:
        raw = event.data if hasattr(event, "data") else str(event)
        for token in raw.split():
            p = Path(token.strip("{}"))
            if p.exists():
                self._add_attachment(p)

    # ── attachment management ────────────────────────────────────────────────

    def _add_attachment(self, filepath: Path) -> None:
        import tkinter as tk
        import threading

        chip_var = tk.StringVar(value=f"⏳ {filepath.name}")
        chip_frame = tk.Frame(self._chips_frame, relief="solid", bd=1)
        chip_frame.pack(side="left", padx=(0, 4), pady=2)
        chip_label = tk.Label(chip_frame, textvariable=chip_var, font=("", 9))
        chip_label.pack(side="left", padx=(4, 0))
        remove_btn = tk.Button(chip_frame, text="×", font=("", 9), relief="flat",
                               command=lambda: self._remove_chip(chip_frame, filepath))
        remove_btn.pack(side="left", padx=2)

        self._pending += 1
        self._send_btn.config(state="disabled")

        def _do_upload() -> None:
            try:
                att = _upload_file(self._upload_url, filepath)
                self._attachments.append(att)
                self._root.after(0, lambda: chip_var.set(f"📎 {filepath.name}"))
            except RuntimeError as exc:
                self._root.after(0, lambda: chip_var.set(f"❌ {filepath.name}"))
                self._root.after(0, lambda: chip_label.config(fg="red"))
                # store error so send is blocked — chip stays in list but has no path
            finally:
                self._pending -= 1
                if self._pending == 0:
                    self._root.after(0, lambda: self._send_btn.config(state="normal"))

        threading.Thread(target=_do_upload, daemon=True).start()

    def _remove_chip(self, chip_frame: Any, filepath: Path) -> None:
        self._attachments = [a for a in self._attachments if a["name"] != filepath.name]
        chip_frame.destroy()

    # ── send ─────────────────────────────────────────────────────────────────

    def _on_send(self) -> None:
        if self._pending > 0:
            return  # still uploading
        text = self._text.get("1.0", "end-1c").strip()
        self._root.destroy()
        if text or self._attachments:
            self._on_submit(text, list(self._attachments))
```

- [ ] **Step 6: Commit**

```bash
git add scripts/operator_popup.py tests/test_operator_popup_upload.py
git commit -m "feat(popup): add _upload_file helper and RichInputWidget"
```

---

## Task 3: Wire `_render_input` and `show_input_bubble` to `RichInputWidget`

**Files:**
- Modify: `scripts/operator_popup.py`

- [ ] **Step 1: Update `_render_input`**

Replace the entire `_render_input` function (lines ~95–126) with:

```python
def _render_input(*, body: str, prefill: str, title: str, upload_url: str = "") -> dict[str, Any]:
    import tkinter as tk
    root = tk.Tk()
    result: dict[str, Any] = {"action": "dismissed", "value": "", "attachments": []}

    def _on_submit(text: str, attachments: list[Attachment]) -> None:
        result["action"] = "confirmed"
        result["value"] = text
        result["attachments"] = attachments

    widget = RichInputWidget(root, on_submit=_on_submit, upload_url=upload_url, title=title)
    if prefill:
        widget._text.insert("1.0", prefill)
    root.mainloop()
    return result
```

Also update `show_notify` caller of `_render_input` (line ~35) to pass `upload_url` from `ui_spec`:

```python
        if ui_type == "input":
            prefill = str(ui_spec.get("prefill", ""))
            upload_url = str(ui_spec.get("upload_url", ""))
            return _render_input(body=body, prefill=prefill, title=title, upload_url=upload_url)
```

- [ ] **Step 2: Update `show_input_bubble`**

Replace the entire `show_input_bubble` function with:

```python
def show_input_bubble(on_submit: Callable[[str, list[Attachment]], None], upload_url: str = "") -> None:
    """Open RichInputWidget bubble. Calls on_submit(text, attachments) on send."""
    import tkinter as tk
    root = tk.Tk()
    root.update_idletasks()
    RichInputWidget(root, on_submit=on_submit, upload_url=upload_url, title="emerge")
    root.mainloop()
```

- [ ] **Step 3: Run existing tests to confirm no regression**

```bash
python -m pytest tests/test_operator_popup_upload.py tests/test_runner_upload.py -q
```

Expected: all tests `PASSED`.

- [ ] **Step 4: Commit**

```bash
git add scripts/operator_popup.py
git commit -m "feat(popup): replace _render_input and show_input_bubble with RichInputWidget"
```

---

## Task 4: `watch_emerge.py` attachment formatting

**Files:**
- Modify: `scripts/watch_emerge.py`
- Modify: `tests/test_watch_emerge.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_watch_emerge.py`:

```python
def test_watch_emerge_operator_message_with_attachments(tmp_path):
    events_root = tmp_path / "events"
    events_root.mkdir(parents=True, exist_ok=True)
    events_file = events_root / "events-mycader-1.jsonl"

    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "watch_emerge.py"),
         "--runner-profile", "mycader-1",
         "--state-root", str(tmp_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    time.sleep(0.3)

    _write_event(events_file, {
        "type": "operator_message",
        "ts_ms": 1000,
        "runner_profile": "mycader-1",
        "text": "请看这个报错",
        "attachments": [
            {"path": "/state/uploads/abc/error.png", "mime": "image/png", "name": "error.png"},
        ],
    })
    time.sleep(0.6)
    proc.terminate()
    out = proc.stdout.read().decode()
    assert "请看这个报错" in out
    assert "/state/uploads/abc/error.png" in out
    assert "image/png" in out
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_watch_emerge.py::test_watch_emerge_operator_message_with_attachments -q
```

Expected: `FAILED` — attachment path not in output.

- [ ] **Step 3: Update `_format_event` in `watch_emerge.py`**

Replace lines 83–86:

```python
    if etype == "operator_message":
        text = event.get("text", "")
        profile = event.get("runner_profile", event.get("profile", "?"))
        return f"[ACTION REQUIRED][Operator:{profile}] {text}"
```

with:

```python
    if etype == "operator_message":
        text = event.get("text", "")
        profile = event.get("runner_profile", event.get("profile", "?"))
        lines = [f"[ACTION REQUIRED][Operator:{profile}] {text}"]
        for att in event.get("attachments", []):
            lines.append(f"[附件: {att.get('path', '')} ({att.get('mime', '')})]")
        return "\n".join(lines)
```

- [ ] **Step 4: Run all watch_emerge tests**

```bash
python -m pytest tests/test_watch_emerge.py -q
```

Expected: all tests `PASSED`.

- [ ] **Step 5: Commit**

```bash
git add scripts/watch_emerge.py tests/test_watch_emerge.py
git commit -m "feat(watch): format operator_message attachments in output"
```

---

## Task 5: Wire up `remote_runner.py`

**Files:**
- Modify: `scripts/remote_runner.py`

- [ ] **Step 1: Update `_post_operator_message` signature**

Replace the existing `_post_operator_message` method (lines ~137–165):

```python
    def _post_operator_message(self, text: str, attachments: list | None = None) -> None:
        """Forward operator tray message to daemon as an operator_message event."""
        import socket as _sock
        import time as _time
        try:
            machine_id = _sock.gethostname()
        except OSError:
            machine_id = "unknown"
        event = {
            "type": "operator_message",
            "text": text,
            "attachments": attachments or [],
            "runner_profile": self._runner_profile,
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
            except (ImportError, OSError):
                pass
```

- [ ] **Step 2: Update `_on_send_message` to pass `upload_url`**

Replace lines ~189–195 (the `_on_send_message` closure and `show_input_bubble` call):

```python
        def _on_send_message(icon: Any, item: Any) -> None:
            from scripts.operator_popup import show_input_bubble
            upload_url = f"{self._team_lead_url}/runner/upload" if self._team_lead_url else ""
            threading.Thread(
                target=show_input_bubble,
                args=(self._post_operator_message, upload_url),
                daemon=True,
            ).start()
```

- [ ] **Step 3: Run full test suite to confirm no regression**

```bash
python -m pytest tests -q
```

Expected: all tests pass. Any new failures indicate a regression — fix before committing.

- [ ] **Step 4: Commit**

```bash
git add scripts/remote_runner.py
git commit -m "feat(runner): pass upload_url to RichInputWidget and forward attachments in operator_message"
```

---

## Self-Review

**Spec coverage:**
- ✅ `RichInputWidget` — Task 2 + 3
- ✅ `POST /runner/upload` — Task 1
- ✅ `watch_emerge.py` attachment formatting — Task 4
- ✅ `remote_runner.py` wiring — Task 5
- ✅ local/remote uniform path — `upload_url` always derived from `_team_lead_url`, no special local branch
- ✅ filename sanitize (`Path(...).name`) — Task 1 Step 4
- ✅ `EMERGE_UPLOAD_MAX_BYTES` — Task 1 Step 4
- ✅ async upload / spinner / disable send button — Task 2 Step 5
- ✅ Ctrl+Enter / ⌘+Enter — Task 2 Step 5

**Placeholders:** None.

**Type consistency:**
- `Attachment` TypedDict defined once in Task 2 Step 3; used in `_upload_file` return, `RichInputWidget.on_submit`, `_render_input` result, `show_input_bubble` callback, `_post_operator_message` parameter — all consistent.
- `_render_input` gains `upload_url` param; `show_notify` passes it from `ui_spec.get("upload_url", "")` — callers that don't supply it get no-upload behavior (chip uploads will fail silently with OSError → `❌` chip).
