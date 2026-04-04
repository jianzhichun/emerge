from __future__ import annotations

from typing import Any, Callable


class LocalNotifier:
    """Shows operator_popup dialog directly in the current process."""

    def notify(
        self,
        stage: str,
        message: str,
        intent_draft: str = "",
        timeout_s: int = 0,
    ) -> dict[str, Any]:
        try:
            from scripts.operator_popup import show_notify
            return show_notify(
                stage=stage,
                message=message,
                intent_draft=intent_draft,
                timeout_s=timeout_s,
            )
        except Exception as exc:
            return {"action": "skip", "intent": "", "error": str(exc)}


class RemoteNotifier:
    """Sends notification request to a runner via POST /notify."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def notify(
        self,
        stage: str,
        message: str,
        intent_draft: str = "",
        timeout_s: int = 0,
    ) -> dict[str, Any]:
        try:
            return self._client.notify(
                stage=stage,
                message=message,
                intent_draft=intent_draft,
                timeout_s=timeout_s,
            )
        except Exception as exc:
            return {"action": "skip", "intent": "", "error": str(exc)}


class NotificationDispatcher:
    """Routes notifications to remote runner or local fallback.

    Always co-fires mcp_push_fn (non-blocking CC path) then dispatches
    to OS-native dialog and waits for the operator's response.
    """

    def __init__(
        self,
        mcp_push_fn: Callable[[str, str], None],
        runner_router: Any | None = None,
    ) -> None:
        self._mcp_push = mcp_push_fn
        self._router = runner_router

    def dispatch(
        self,
        stage: str,
        message: str,
        intent_draft: str = "",
        timeout_s: int = 0,
        machine_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Send notification via MCP (non-blocking) and OS dialog (blocking).

        Returns the operator's response dict from the OS dialog.
        """
        # Always push to CC (fire-and-forget)
        try:
            self._mcp_push(stage, message)
        except Exception:
            pass

        # OS dialog: remote first, local fallback
        return self._notify_os(stage, message, intent_draft, timeout_s, machine_ids)

    def _notify_os(
        self,
        stage: str,
        message: str,
        intent_draft: str,
        timeout_s: int,
        machine_ids: list[str] | None,
    ) -> dict[str, Any]:
        if self._router is not None:
            profile = (machine_ids or [None])[0] or "default"
            client = self._router.find_client({"target_profile": profile})
            if client is not None:
                return RemoteNotifier(client).notify(
                    stage=stage,
                    message=message,
                    intent_draft=intent_draft,
                    timeout_s=timeout_s,
                )
        return LocalNotifier().notify(
            stage=stage,
            message=message,
            intent_draft=intent_draft,
            timeout_s=timeout_s,
        )
