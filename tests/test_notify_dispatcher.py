from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_CHOICE_SPEC = {"type": "choice", "body": "接管？", "options": ["好", "不用"]}
_INPUT_SPEC = {"type": "input", "body": "你在做什么？", "prefill": "草稿"}


def test_local_notifier_calls_show_notify(monkeypatch):
    import scripts.operator_popup as popup_mod
    from scripts.notify_dispatcher import LocalNotifier

    monkeypatch.setattr(popup_mod, "show_notify",
        lambda spec: {"action": "selected", "value": spec["options"][0]})
    result = LocalNotifier().notify(_CHOICE_SPEC)
    assert result == {"action": "selected", "value": "好"}


def test_local_notifier_returns_skip_on_error(monkeypatch):
    import scripts.operator_popup as popup_mod
    from scripts.notify_dispatcher import LocalNotifier

    def raise_err(spec):
        raise RuntimeError("no display")

    monkeypatch.setattr(popup_mod, "show_notify", raise_err)
    result = LocalNotifier().notify(_CHOICE_SPEC)
    assert result["action"] == "skip"
    assert "error" in result


def test_remote_notifier_calls_runner_client():
    from scripts.notify_dispatcher import RemoteNotifier

    class FakeClient:
        def notify(self, ui_spec):
            return {"action": "selected", "value": ui_spec["options"][0]}

    result = RemoteNotifier(client=FakeClient()).notify(_CHOICE_SPEC)
    assert result == {"action": "selected", "value": "好"}


def test_remote_notifier_returns_skip_on_error():
    from scripts.notify_dispatcher import RemoteNotifier

    class FailClient:
        def notify(self, ui_spec):
            raise RuntimeError("connection refused")

    result = RemoteNotifier(client=FailClient()).notify(_CHOICE_SPEC)
    assert result["action"] == "skip"
    assert "error" in result


def test_dispatcher_uses_remote_when_runner_available(monkeypatch):
    from scripts.notify_dispatcher import NotificationDispatcher

    remote_calls = []

    class FakeRouter:
        def find_client(self, args):
            class C:
                def notify(self, ui_spec):
                    remote_calls.append(ui_spec["type"])
                    return {"action": "selected", "value": "好"}
            return C()

    dispatcher = NotificationDispatcher(runner_router=FakeRouter())
    result = dispatcher.dispatch(_CHOICE_SPEC)
    assert result == {"action": "selected", "value": "好"}
    assert remote_calls == ["choice"]


def test_dispatcher_falls_back_to_local_when_no_runner(monkeypatch):
    import scripts.operator_popup as popup_mod
    from scripts.notify_dispatcher import NotificationDispatcher

    monkeypatch.setattr(popup_mod, "show_notify",
        lambda spec: {"action": "confirmed", "value": "local"})

    result = NotificationDispatcher(runner_router=None).dispatch(_INPUT_SPEC)
    assert result == {"action": "confirmed", "value": "local"}


def test_dispatcher_machine_ids_selects_runner_profile(monkeypatch):
    from scripts.notify_dispatcher import NotificationDispatcher

    selected_profiles = []

    class FakeRouter:
        def find_client(self, args):
            selected_profiles.append(args.get("target_profile"))
            class C:
                def notify(self, ui_spec): return {"action": "selected", "value": ""}
            return C()

    NotificationDispatcher(runner_router=FakeRouter()).dispatch(
        _CHOICE_SPEC, machine_ids=["mycader-1"]
    )
    assert selected_profiles == ["mycader-1"]
