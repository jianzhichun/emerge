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


def _upload_file(upload_url: str, filepath: Path) -> "Attachment":
    """Upload file to daemon via multipart POST. Returns Attachment or raises RuntimeError."""
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


class RichInputWidget:
    """Claude-Code-style input widget: text area + attachment chips + upload.

    Layout:
        ┌────────────────────────────────────┐
        │ multi-line Text area (4 rows)      │
        ├────────────────────────────────────┤
        │ [📎 file.py ×] [🖼 img.png ×]     │
        ├────────────────────────────────────┤
        │ [📁 文件] [🖼 图片]    [发送 ↵]   │
        └────────────────────────────────────┘
    """

    def __init__(
        self,
        parent: Any,
        on_submit: "Callable[[str, list[Attachment]], None]",
        upload_url: str,
        title: str = "emerge",
    ) -> None:
        import tkinter as tk
        self._root = parent
        self._on_submit = on_submit
        self._upload_url = upload_url
        self._attachments: list[Attachment] = []
        self._pending: int = 0

        parent.title(title)
        parent.attributes("-topmost", True)
        parent.resizable(True, False)
        parent.update_idletasks()
        sw = parent.winfo_screenwidth()
        sh = parent.winfo_screenheight()
        w, h = 480, 160
        parent.geometry(f"{w}x{h}+{int(sw / 2 - w / 2)}+{int(sh / 2 - h / 2)}")

        self._text = tk.Text(parent, height=4, width=56, relief="solid", bd=1, wrap="word")
        self._text.pack(padx=10, pady=(10, 4), fill="x")
        self._text.focus_set()

        self._chips_frame = tk.Frame(parent)
        self._chips_frame.pack(padx=10, fill="x")

        toolbar = tk.Frame(parent)
        toolbar.pack(padx=10, pady=(4, 10), fill="x")
        tk.Button(toolbar, text="📁 文件", command=self._pick_file, width=8).pack(side="left", padx=(0, 4))
        tk.Button(toolbar, text="🖼 图片", command=self._pick_image, width=8).pack(side="left")
        self._send_btn = tk.Button(toolbar, text="发送 ↵", command=self._on_send, width=8)
        self._send_btn.pack(side="right")

        parent.bind("<Control-Return>", lambda _e: self._on_send())
        parent.bind("<Command-Return>", lambda _e: self._on_send())

        try:
            parent.drop_target_register("DND_Files")  # type: ignore[attr-defined]
            parent.dnd_bind("<<Drop>>", self._on_drop)  # type: ignore[attr-defined]
        except Exception:
            pass

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

    def _add_attachment(self, filepath: Path) -> None:
        import tkinter as tk
        import threading

        chip_var = tk.StringVar(value=f"⏳ {filepath.name}")
        chip_frame = tk.Frame(self._chips_frame, relief="solid", bd=1)
        chip_frame.pack(side="left", padx=(0, 4), pady=2)
        chip_label = tk.Label(chip_frame, textvariable=chip_var, font=("", 9))
        chip_label.pack(side="left", padx=(4, 0))
        tk.Button(
            chip_frame, text="×", font=("", 9), relief="flat",
            command=lambda: self._remove_chip(chip_frame, filepath),
        ).pack(side="left", padx=2)

        self._pending += 1
        self._send_btn.config(state="disabled")

        def _do_upload() -> None:
            try:
                att = _upload_file(self._upload_url, filepath)
                self._attachments.append(att)
                self._root.after(0, lambda: chip_var.set(f"📎 {filepath.name}"))
            except RuntimeError:
                self._root.after(0, lambda: chip_var.set(f"❌ {filepath.name}"))
                self._root.after(0, lambda: chip_label.config(fg="red"))
            finally:
                self._pending -= 1
                if self._pending == 0:
                    self._root.after(0, lambda: self._send_btn.config(state="normal"))

        threading.Thread(target=_do_upload, daemon=True).start()

    def _remove_chip(self, chip_frame: Any, filepath: Path) -> None:
        self._attachments = [a for a in self._attachments if a["name"] != filepath.name]
        chip_frame.destroy()

    def _on_send(self) -> None:
        if self._pending > 0:
            return
        text = self._text.get("1.0", "end-1c").strip()
        self._root.destroy()
        if text or self._attachments:
            self._on_submit(text, list(self._attachments))


def show_notify(ui_spec: dict) -> dict[str, Any]:
    """Show OS-native blocking dialog driven by ui_spec.

    ui_spec fields:
      type     : "choice" | "input" | "confirm" | "info" | "toast"
      body     : str  — main message text
      title    : str  — window title (default "emerge"; ignored for toast)
      options  : list[str]  — button labels (required for type="choice")
      prefill  : str  — pre-filled text (type="input")
      timeout_s: int  — >0 auto-selects options[0] after countdown (default 0);
                        for toast: auto-dismiss delay in seconds (default 5)

    Returns:
      {"action": "selected"|"confirmed"|"dismissed"|"skip", "value": str}
    """
    ui_type = ui_spec.get("type", "")
    if ui_type not in ("choice", "input", "confirm", "info", "toast"):
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
        if ui_type == "toast":
            return _render_toast(body=body, timeout_s=max(1, timeout_s or 5))
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
        countdown_var = tk.StringVar(value=f"(Auto-selecting {options[0]} in {timeout_s}s)")
        tk.Label(root, textvariable=countdown_var, font=("", 9), fg="gray").pack()
        remaining = [timeout_s]

        def update_countdown() -> None:
            remaining[0] -= 1
            if remaining[0] <= 0:
                result["action"] = "selected"
                result["value"] = options[0]
                root.destroy()
                return
            countdown_var.set(f"(Auto-selecting {options[0]} in {remaining[0]}s)")
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

    tk.Button(btn_frame, text="Confirm", command=on_confirm, width=8).pack(side="left", padx=4)
    tk.Button(btn_frame, text="Skip", command=on_dismiss, width=8).pack(side="left", padx=4)
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

    tk.Button(btn_frame, text="Confirm", command=on_confirm, width=10).pack(side="left", padx=6)
    tk.Button(btn_frame, text="Cancel", command=on_dismiss, width=10).pack(side="left", padx=6)
    root.mainloop()
    return result


def _render_toast(*, body: str, timeout_s: int) -> dict[str, Any]:
    """Non-interactive toast that auto-dismisses after timeout_s seconds.

    Spawns a daemon thread for the tkinter window and returns immediately.
    """
    import threading

    def _show() -> None:  # intentionally closes over body/timeout_s — both are immutable call-locals
        import tkinter as tk
        try:
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
        except Exception:
            pass  # headless or display error — silently skip

    threading.Thread(target=_show, daemon=True).start()
    return {"action": "dismissed", "value": ""}


def _render_info(*, body: str, title: str) -> dict[str, Any]:
    import tkinter as tk

    root = tk.Tk()
    root.title(title)
    root.attributes("-topmost", True)
    root.resizable(False, False)
    tk.Label(root, text=body, wraplength=300, font=("", 11), justify="center").pack(
        pady=(16, 8), padx=16
    )
    tk.Button(root, text="Close", command=root.destroy, width=10).pack(pady=(0, 14))
    root.mainloop()
    return {"action": "dismissed", "value": ""}


def show_input_bubble(on_submit: Callable[[str], None]) -> None:
    """Open a minimal tkinter input bubble.

    Calls on_submit(text) with the stripped text when the operator clicks Send
    or presses Enter. Silently closes on Cancel or empty input.
    """
    import tkinter as tk
    root = tk.Tk()
    root.title("emerge")
    root.attributes("-topmost", True)
    root.resizable(False, False)
    root.update_idletasks()
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    w, h = 360, 120
    root.geometry(f"{w}x{h}+{int(sw / 2 - w / 2)}+{int(sh / 2 - h / 2)}")
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
