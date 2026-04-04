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
