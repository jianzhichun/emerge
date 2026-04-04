from __future__ import annotations
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_show_notify_unknown_stage_returns_skip():
    from scripts.operator_popup import show_notify
    result = show_notify(stage="unknown", message="test")
    assert result == {"action": "skip", "intent": ""}


def test_show_notify_graceful_on_no_display(monkeypatch):
    """When tkinter has no display, show_notify returns skip with error field."""
    import scripts.operator_popup as popup_mod
    import tkinter as tk

    def bad_tk(*args, **kwargs):
        raise RuntimeError("no display")

    monkeypatch.setattr(tk, "Tk", bad_tk)
    result = popup_mod.show_notify(stage="canary", message="test msg")
    assert result["action"] == "skip"
    assert "error" in result


def test_show_notify_explore_confirm(monkeypatch):
    """Simulate user editing intent and clicking 确认."""
    import scripts.operator_popup as popup_mod

    def mock_show_explore(message, intent_draft):
        return {"action": "confirm", "intent": "edited: " + intent_draft}

    monkeypatch.setattr(popup_mod, "_show_explore", mock_show_explore)
    result = popup_mod.show_notify(
        stage="explore",
        message="重复 5 次",
        intent_draft="AI 草稿",
    )
    assert result["action"] == "confirm"
    assert result["intent"] == "edited: AI 草稿"


def test_show_notify_canary_takeover(monkeypatch):
    """Simulate user clicking 接管 in canary dialog."""
    import scripts.operator_popup as popup_mod

    def mock_show_canary(message, timeout_s):
        return {"action": "takeover", "intent": ""}

    monkeypatch.setattr(popup_mod, "_show_canary", mock_show_canary)
    result = popup_mod.show_notify(stage="canary", message="接管？", timeout_s=0)
    assert result["action"] == "takeover"


def test_show_notify_stable_auto_takeover(monkeypatch):
    """Stable passes timeout_s to _show_canary."""
    import scripts.operator_popup as popup_mod

    captured = {}

    def mock_show_canary(message, timeout_s):
        captured["timeout_s"] = timeout_s
        return {"action": "takeover", "intent": ""}

    monkeypatch.setattr(popup_mod, "_show_canary", mock_show_canary)
    popup_mod.show_notify(stage="stable", message="stable msg", timeout_s=10)
    assert captured["timeout_s"] == 10
