from __future__ import annotations
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_local_notifier_calls_show_notify(monkeypatch):
    import scripts.operator_popup as popup_mod
    from scripts.notify_dispatcher import LocalNotifier

    monkeypatch.setattr(popup_mod, "show_notify",
        lambda stage, message, intent_draft="", timeout_s=0:
            {"action": "confirm", "intent": "test intent"})

    notifier = LocalNotifier()
    result = notifier.notify(stage="explore", message="msg", intent_draft="draft")
    assert result["action"] == "confirm"
    assert result["intent"] == "test intent"


def test_local_notifier_returns_skip_on_error(monkeypatch):
    import scripts.operator_popup as popup_mod
    from scripts.notify_dispatcher import LocalNotifier

    def raise_err(stage, message, intent_draft="", timeout_s=0):
        raise RuntimeError("no display")

    monkeypatch.setattr(popup_mod, "show_notify", raise_err)

    notifier = LocalNotifier()
    result = notifier.notify(stage="canary", message="msg")
    assert result["action"] == "skip"
    assert "error" in result


def test_remote_notifier_calls_runner_client(monkeypatch):
    from scripts.notify_dispatcher import RemoteNotifier

    class FakeClient:
        def notify(self, stage, message, intent_draft="", timeout_s=0):
            return {"action": "takeover", "intent": ""}

    notifier = RemoteNotifier(client=FakeClient())
    result = notifier.notify(stage="canary", message="接管？")
    assert result["action"] == "takeover"


def test_remote_notifier_returns_skip_on_error(monkeypatch):
    from scripts.notify_dispatcher import RemoteNotifier

    class FailClient:
        def notify(self, stage, message, intent_draft="", timeout_s=0):
            raise RuntimeError("connection refused")

    notifier = RemoteNotifier(client=FailClient())
    result = notifier.notify(stage="canary", message="msg")
    assert result["action"] == "skip"
    assert "error" in result


def test_dispatcher_uses_remote_when_runner_available(monkeypatch):
    from scripts.notify_dispatcher import NotificationDispatcher

    mcp_calls = []
    remote_calls = []

    class FakeRouter:
        def find_client(self, args):
            class C:
                def notify(self, stage, message, intent_draft="", timeout_s=0):
                    remote_calls.append(stage)
                    return {"action": "takeover", "intent": ""}
            return C()

    dispatcher = NotificationDispatcher(
        mcp_push_fn=lambda stage, msg: mcp_calls.append(stage),
        runner_router=FakeRouter(),
    )
    result = dispatcher.dispatch(stage="canary", message="msg")
    assert result["action"] == "takeover"
    assert mcp_calls == ["canary"]   # MCP always fires
    assert remote_calls == ["canary"]  # remote used


def test_dispatcher_falls_back_to_local_when_no_runner(monkeypatch):
    import scripts.operator_popup as popup_mod
    from scripts.notify_dispatcher import NotificationDispatcher

    monkeypatch.setattr(popup_mod, "show_notify",
        lambda stage, message, intent_draft="", timeout_s=0:
            {"action": "confirm", "intent": "local"})

    mcp_calls = []
    dispatcher = NotificationDispatcher(
        mcp_push_fn=lambda stage, msg: mcp_calls.append(stage),
        runner_router=None,
    )
    result = dispatcher.dispatch(stage="explore", message="msg", intent_draft="draft")
    assert result["action"] == "confirm"
    assert result["intent"] == "local"
    assert mcp_calls == ["explore"]


def test_dispatcher_machines_param_selects_runner(monkeypatch):
    """machine_ids[0] is used as target_profile for runner selection."""
    from scripts.notify_dispatcher import NotificationDispatcher

    selected_profiles = []

    class FakeRouter:
        def find_client(self, args):
            selected_profiles.append(args.get("target_profile"))
            class C:
                def notify(self, stage, message, intent_draft="", timeout_s=0):
                    return {"action": "manual", "intent": ""}
            return C()

    dispatcher = NotificationDispatcher(
        mcp_push_fn=lambda *a: None,
        runner_router=FakeRouter(),
    )
    dispatcher.dispatch(stage="canary", message="msg", machine_ids=["mycader-1"])
    assert selected_profiles == ["mycader-1"]
