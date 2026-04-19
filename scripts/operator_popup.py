from __future__ import annotations

from typing import Any, Callable, TypedDict


class Attachment(TypedDict):
    path: str
    mime: str
    name: str


def _upload_file(upload_url: str, filepath: "Path") -> "Attachment":
    """Upload file to daemon via multipart POST. Returns Attachment or raises RuntimeError."""
    import json as _json
    import mimetypes as _mt
    import urllib.error as _ue
    import urllib.request as _ur
    from pathlib import Path
    import uuid as _uuid
    if not upload_url:
        raise RuntimeError("upload failed: no upload_url configured")
    mime, _ = _mt.guess_type(filepath.name)
    mime = mime or "application/octet-stream"
    # Random boundary prevents collision with binary file content.
    boundary = _uuid.uuid4().hex
    # Sanitize filename: strip control chars and escape double-quote so the
    # Content-Disposition header value stays well-formed.
    safe_name = (
        filepath.name.replace("\r", "").replace("\n", "").replace('"', '\\"')
    )
    file_bytes = filepath.read_bytes()
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
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
        parent.protocol("WM_DELETE_WINDOW", parent.destroy)

        try:
            parent.drop_target_register("DND_Files")  # type: ignore[attr-defined]
            parent.dnd_bind("<<Drop>>", self._on_drop)  # type: ignore[attr-defined]
        except Exception:
            pass

    def _pick_file(self) -> None:
        import tkinter.filedialog as _fd
        from pathlib import Path
        paths = _fd.askopenfilenames(parent=self._root, title="选择文件")
        for p in paths:
            self._add_attachment(Path(p))

    def _pick_image(self) -> None:
        import tkinter.filedialog as _fd
        from pathlib import Path
        paths = _fd.askopenfilenames(
            parent=self._root, title="选择图片",
            filetypes=[("图片", "*.png *.jpg *.jpeg *.gif *.bmp *.webp"), ("所有文件", "*")],
        )
        for p in paths:
            self._add_attachment(Path(p))

    def _on_drop(self, event: Any) -> None:
        import re
        from pathlib import Path
        raw = event.data if hasattr(event, "data") else str(event)
        # tkinterdnd2 format: paths with spaces are wrapped in {}, e.g.:
        #   /simple/path {/path with spaces/file.txt} /another/path
        paths = re.findall(r"\{([^}]+)\}|(\S+)", raw)
        for braced, plain in paths:
            p = Path(braced or plain)
            if p.exists():
                self._add_attachment(p)

    def _add_attachment(self, filepath: Path) -> None:
        import tkinter as tk
        import threading
        import uuid as _uuid_mod

        slot_id = _uuid_mod.uuid4().hex
        cancelled: set[str] = set()

        chip_var = tk.StringVar(value=f"⏳ {filepath.name}")
        chip_frame = tk.Frame(self._chips_frame, relief="solid", bd=1)
        chip_frame.pack(side="left", padx=(0, 4), pady=2)
        chip_label = tk.Label(chip_frame, textvariable=chip_var, font=("", 9))
        chip_label.pack(side="left", padx=(4, 0))

        def _remove_this_chip() -> None:
            cancelled.add(slot_id)
            self._attachments = [a for a in self._attachments if a.get("_slot") != slot_id]
            chip_frame.destroy()

        tk.Button(
            chip_frame, text="×", font=("", 9), relief="flat",
            command=_remove_this_chip,
        ).pack(side="left", padx=2)

        self._pending += 1
        self._send_btn.config(state="disabled")

        def _schedule(fn) -> None:
            """Dispatch fn onto tkinter's main thread; silently drop if window already closed."""
            try:
                self._root.after(0, fn)
            except Exception:
                pass  # TclError: window destroyed while upload was in flight

        def _do_upload() -> None:
            try:
                att = _upload_file(self._upload_url, filepath)
                def _on_success(a=att):
                    if slot_id not in cancelled:
                        self._attachments.append({**a, "_slot": slot_id})
                        chip_var.set(f"📎 {filepath.name}")
                    # if cancelled: chip_frame already destroyed, nothing to update
                _schedule(_on_success)
            except RuntimeError:
                _schedule(lambda: chip_var.set(f"❌ {filepath.name}"))
                _schedule(lambda: chip_label.config(fg="red"))
            finally:
                def _finish():
                    self._pending -= 1
                    if self._pending == 0:
                        self._send_btn.config(state="normal")
                _schedule(_finish)

        threading.Thread(target=_do_upload, daemon=True).start()

    def _on_send(self) -> None:
        if self._pending > 0:  # uploads in flight — send_btn is already disabled, but guard keyboard shortcut too
            return
        text = self._text.get("1.0", "end-1c").strip()
        self._root.destroy()
        if text or self._attachments:  # allow attach-only messages (no text required)
            clean = [{k: v for k, v in a.items() if k != "_slot"} for a in self._attachments]
            self._on_submit(text, clean)


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
            upload_url = str(ui_spec.get("upload_url", ""))
            return _render_input(body=body, prefill=prefill, title=title, upload_url=upload_url)
        if ui_type == "confirm":
            return _render_confirm(body=body, title=title)
        if ui_type == "toast":
            return _render_toast(body=body, timeout_s=max(1, timeout_s or 5))
        # info
        return _render_info(body=body, title=title)
    except Exception as exc:
        return {"action": "skip", "value": "", "error": str(exc)}


def _set_window_icon(root: Any) -> None:
    """Set emerge icon on a tkinter Tk window. Silently no-ops on any error."""
    try:
        from pathlib import Path
        from PIL import Image, ImageTk  # type: ignore[import]
        _icon_png = Path(__file__).parent.parent / "assets" / "icon-64.png"
        img = Image.open(_icon_png)
        photo = ImageTk.PhotoImage(img)
        root.wm_iconphoto(True, photo)
        root._emerge_icon_ref = photo  # prevent GC
    except Exception:
        pass


def _render_choice(*, body: str, options: list[str], title: str, timeout_s: int) -> dict[str, Any]:
    import tkinter as tk

    root = tk.Tk()
    root.title(title)
    _set_window_icon(root)
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


def _render_input(*, body: str, prefill: str, title: str, upload_url: str = "") -> dict[str, Any]:
    import tkinter as tk
    root = tk.Tk()
    _set_window_icon(root)
    result: dict[str, Any] = {"action": "dismissed", "value": "", "attachments": []}

    tk.Label(root, text=body, wraplength=340, justify="left").pack(
        pady=(12, 4), padx=16, anchor="w"
    )

    def _on_submit(text: str, attachments: list) -> None:
        result["action"] = "confirmed"
        result["value"] = text
        result["attachments"] = attachments

    widget = RichInputWidget(root, on_submit=_on_submit, upload_url=upload_url, title=title)
    if prefill:
        widget._text.insert("1.0", prefill)
    root.mainloop()
    return result


def _render_confirm(*, body: str, title: str) -> dict[str, Any]:
    import tkinter as tk

    root = tk.Tk()
    root.title(title)
    _set_window_icon(root)
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

    Platform dispatch:
      macOS   → osascript display notification (no NSApp conflict with pystray)
      Windows → PowerShell balloon via System.Windows.Forms.NotifyIcon
      other   → subprocess-isolated tkinter window
    """
    import subprocess, sys

    if sys.platform == "darwin":
        # Escape for AppleScript string literal: collapse control chars
        # (newlines break the single-line -e string), then escape double-quotes.
        safe = (
            body.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
                .replace('"', '\\"')
        )
        try:
            subprocess.Popen(
                ["osascript", "-e", f'display notification "{safe}" with title "emerge"'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            pass
        return {"action": "dismissed", "value": ""}

    if sys.platform == "win32":
        # PowerShell balloon tip via System.Windows.Forms (always available on Windows).
        # Single-quotes safe for PS string; double-quotes escaped above for PS interpolation.
        safe = body.replace("'", "''").replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$n=[System.Windows.Forms.NotifyIcon]::new();"
            "$n.Icon=[System.Drawing.SystemIcons]::Information;"
            "$n.Visible=$true;"
            f"$n.ShowBalloonTip({timeout_s * 1000},'emerge','{safe}',"
            "[System.Windows.Forms.ToolTipIcon]::Info);"
            f"Start-Sleep -Milliseconds {timeout_s * 1000 + 500};"
            "$n.Dispose()"
        )
        try:
            subprocess.Popen(
                ["powershell", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", ps],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=0x08000000,  # CREATE_NO_WINDOW — suppress console flash
            )
        except FileNotFoundError:
            pass
        return {"action": "dismissed", "value": ""}

    # Linux / other: tkinter in a subprocess (isolated display context).
    tk_script = (
        "import tkinter as tk\n"
        "root = tk.Tk()\n"
        "root.overrideredirect(True)\n"
        "root.attributes('-topmost', True)\n"
        "root.update_idletasks()\n"
        "sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()\n"
        "w, h = 300, 64\n"
        "root.geometry(f'{w}x{h}+{sw - w - 20}+{sh - h - 60}')\n"
        f"tk.Label(root, text={body!r}, wraplength=280, font=('', 10), justify='left')"
        ".pack(pady=8, padx=12, anchor='w')\n"
        f"root.after({timeout_s * 1000}, root.destroy)\n"
        "root.mainloop()\n"
    )
    try:
        subprocess.Popen(
            [sys.executable, "-c", tk_script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass
    return {"action": "dismissed", "value": ""}


def _render_info(*, body: str, title: str) -> dict[str, Any]:
    import tkinter as tk

    root = tk.Tk()
    root.title(title)
    _set_window_icon(root)
    root.attributes("-topmost", True)
    root.resizable(False, False)
    tk.Label(root, text=body, wraplength=300, font=("", 11), justify="center").pack(
        pady=(16, 8), padx=16
    )
    tk.Button(root, text="Close", command=root.destroy, width=10).pack(pady=(0, 14))
    root.mainloop()
    return {"action": "dismissed", "value": ""}


def show_input_bubble(on_submit: "Callable[[str, list], None]", upload_url: str = "") -> None:
    """Open RichInputWidget bubble. Calls on_submit(text, attachments) on send."""
    import tkinter as tk
    root = tk.Tk()
    _set_window_icon(root)
    RichInputWidget(root, on_submit=on_submit, upload_url=upload_url, title="emerge")
    root.mainloop()
