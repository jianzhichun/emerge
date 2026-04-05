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
