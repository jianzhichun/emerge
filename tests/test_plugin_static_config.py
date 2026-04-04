import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_plugin_manifest_exists_and_has_required_keys():
    manifest_path = ROOT / ".claude-plugin" / "plugin.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert data["name"] == "emerge"
    assert "version" in data


def test_mcp_config_has_core_stdio_and_expected_tools_path():
    mcp_path = ROOT / ".mcp.json"
    data = json.loads(mcp_path.read_text(encoding="utf-8"))
    assert "mcpServers" in data
    assert "core" in data["mcpServers"]
    server = data["mcpServers"]["core"]
    assert server["type"] == "stdio"
    assert "scripts/repl_daemon.py" in server["args"]


def test_hooks_json_has_required_events_and_post_tool_matcher():
    hooks_path = ROOT / "hooks" / "hooks.json"
    data = json.loads(hooks_path.read_text(encoding="utf-8"))
    hooks = data["hooks"]
    for event in ("Setup", "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure", "PreCompact"):
        assert event in hooks
        assert isinstance(hooks[event], list) and hooks[event]

    matcher = hooks["PostToolUse"][0]["matcher"]
    assert "mcp__plugin_.*emerge.*__icc_(read|write|exec|reconcile)" == matcher


def test_policy_command_uses_plugin_root_for_repl_admin():
    policy_md = (ROOT / "commands" / "policy.md").read_text(encoding="utf-8")
    assert "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" in policy_md
    assert "policy-status" in policy_md
    assert "python3 scripts/repl_admin.py" not in policy_md


def test_init_command_has_valid_description_frontmatter():
    init_md = (ROOT / "commands" / "init.md").read_text(encoding="utf-8")
    assert init_md.startswith("---\n")
    assert "\ndescription: " in init_md
    assert "## description:" not in init_md
    assert "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" in init_md
    assert "runner-status" in init_md
    assert "runner-bootstrap" in init_md
    assert "init_ok" in init_md and "degraded" in init_md and "blocked" in init_md


def test_runner_status_command_uses_plugin_root_for_repl_admin():
    runner_md = (ROOT / "commands" / "runner-status.md").read_text(encoding="utf-8")
    assert "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" in runner_md
    assert "runner-status" in runner_md
    assert "python3 scripts/repl_admin.py" not in runner_md
