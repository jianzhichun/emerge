from __future__ import annotations

from typing import Any


class LocalNotifier:
    """Shows operator_popup dialog directly in the current process."""

    def notify(self, ui_spec: dict) -> dict[str, Any]:
        try:
            from scripts.operator_popup import show_notify
            return show_notify(ui_spec)
        except Exception as exc:
            return {"action": "skip", "value": "", "error": str(exc)}


class RemoteNotifier:
    """Sends notification request to a runner via POST /notify."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def notify(self, ui_spec: dict) -> dict[str, Any]:
        try:
            return self._client.notify(ui_spec)
        except Exception as exc:
            return {"action": "skip", "value": "", "error": str(exc)}


class NotificationDispatcher:
    """Routes OS-native dialog to remote runner or local fallback.

    MCP push is the daemon's responsibility; this class handles only the
    OS dialog routing. CC calls this via icc_exec when it decides to engage
    the operator.
    """

    def __init__(self, runner_router: Any | None = None) -> None:
        self._router = runner_router

    def dispatch(
        self,
        ui_spec: dict,
        machine_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Route ui_spec dialog to remote runner or local Tkinter.

        Returns operator response: {action, value}.
        """
        if self._router is not None:
            profile = (machine_ids or [None])[0] or "default"
            client = self._router.find_client({"target_profile": profile})
            if client is not None:
                return RemoteNotifier(client).notify(ui_spec)
        return LocalNotifier().notify(ui_spec)
