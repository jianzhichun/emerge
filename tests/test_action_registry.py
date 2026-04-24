from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_builtin_action_types_registered() -> None:
    from scripts.admin.actions import ActionRegistry

    known = set(ActionRegistry.known_types())
    assert "intent.set" in known
    assert "intent.delete" in known
    assert "notes.comment" in known
    assert "notes.edit" in known
    assert "core.tool-call" in known
    assert "core.crystallize" in known
    assert "core.prompt" in known


def test_validate_rejects_unknown_and_accepts_known() -> None:
    from scripts.admin.actions import ActionRegistry

    assert ActionRegistry.validate({"type": "unknown.action"}) is not None
    assert ActionRegistry.validate({"type": "intent.delete", "key": "x"}) is None


def test_enrich_notes_comment_includes_notes_context(tmp_path: Path, monkeypatch) -> None:
    from scripts.admin.api import _enrich_actions

    connector_root = tmp_path / "connectors"
    notes_path = connector_root / "zwcad" / "NOTES.md"
    notes_path.parent.mkdir(parents=True)
    notes_path.write_text("# Notes\nhello", encoding="utf-8")
    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(connector_root))

    result = _enrich_actions(
        [{"type": "notes.comment", "connector": "zwcad", "comment": "update"}]
    )
    assert "instruction" in result[0]
    assert "current_notes" in result[0]
    assert "notes_path" in result[0]


def test_crystallize_to_yaml_action_is_registered():
    from scripts.admin.actions.registry import ActionRegistry
    from scripts.admin.actions.builtins import register_builtins
    register_builtins(ActionRegistry)
    spec = ActionRegistry.get("crystallize.to-yaml")
    assert spec is not None
    assert spec.hazard == "write"


def test_crystallize_to_yaml_enrich_injects_instruction():
    import json
    from scripts.admin.actions.registry import ActionRegistry, ActionContext
    from scripts.admin.actions.builtins import register_builtins
    from pathlib import Path

    register_builtins(ActionRegistry)
    spec = ActionRegistry.get("crystallize.to-yaml")
    ctx = ActionContext(connector_root=Path("/tmp"))

    action = {
        "type": "crystallize.to-yaml",
        "payload": {
            "intent_signature": "mock.write.multi-op",
            "span_id": "span-abc",
            "actions": [
                {
                    "tool_name": "mcp__plugin_emerge__icc_exec",
                    "args_snapshot": {"intent_signature": "mock.read.layers"},
                    "result_summary": {"rows_count": 3},
                },
                {
                    "tool_name": "mcp__plugin_emerge__icc_exec",
                    "args_snapshot": {"intent_signature": "mock.write.add-wall"},
                    "result_summary": {"ok": "true"},
                },
            ],
        },
    }
    payload_obj = spec.payload(**action["payload"])
    enriched = spec.enrich(action, payload_obj, ctx)
    assert "instruction" in enriched
    assert "mock.write.multi-op" in enriched["instruction"]
    assert "connector_call" in enriched["instruction"]


def test_adapter_can_register_new_action_type() -> None:
    from scripts.admin.actions.registry import ActionRegistry, ActionSpec

    @dataclass(frozen=True)
    class _Payload:
        value: str

    if ActionRegistry.get_spec("testadapter.echo") is None:
        ActionRegistry.register(
            ActionSpec(
                type="testadapter.echo",
                payload=_Payload,
                hazard="safe",
                description="echo test action",
            )
        )
    assert ActionRegistry.validate({"type": "testadapter.echo", "value": "ok"}) is None

