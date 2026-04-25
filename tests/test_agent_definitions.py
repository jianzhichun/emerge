from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_generic_operator_watcher_definition_is_connector_agnostic():
    agent_path = ROOT / "agents" / "operator-watcher.md"
    text = agent_path.read_text(encoding="utf-8")

    assert "name: operator-watcher" in text
    assert "watcher_profile.yaml" in text
    assert "icc_crystallize" not in text.split("---", 2)[1]
    assert "hm-watcher" not in text
    assert "zwcad-watcher" not in text


def test_watcher_profile_schema_accepts_connector_profiles():
    from scripts.watcher_profiles import validate_watcher_profile

    profile = {
        "connector": "hypermesh",
        "sources": [{"type": "file", "path": "~/HW_TEMP/command.tcl"}],
        "parser": {"type": "regex", "pattern": r"automesh (?P<density>\\w+)"},
        "intent_hints": [{"when": {"command": "automesh"}, "intent": "hypermesh.write.automesh"}],
        "preference_hints": [{"name": "density", "source": "density", "ttl_days": 30}],
        "redaction": {"drop_fields": ["license_key"]},
    }

    normalized = validate_watcher_profile(profile)

    assert normalized["connector"] == "hypermesh"
    assert normalized["sources"][0]["type"] == "file"
    assert normalized["intent_hints"][0]["intent"] == "hypermesh.write.automesh"


def test_watcher_profile_schema_rejects_bad_intent():
    from scripts.watcher_profiles import validate_watcher_profile

    try:
        validate_watcher_profile(
            {
                "connector": "hypermesh",
                "sources": [{"type": "file", "path": "command.tcl"}],
                "intent_hints": [{"intent": "bad"}],
            }
        )
        assert False, "invalid profile should fail"
    except ValueError as exc:
        assert "connector.mode.name" in str(exc)
