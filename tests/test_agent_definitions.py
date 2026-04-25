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


def test_load_watcher_profile_fail_gracefully(tmp_path):
    from scripts.watcher_profiles import load_watcher_profile

    root = tmp_path / "connectors"
    assert load_watcher_profile("missing", connector_root=root) is None

    bad = root / "bad"
    bad.mkdir(parents=True)
    (bad / "watcher_profile.yaml").write_text("not: [valid", encoding="utf-8")
    assert load_watcher_profile("bad", connector_root=root) is None

    invalid = root / "invalid"
    invalid.mkdir()
    (invalid / "watcher_profile.yaml").write_text("connector: invalid\nsources: []\n", encoding="utf-8")
    assert load_watcher_profile("invalid", connector_root=root) is None


def test_materialize_active_profiles_writes_valid_profiles(tmp_path):
    import json

    from scripts.watcher_profiles import materialize_active_profiles

    connector_root = tmp_path / "connectors"
    foo = connector_root / "foo"
    foo.mkdir(parents=True)
    (foo / "watcher_profile.yaml").write_text(
        """
connector: foo
sources:
  - type: file
    path: ~/foo/activity.log
parser:
  type: regex
  pattern: 'action=(?P<action>\\w+)'
intent_hints:
  - when:
      action: save
    intent: foo.write.save
redaction:
  drop_fields:
    - token
""".strip(),
        encoding="utf-8",
    )
    broken = connector_root / "broken"
    broken.mkdir()
    (broken / "watcher_profile.yaml").write_text("connector: nope\nsources: []\n", encoding="utf-8")

    out = materialize_active_profiles(tmp_path / "state", connector_root=connector_root)
    data = json.loads(out.read_text(encoding="utf-8"))

    assert sorted(data["profiles"]) == ["foo"]
    assert data["profiles"]["foo"]["intent_hints"][0]["intent"] == "foo.write.save"


def test_template_watcher_profile_is_valid():
    import yaml

    from scripts.watcher_profiles import validate_watcher_profile

    path = ROOT / "connectors" / "_template" / "watcher_profile.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    profile = validate_watcher_profile(raw)

    assert profile["connector"] == "_template"
    assert profile["intent_hints"]
