from __future__ import annotations

from typing import Any, Callable


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
