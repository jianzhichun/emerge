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


# ── Emerge Design System ─────────────────────────────────────────────────────
# Dark palette — slate scale + cyan accent, readable on any monitor.
_T = {
    "bg":      "#0f172a",   # slate-900  window background
    "surface": "#1e293b",   # slate-800  inputs / chips
    "border":  "#334155",   # slate-700  borders / separators
    "text":    "#e2e8f0",   # slate-200  primary text
    "muted":   "#64748b",   # slate-500  countdown / hint text
    "accent":  "#06b6d4",   # cyan-500   primary action / accent bar
    "acc_dk":  "#0891b2",   # cyan-600   accent hover
    "neutral": "#334155",   # slate-700  secondary button bg
    "neu_dk":  "#475569",   # slate-600  secondary button hover
    "danger":  "#1e293b",   # same as surface (cancel is low-key, not red)
    "dan_dk":  "#334155",
}
_FONT    = ("TkDefaultFont", 11)
_FONT_SM = ("TkDefaultFont", 9)
_FONT_TT = ("TkFixedFont", 10)   # monospace for input area


def _style_win(win: Any, title: str) -> None:
    """Apply emerge dark theme to a Toplevel/Tk window."""
    win.title(title)
    win.configure(bg=_T["bg"])


def _accent_bar(parent: Any) -> None:
    """3px cyan accent strip pinned to the top of the window."""
    import tkinter as tk
    tk.Frame(parent, bg=_T["accent"], height=3).pack(fill="x", side="top")


def _center_win(win: Any) -> None:
    """Center window on screen after geometry is known."""
    win.update_idletasks()
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    w, h = win.winfo_reqwidth(), win.winfo_reqheight()
    win.geometry(f"+{int(sw / 2 - w / 2)}+{int(sh / 2 - h / 2)}")


def _mk_btn(
    parent: Any,
    text: str,
    command: Any,
    primary: bool = False,
    width: int = 10,
) -> Any:
    """Flat themed button with hover effect."""
    import tkinter as tk
    bg  = _T["accent"]  if primary else _T["neutral"]
    hvr = _T["acc_dk"] if primary else _T["neu_dk"]
    b = tk.Button(
        parent, text=text, command=command, width=width,
        relief="flat", bd=0,
        bg=bg, fg="#ffffff",
        activebackground=hvr, activeforeground="#ffffff",
        cursor="hand2", padx=12, pady=7,
        font=_FONT,
    )
    b.bind("<Enter>", lambda _e, _b=b, _h=hvr: _b.config(bg=_h))
    b.bind("<Leave>", lambda _e, _b=b, _bg=bg: _b.config(bg=_bg))
    return b


class RichInputWidget:
    """Claude-Code-style input widget: text area + attachment chips + upload.

    Layout:
        ┌────────────────────────────────────┐
        │░░░░░░░ emerge accent bar ░░░░░░░░░░│
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

        _style_win(parent, title)
        _accent_bar(parent)
        parent.attributes("-topmost", True)
        parent.resizable(True, False)
        parent.update_idletasks()
        sw = parent.winfo_screenwidth()
        sh = parent.winfo_screenheight()
        w, h = 500, 192
        parent.geometry(f"{w}x{h}+{int(sw / 2 - w / 2)}+{int(sh / 2 - h / 2)}")

        # Text area wrapped in a 1px border-frame (slate-700 bg = border colour)
        text_border = tk.Frame(parent, bg=_T["border"])
        text_border.pack(padx=12, pady=(10, 4), fill="x")
        self._text = tk.Text(
            text_border, height=4, width=56,
            relief="flat", bd=0,
            bg=_T["surface"], fg=_T["text"],
            insertbackground=_T["text"],
            selectbackground=_T["accent"],
            font=_FONT_TT, wrap="word",
            padx=8, pady=6,
        )
        self._text.pack(fill="x", padx=1, pady=1)
        self._text.focus_set()

        self._chips_frame = tk.Frame(parent, bg=_T["bg"])
        self._chips_frame.pack(padx=12, fill="x")

        # Separator
        tk.Frame(parent, bg=_T["border"], height=1).pack(fill="x", padx=0, pady=(4, 0))

        toolbar = tk.Frame(parent, bg=_T["bg"])
        toolbar.pack(padx=12, pady=8, fill="x")

        def _file_btn(text: str, cmd: Any) -> Any:
            b = tk.Button(
                toolbar, text=text, command=cmd,
                relief="flat", bd=0,
                bg=_T["surface"], fg=_T["muted"],
                activebackground=_T["border"], activeforeground=_T["text"],
                cursor="hand2", padx=8, pady=5, font=_FONT_SM,
            )
            b.bind("<Enter>", lambda _e, _b=b: _b.config(fg=_T["text"]))
            b.bind("<Leave>", lambda _e, _b=b: _b.config(fg=_T["muted"]))
            return b

        _file_btn("📁 文件", self._pick_file).pack(side="left", padx=(0, 6))
        _file_btn("🖼 图片", self._pick_image).pack(side="left")
        self._send_btn = _mk_btn(toolbar, "发送 ↵", self._on_send, primary=True, width=9)
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
        paths = re.findall(r"\{([^}]+)\}|(\S+)", raw)
        for braced, plain in paths:
            p = Path(braced or plain)
            if p.exists():
                self._add_attachment(p)

    def _add_attachment(self, filepath: "Path") -> None:
        import tkinter as tk
        import threading
        import uuid as _uuid_mod

        slot_id = _uuid_mod.uuid4().hex
        cancelled: set[str] = set()

        chip_var = tk.StringVar(value=f"⏳ {filepath.name}")
        chip_border = tk.Frame(self._chips_frame, bg=_T["border"])
        chip_border.pack(side="left", padx=(0, 5), pady=3)
        chip_inner = tk.Frame(chip_border, bg=_T["surface"])
        chip_inner.pack(padx=1, pady=1)
        chip_label = tk.Label(
            chip_inner, textvariable=chip_var,
            bg=_T["surface"], fg=_T["text"], font=_FONT_SM,
        )
        chip_label.pack(side="left", padx=(6, 2))

        def _remove_this_chip() -> None:
            cancelled.add(slot_id)
            self._attachments = [a for a in self._attachments if a.get("_slot") != slot_id]
            chip_border.destroy()

        tk.Button(
            chip_inner, text="×", font=_FONT_SM,
            relief="flat", bd=0,
            bg=_T["surface"], fg=_T["muted"],
            activebackground=_T["border"], activeforeground=_T["text"],
            cursor="hand2",
            command=_remove_this_chip,
        ).pack(side="left", padx=(0, 4))

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
                _schedule(_on_success)
            except RuntimeError:
                _schedule(lambda: chip_var.set(f"❌ {filepath.name}"))
                _schedule(lambda: chip_label.config(fg="#f43f5e"))
            finally:
                def _finish():
                    self._pending -= 1
                    if self._pending == 0:
                        self._send_btn.config(state="normal")
                _schedule(_finish)

        threading.Thread(target=_do_upload, daemon=True).start()

    def _on_send(self) -> None:
        if self._pending > 0:  # uploads in flight — guard keyboard shortcut too
            return
        text = self._text.get("1.0", "end-1c").strip()
        self._root.destroy()
        if text or self._attachments:
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
    ui_type = str(ui_spec.get("type", "") or "")
    if ui_type not in {"choice", "input", "confirm", "info", "toast"}:
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
        renderers = {
            "input": lambda: _render_input(
                body=body,
                prefill=str(ui_spec.get("prefill", "")),
                title=title,
                upload_url=str(ui_spec.get("upload_url", "")),
            ),
            "confirm": lambda: _render_confirm(body=body, title=title),
            "toast": lambda: _render_toast(body=body, timeout_s=max(1, timeout_s or 5)),
            "info": lambda: _render_info(body=body, title=title),
        }
        return renderers[ui_type]()
    except Exception as exc:
        return {"action": "skip", "value": "", "error": str(exc)}

def _set_window_icon(root: Any) -> None:
    """Set emerge icon on a tkinter Tk/Toplevel window. Silently no-ops on any error."""
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


# ── Tkinter 专用线程调度器 ─────────────────────────────────────────────────
# tkinter 不是线程安全的，Tk() 必须在固定线程创建和驱动。
# 所有阻塞弹窗通过 _tk_dispatch 派发到该线程。
# fn(root, on_result) 是非阻塞的：立即返回，用户点击时调用 on_result(dict)。
# 这样 mainloop 始终在运转，不会被 wait_window() 的嵌套事件循环阻塞
#（Windows Session 0 下 wait_window() 会永久挂起）。
import queue as _queue
import threading as _threading

_tk_dispatch_queue: _queue.Queue = _queue.Queue()
_tk_root: Any = None
_tk_thread: "_threading.Thread | None" = None
_tk_thread_lock = _threading.Lock()
_tk_start_error: Exception | None = None


def _ensure_tk_thread() -> None:
    global _tk_root, _tk_thread, _tk_start_error
    with _tk_thread_lock:
        if _tk_thread is not None and _tk_thread.is_alive():
            return
        _tk_start_error = None
        ready = _threading.Event()

        def _run() -> None:
            global _tk_root, _tk_start_error
            import tkinter as tk
            try:
                _tk_root = tk.Tk()
                # Keep a 1×1 off-screen transparent window so Windows background
                # processes keep the event loop pumping (withdraw() exits mainloop
                # immediately when no visible windows exist on some configs).
                if hasattr(_tk_root, "overrideredirect"):
                    _tk_root.overrideredirect(True)
                if hasattr(_tk_root, "geometry"):
                    _tk_root.geometry("1x1+-10000+-10000")
                if hasattr(_tk_root, "attributes"):
                    _tk_root.attributes("-alpha", 0.0)
                if hasattr(_tk_root, "after"):
                    _tk_root.after(50, _tk_poll)
                ready.set()
                if hasattr(_tk_root, "mainloop"):
                    _tk_root.mainloop()
            except Exception as exc:
                _tk_start_error = exc
                _tk_root = None
                ready.set()

        _tk_thread = _threading.Thread(target=_run, name="tk-main", daemon=True)
        _tk_thread.start()
        ready.wait(timeout=5)
        if _tk_root is None:
            if _tk_start_error is not None:
                raise RuntimeError(f"tk unavailable: {_tk_start_error}")
            raise RuntimeError("tk unavailable: startup timeout")


def _tk_poll() -> None:
    global _tk_root
    try:
        while True:
            fn, on_result = _tk_dispatch_queue.get_nowait()
            try:
                fn(_tk_root, on_result)  # non-blocking; on_result called later by button handler
            except Exception as exc:
                import logging as _lg
                _lg.warning("tk-poll: _build raised %s: %s", type(exc).__name__, exc, exc_info=True)
                on_result({"action": "dismissed", "value": "", "error": str(exc)})
    except _queue.Empty:
        pass
    if _tk_root is not None:
        _tk_root.after(50, _tk_poll)


def _tk_dispatch(fn: Callable) -> dict[str, Any]:
    """Schedule fn(root, on_result) on tk main thread; block caller until on_result fires (max 120 s)."""
    _ensure_tk_thread()
    holder: list = []
    ev = _threading.Event()

    def on_result(result: dict) -> None:
        holder.append(result)
        ev.set()

    if _tk_root is not None and not hasattr(_tk_root, "after"):
        # Test doubles may not implement tkinter scheduling primitives.
        try:
            fn(_tk_root, on_result)
        except Exception as exc:
            return {"action": "skip", "value": "", "error": str(exc)}
        return holder[0] if holder else {"action": "dismissed", "value": ""}

    _tk_dispatch_queue.put((fn, on_result))
    ev.wait(timeout=120)
    return holder[0] if holder else {"action": "dismissed", "value": ""}


# ── 弹窗渲染函数 ────────────────────────────────────────────────────────────

def _render_choice(*, body: str, options: list[str], title: str, timeout_s: int) -> dict[str, Any]:
    def _build(root: Any, on_result: Callable) -> None:
        import tkinter as tk
        win = tk.Toplevel(root)
        _style_win(win, title)
        _set_window_icon(win)
        _accent_bar(win)
        win.attributes("-topmost", True)
        win.resizable(False, False)
        win.lift()
        win.focus_force()
        result: dict[str, Any] = {"action": "dismissed", "value": ""}
        fired = [False]

        def _finish(r: dict) -> None:
            if not fired[0]:
                fired[0] = True
                on_result(r)

        tk.Label(
            win, text=body,
            wraplength=320, font=_FONT, justify="center",
            bg=_T["bg"], fg=_T["text"],
        ).pack(pady=(20, 6), padx=24)

        if timeout_s > 0:
            countdown_var = tk.StringVar(
                value=f"Auto-selecting '{options[0]}' in {timeout_s}s"
            )
            tk.Label(
                win, textvariable=countdown_var,
                font=_FONT_SM, bg=_T["bg"], fg=_T["muted"],
            ).pack(pady=(0, 4))
            remaining = [timeout_s]

            def update_countdown() -> None:
                if fired[0]:
                    return
                remaining[0] -= 1
                if remaining[0] <= 0:
                    result["action"] = "selected"
                    result["value"] = options[0]
                    win.destroy()
                    _finish(result)
                    return
                countdown_var.set(f"Auto-selecting '{options[0]}' in {remaining[0]}s")
                win.after(1000, update_countdown)

            win.after(1000, update_countdown)

        btn_frame = tk.Frame(win, bg=_T["bg"])
        btn_frame.pack(pady=(10, 20))

        for i, opt in enumerate(options):
            def make_handler(o: str) -> Any:
                def handler() -> None:
                    result["action"] = "selected"
                    result["value"] = o
                    win.destroy()
                    _finish(result)
                return handler

            _mk_btn(btn_frame, opt, make_handler(opt), primary=(i == 0)).pack(
                side="left", padx=5
            )

        _center_win(win)
        win.protocol("WM_DELETE_WINDOW", lambda: _finish(result))

    return _tk_dispatch(_build)


def _render_input(*, body: str, prefill: str, title: str, upload_url: str = "") -> dict[str, Any]:
    def _build(root: Any, on_result: Callable) -> None:
        import tkinter as tk
        win = tk.Toplevel(root)
        _set_window_icon(win)
        win.attributes("-topmost", True)
        win.lift()
        win.focus_force()
        result: dict[str, Any] = {"action": "dismissed", "value": "", "attachments": []}
        fired = [False]

        def _finish(r: dict) -> None:
            if not fired[0]:
                fired[0] = True
                on_result(r)

        if body:
            tk.Label(
                win, text=body, wraplength=460, justify="left",
                bg=_T["bg"], fg=_T["text"], font=_FONT,
            ).pack(pady=(14, 4), padx=14, anchor="w")

        def _on_submit(text: str, attachments: list) -> None:
            result["action"] = "confirmed"
            result["value"] = text
            result["attachments"] = attachments
            win.destroy()
            _finish(result)

        widget = RichInputWidget(win, on_submit=_on_submit, upload_url=upload_url, title=title)
        if prefill:
            widget._text.insert("1.0", prefill)
        win.protocol("WM_DELETE_WINDOW", lambda: _finish(result))

    return _tk_dispatch(_build)


def _render_confirm(*, body: str, title: str) -> dict[str, Any]:
    def _build(root: Any, on_result: Callable) -> None:
        import tkinter as tk
        win = tk.Toplevel(root)
        _style_win(win, title)
        _set_window_icon(win)
        _accent_bar(win)
        win.attributes("-topmost", True)
        win.resizable(False, False)
        win.lift()
        win.focus_force()
        result: dict[str, Any] = {"action": "dismissed", "value": ""}
        fired = [False]

        def _finish(r: dict) -> None:
            if not fired[0]:
                fired[0] = True
                on_result(r)

        tk.Label(
            win, text=body,
            wraplength=320, font=_FONT, justify="center",
            bg=_T["bg"], fg=_T["text"],
        ).pack(pady=(20, 8), padx=24)

        btn_frame = tk.Frame(win, bg=_T["bg"])
        btn_frame.pack(pady=(8, 20))

        def on_confirm() -> None:
            result["action"] = "confirmed"
            result["value"] = "confirmed"
            win.destroy()
            _finish(result)

        def on_dismiss() -> None:
            win.destroy()
            _finish(result)

        _mk_btn(btn_frame, "Cancel", on_dismiss, primary=False).pack(side="left", padx=5)
        _mk_btn(btn_frame, "Confirm", on_confirm, primary=True).pack(side="left", padx=5)
        _center_win(win)
        win.protocol("WM_DELETE_WINDOW", on_dismiss)

    return _tk_dispatch(_build)


def _render_toast(*, body: str, timeout_s: int) -> dict[str, Any]:
    """Non-interactive toast that auto-dismisses after timeout_s seconds.

    Platform dispatch:
      macOS   → osascript display notification (no NSApp conflict with pystray)
      Windows → PowerShell balloon via System.Windows.Forms.NotifyIcon
      other   → subprocess-isolated tkinter window
    """
    import subprocess, sys

    if sys.platform == "darwin":
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
                creationflags=0x08000000,
            )
        except FileNotFoundError:
            pass
        return {"action": "dismissed", "value": ""}

    # Linux / other: emerge-themed tkinter toast in a subprocess.
    bg, text, accent = _T["bg"], _T["text"], _T["accent"]
    tk_script = (
        "import tkinter as tk\n"
        "root = tk.Tk()\n"
        "root.overrideredirect(True)\n"
        "root.attributes('-topmost', True)\n"
        f"root.configure(bg={bg!r})\n"
        "root.update_idletasks()\n"
        "sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()\n"
        "w, h = 320, 72\n"
        "root.geometry(f'{w}x{h}+{sw - w - 20}+{sh - h - 60}')\n"
        f"tk.Frame(root, bg={accent!r}, height=3).pack(fill='x', side='top')\n"
        f"tk.Label(root, text={body!r}, wraplength=296, font=('TkDefaultFont',10),"
        f" bg={bg!r}, fg={text!r}, justify='left').pack(pady=10, padx=12, anchor='w')\n"
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
    def _build(root: Any, on_result: Callable) -> None:
        import tkinter as tk
        win = tk.Toplevel(root)
        _style_win(win, title)
        _set_window_icon(win)
        _accent_bar(win)
        win.attributes("-topmost", True)
        win.resizable(False, False)
        win.lift()
        win.focus_force()
        result: dict[str, Any] = {"action": "dismissed", "value": ""}
        fired = [False]

        def _finish() -> None:
            if not fired[0]:
                fired[0] = True
                win.destroy()
                on_result(result)

        tk.Label(
            win, text=body,
            wraplength=320, font=_FONT, justify="center",
            bg=_T["bg"], fg=_T["text"],
        ).pack(pady=(20, 8), padx=24)

        _mk_btn(win, "Close", _finish, primary=False, width=10).pack(pady=(4, 20))
        _center_win(win)
        win.protocol("WM_DELETE_WINDOW", _finish)

    return _tk_dispatch(_build)


def show_input_bubble(
    on_submit: "Callable[[str, list], None]",
    upload_url: str = "",
    on_close: "Callable[[], None] | None" = None,
) -> None:
    """Open RichInputWidget bubble. Calls on_submit(text, attachments) on send.

    on_close is called when the window is destroyed (submit or X button).
    """
    _ensure_tk_thread()

    def _build(root: Any, _on_result: Callable) -> None:
        import tkinter as tk
        win = tk.Toplevel(root)
        _set_window_icon(win)
        RichInputWidget(win, on_submit=on_submit, upload_url=upload_url, title="emerge")
        if on_close:
            win.bind(
                "<Destroy>",
                lambda e, _w=win: on_close() if e.widget is _w else None,
            )

    if _tk_root is not None and not hasattr(_tk_root, "after"):
        try:
            # Lightweight fallback for tests with minimal Tk doubles.
            RichInputWidget(_tk_root, on_submit=on_submit, upload_url=upload_url, title="emerge")
        except Exception:
            pass
        return
    _tk_dispatch_queue.put((_build, lambda _: None))
