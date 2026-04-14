from __future__ import annotations
import sys
from pathlib import Path

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
        def update_idletasks(self): pass
        def winfo_screenwidth(self): return 1920
        def winfo_screenheight(self): return 1080
        def geometry(self, *a): pass
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
        def update_idletasks(self): pass
        def winfo_screenwidth(self): return 1920
        def winfo_screenheight(self): return 1080
        def geometry(self, *a): pass
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
