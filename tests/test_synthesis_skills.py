from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read_skill(name: str) -> str:
    return (ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")


def _frontmatter(text: str) -> dict[str, str]:
    assert text.startswith("---\n")
    end = text.index("\n---", 4)
    fields: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip()
    return fields


def test_forward_synthesis_skill_declares_required_contract():
    text = _read_skill("emerge-forward-synthesis")

    assert "name: emerge-forward-synthesis" in text
    assert "forward_synthesis_pending" in text
    assert "icc_exec" in text
    assert "conservative" in text.lower() or "保守" in text
    assert "__args" in text
    assert "required_fields" in text
    for field in ("connector", "mode", "pipeline_name", "code", "confidence", "rationale"):
        assert field in text


def test_reverse_synthesis_skill_declares_required_contract():
    text = _read_skill("emerge-reverse-synthesis")

    assert "name: emerge-reverse-synthesis" in text
    assert "synthesis_job_ready" in text
    assert "icc_exec" in text
    assert "raw operator events" in text
    assert "__args" in text
    for field in ("connector", "mode", "pipeline_name", "code", "confidence", "rationale"):
        assert field in text


def test_forward_prompt_rationale_documents_product_rules():
    text = (ROOT / "docs" / "synthesis" / "forward_prompt_rationale.md").read_text(encoding="utf-8")

    assert "保守优先" in text
    assert "few-shot" in text
    assert "verify" in text


def test_product_skills_have_frontmatter_and_trigger_descriptions():
    required = [
        "emerge-forward-synthesis",
        "emerge-reverse-synthesis",
        "cockpit-rendering",
        "runner-elicitation-policy",
        "admin-runner-operations",
    ]

    for name in required:
        text = _read_skill(name)
        meta = _frontmatter(text)
        assert meta["name"] == name
        assert len(meta.get("description", "")) > 40


def test_admin_commands_exist_with_clear_invocation_contracts():
    for name in ("admin-batch-update-runners", "diagnose-stuck-flywheel"):
        text = (ROOT / "commands" / f"{name}.md").read_text(encoding="utf-8")
        meta = _frontmatter(text)
        assert meta.get("description")
        assert "icc_" in text or "scripts/" in text


def test_distiller_and_connector_watcher_template_are_markdown_only():
    agent_names = ["forward-distiller", "operator-watcher", "connector-watcher-template"]
    forbidden_tools = {"Write", "Edit", "ApplyPatch"}
    forbidden_runtime_calls = ("icc_crystallize", "icc_exec", "IntentRegistry")

    for name in agent_names:
        text = (ROOT / "agents" / f"{name}.md").read_text(encoding="utf-8")
        meta = _frontmatter(text)
        assert meta["name"] == name
        tools = {tool.strip() for tool in meta.get("tools", "").split(",")}
        assert tools.isdisjoint(forbidden_tools)
        for token in forbidden_runtime_calls:
            assert token not in text


def test_product_does_not_ship_vertical_specific_watcher_agents():
    forbidden_vertical_agents = {"hm", "zwcad", "solidworks", "catia", "focus6"}
    shipped_agents = {p.stem for p in (ROOT / "agents").glob("*.md")}

    assert shipped_agents.isdisjoint(forbidden_vertical_agents)


def test_product_markdown_assets_are_generic_not_vertical_specific():
    forbidden_terms = (
        "HyperMesh",
        "hypermesh",
        "ZWCAD",
        "zwcad",
        "SolidWorks",
        "solidworks",
        "CATIA",
        "catia",
        "FOCUS6",
        "focus6",
    )
    markdown_roots = (ROOT / "skills", ROOT / "agents", ROOT / "commands")
    offenders: list[str] = []

    for markdown_root in markdown_roots:
        for path in markdown_root.rglob("*.md"):
            text = path.read_text(encoding="utf-8")
            for term in forbidden_terms:
                if term in text:
                    offenders.append(f"{path.relative_to(ROOT)} contains {term}")

    assert offenders == []


def test_v3_generic_workflow_skills_exist_with_precise_triggers():
    expected_triggers = {
        "distill-from-pattern": "pattern_pending_synthesis",
        "crystallize-from-wal": "synthesis_ready",
        "aggregate-suggestions": "pattern_aggregated",
        "judge-promote-flywheel": "evidence_applied",
    }

    for name, trigger in expected_triggers.items():
        text = _read_skill(name)
        meta = _frontmatter(text)
        assert meta["name"] == name
        assert trigger in meta.get("description", "")
        assert len(meta.get("description", "")) > 60


def test_distill_from_pattern_documents_observation_to_synthesis_handoff():
    text = _read_skill("distill-from-pattern")
    assert "pattern_observed" in text
    assert "local_pattern_observed" in text
    assert "pattern_aggregated" in text
    assert "scripts/synthesis_events.py" in text
    assert "pattern_pending_synthesis" in text
    assert "synthesis_job_ready" in text
    assert "Do not reintroduce a Python coordinator" in text
