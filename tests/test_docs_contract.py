from __future__ import annotations

import json
from pathlib import Path

from scripts.mcp.schemas import get_tool_schemas
ROOT = Path(__file__).resolve().parents[1]


def _readme_tools_set(text: str) -> set[str]:
    out: set[str] = set()
    in_tools = False
    for line in text.splitlines():
        if line.strip() == "**Tools:**":
            in_tools = True
            continue
        if in_tools and line.startswith("**Resources:**"):
            break
        if not in_tools:
            continue
        line = line.strip()
        if not line.startswith("| `"):
            continue
        chunks = line.split("`")
        if len(chunks) >= 2:
            out.add(chunks[1])
    return out


def _readme_hooks_set(text: str) -> set[str]:
    for line in text.splitlines():
        if line.startswith("**Hooks**"):
            chunks = line.split("`")
            out = {chunks[i] for i in range(1, len(chunks), 2)}
            out.discard("hooks/hooks.json")
            return out
    return set()


def test_readme_tools_match_public_mcp_schemas():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    readme_tools = _readme_tools_set(readme)
    schema_tools = {
        item["name"]
        for item in get_tool_schemas()
        if isinstance(item, dict)
    }
    assert readme_tools == schema_tools


def test_readme_hooks_match_hooks_json_events():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    readme_hooks = _readme_hooks_set(readme)
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    assert readme_hooks == set(hooks.keys())


def test_distilling_skill_keeps_policy_thresholds_out_of_generic_workflow():
    skill = (ROOT / "skills" / "distilling-operator-flows" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    forbidden_tokens = ["attempts >=", "success_rate >=", "verify_rate >=", "human_fix_rate <="]
    for token in forbidden_tokens:
        assert token not in skill


def test_contract_docs_do_not_reference_deleted_synthesis_runtime():
    active_docs = [
        ROOT / "README.md",
        ROOT / "CLAUDE.md",
        ROOT / "docs" / "architecture.md",
    ]
    forbidden_tokens = [
        "SynthesisAgent",
        "scripts/synthesis_agent.py",
        "scripts/synthesis_coordinator.py",
        "icc_synthesis_submit",
    ]
    for path in active_docs:
        text = path.read_text(encoding="utf-8")
        for token in forbidden_tokens:
            assert token not in text, f"{path.relative_to(ROOT)} still references {token}"


def test_readme_lifecycle_diagram_matches_policy_thresholds():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "attempts >= 5, success >= 90, verify >= 98" in readme
    assert "attempts >= 15, success >= 95, verify >= 99" in readme
    assert "attempts >= 20, success >= 95" not in readme
    assert "attempts >= 40, success >= 97" not in readme
