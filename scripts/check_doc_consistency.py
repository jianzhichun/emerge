from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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


README_PATH = ROOT / "README.md"
HOOKS_JSON_PATH = ROOT / "hooks" / "hooks.json"
SKILL_PATH = ROOT / "skills" / "distilling-operator-flows" / "SKILL.md"


def _extract_backtick_tokens(text: str) -> set[str]:
    return set(re.findall(r"`([^`]+)`", text))


def _extract_readme_tools(text: str) -> set[str]:
    tools: set[str] = set()
    in_tools = False
    for line in text.splitlines():
        if line.strip() == "**Tools:**":
            in_tools = True
            continue
        if in_tools and line.startswith("**Resources:**"):
            break
        if not in_tools:
            continue
        m = re.match(r"^\|\s*`([^`]+)`\s*\|", line)
        if m:
            tools.add(m.group(1))
    return tools


def _extract_readme_hooks(text: str) -> set[str]:
    hooks_line = ""
    for line in text.splitlines():
        if line.startswith("**Hooks**"):
            hooks_line = line
            break
    if not hooks_line:
        return set()
    out = _extract_backtick_tokens(hooks_line)
    out.discard("hooks/hooks.json")
    return out


def _extract_hooks_json_events(text: str) -> set[str]:
    import json

    payload = json.loads(text)
    hooks = payload.get("hooks", {})
    if not isinstance(hooks, dict):
        return set()
    return set(hooks.keys())


def _skill_has_expected_thresholds(text: str) -> tuple[bool, list[str]]:
    expected = [
        f"attempts >= {PROMOTE_MIN_ATTEMPTS}",
        f"success_rate >= {PROMOTE_MIN_SUCCESS_RATE}",
        f"verify_rate >= {PROMOTE_MIN_VERIFY_RATE}",
        f"human_fix_rate <= {PROMOTE_MAX_HUMAN_FIX_RATE}",
        f"attempts >= {STABLE_MIN_ATTEMPTS}",
        f"success_rate >= {STABLE_MIN_SUCCESS_RATE}",
        f"verify_rate >= {STABLE_MIN_VERIFY_RATE}",
    ]
    missing = [token for token in expected if token not in text]
    return len(missing) == 0, missing


def main() -> int:
    readme_text = README_PATH.read_text(encoding="utf-8")
    hooks_text = HOOKS_JSON_PATH.read_text(encoding="utf-8")
    skill_text = SKILL_PATH.read_text(encoding="utf-8")

    failures: list[str] = []

    readme_tools = _extract_readme_tools(readme_text)
    schema_tools = {
        schema["name"] for schema in get_tool_schemas() if isinstance(schema, dict)
    }
    if readme_tools != schema_tools:
        only_readme = sorted(readme_tools - schema_tools)
        only_schema = sorted(schema_tools - readme_tools)
        failures.append(
            "README MCP tools table mismatch:\n"
            f"  only_in_readme={only_readme}\n"
            f"  only_in_schema={only_schema}"
        )

    readme_hooks = _extract_readme_hooks(readme_text)
    hooks_json_events = _extract_hooks_json_events(hooks_text)
    if readme_hooks != hooks_json_events:
        only_readme_hooks = sorted(readme_hooks - hooks_json_events)
        only_json_hooks = sorted(hooks_json_events - readme_hooks)
        failures.append(
            "README hooks list mismatch:\n"
            f"  only_in_readme={only_readme_hooks}\n"
            f"  only_in_hooks_json={only_json_hooks}"
        )

    ok_skill, missing_tokens = _skill_has_expected_thresholds(skill_text)
    if not ok_skill:
        failures.append(f"Skill thresholds out of sync; missing tokens: {missing_tokens}")

    if failures:
        print("Documentation consistency check failed:")
        for item in failures:
            print(f"- {item}")
        return 1

    print("Documentation consistency check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
