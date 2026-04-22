from __future__ import annotations

import json
from pathlib import Path

from scripts.mcp.schemas import get_tool_schemas
from scripts.policy_config import (
    PROMOTE_MAX_HUMAN_FIX_RATE,
    PROMOTE_MIN_ATTEMPTS,
    PROMOTE_MIN_SUCCESS_RATE,
    PROMOTE_MIN_VERIFY_RATE,
    STABLE_MIN_ATTEMPTS,
    STABLE_MIN_SUCCESS_RATE,
    STABLE_MIN_VERIFY_RATE,
)


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


def test_distilling_skill_threshold_tokens_match_policy_constants():
    skill = (ROOT / "skills" / "distilling-operator-flows" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    expected_tokens = [
        f"attempts >= {PROMOTE_MIN_ATTEMPTS}",
        f"success_rate >= {PROMOTE_MIN_SUCCESS_RATE}",
        f"verify_rate >= {PROMOTE_MIN_VERIFY_RATE}",
        f"human_fix_rate <= {PROMOTE_MAX_HUMAN_FIX_RATE}",
        f"attempts >= {STABLE_MIN_ATTEMPTS}",
        f"success_rate >= {STABLE_MIN_SUCCESS_RATE}",
        f"verify_rate >= {STABLE_MIN_VERIFY_RATE}",
    ]
    for token in expected_tokens:
        assert token in skill
