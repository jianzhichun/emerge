from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from scripts.admin.actions.registry import ActionContext, ActionRegistry, ActionSpec


@dataclass(frozen=True)
class IntentSetPayload:
    key: str
    fields: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IntentDeletePayload:
    key: str


@dataclass(frozen=True)
class NotesCommentPayload:
    connector: str
    comment: str


@dataclass(frozen=True)
class NotesEditPayload:
    connector: str
    content: str


@dataclass(frozen=True)
class ToolCallPayload:
    call: dict[str, Any]
    intent_signature: str | None = None
    connector: str | None = None
    scenario: str | None = None
    auto: dict[str, Any] = field(default_factory=dict)
    flywheel: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CoreCrystallizePayload:
    connector: str
    component: str


@dataclass(frozen=True)
class CorePromptPayload:
    prompt: str


def _enrich_core_prompt(
    action: dict[str, Any], _payload: CorePromptPayload, _ctx: ActionContext
) -> dict[str, Any]:
    a = dict(action)
    a["instruction"] = (
        "The user has queued a free-form instruction via the Emerge Cockpit. "
        "Execute the `prompt` field as a direct user request. "
        "Treat it exactly as if the user had typed it in the chat."
    )
    return a


def _enrich_notes_comment(
    action: dict[str, Any], payload: NotesCommentPayload, ctx: ActionContext
) -> dict[str, Any]:
    a = dict(action)
    notes_path = ctx.connector_root / payload.connector / "NOTES.md"
    try:
        notes_path.resolve().relative_to(ctx.connector_root.resolve())
        current_notes = notes_path.read_text(encoding="utf-8") if notes_path.exists() else ""
    except (ValueError, OSError):
        current_notes = ""
    a["current_notes"] = current_notes
    a["notes_path"] = str(notes_path)
    a["instruction"] = (
        "The user has provided an edit instruction for the connector's NOTES.md. "
        "Read `current_notes`, apply the `comment` as a natural-language edit "
        "(e.g. fix a mistake, add a detail, restructure a section, remove stale info). "
        "Rewrite the file at `notes_path` with your judgment — do NOT blindly append. "
        "Preserve existing useful content. Keep the file concise and accurate."
    )
    return a


def _enrich_tool_call(
    action: dict[str, Any], payload: ToolCallPayload, _ctx: ActionContext
) -> dict[str, Any]:
    a = dict(action)
    call = payload.call if isinstance(payload.call, dict) else {}
    tool_name = str(call.get("tool", "")).strip()
    arguments = call.get("arguments", {})
    if not tool_name or not isinstance(arguments, dict):
        a["instruction"] = (
            "Invalid cockpit tool-call payload. "
            "Expected call.tool (any icc_* tool name) and call.arguments object."
        )
        return a
    auto_mode = str((payload.auto or {}).get("mode", "assist"))
    a["instruction"] = (
        "Deterministic tool call (no free-form reasoning): "
        f"call `{tool_name}` exactly once with `call.arguments`; "
        "return the tool output to the user. "
        f"intent_signature={payload.intent_signature or ''}. "
        f"automation_mode={auto_mode}. "
        "Only if automation_mode=auto AND flywheel.synthesis_ready=true, "
        "queue a follow-up crystallization suggestion."
    )
    return a


def register_builtins(registry: type[ActionRegistry]) -> None:
    registry.register(
        ActionSpec(
            type="intent.set",
            payload=IntentSetPayload,
            hazard="write",
            description="Update intent policy fields.",
        )
    )
    registry.register(
        ActionSpec(
            type="intent.delete",
            payload=IntentDeletePayload,
            hazard="danger",
            description="Delete an intent policy entry.",
        )
    )
    registry.register(
        ActionSpec(
            type="notes.comment",
            payload=NotesCommentPayload,
            enrich=_enrich_notes_comment,
            hazard="write",
            description="Apply a natural-language NOTES edit instruction.",
        )
    )
    registry.register(
        ActionSpec(
            type="notes.edit",
            payload=NotesEditPayload,
            hazard="write",
            description="Apply explicit NOTES content rewrite.",
        )
    )
    registry.register(
        ActionSpec(
            type="core.tool-call",
            payload=ToolCallPayload,
            enrich=_enrich_tool_call,
            hazard="safe",
            description="Execute a deterministic MCP tool call from queue.",
        )
    )
    registry.register(
        ActionSpec(
            type="core.crystallize",
            payload=CoreCrystallizePayload,
            hazard="write",
            description="Crystallize a component to disk.",
        )
    )
    registry.register(
        ActionSpec(
            type="core.prompt",
            payload=CorePromptPayload,
            enrich=_enrich_core_prompt,
            hazard="write",
            description="Submit a free-form prompt to CC.",
        )
    )
