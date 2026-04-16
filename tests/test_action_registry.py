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
    assert "pipeline.set" in known
    assert "pipeline.delete" in known
    assert "notes.comment" in known
    assert "notes.edit" in known
    assert "core.tool-call" in known
    assert "core.crystallize" in known
    assert "core.prompt" in known


def test_validate_rejects_unknown_and_accepts_known() -> None:
    from scripts.admin.actions import ActionRegistry

    assert ActionRegistry.validate({"type": "unknown.action"}) is not None
    assert ActionRegistry.validate({"type": "pipeline.delete", "key": "x"}) is None


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

