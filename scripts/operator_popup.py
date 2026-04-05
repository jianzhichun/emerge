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
