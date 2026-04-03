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
    for event in ("SessionStart", "UserPromptSubmit", "PostToolUse", "PreCompact"):
        assert event in hooks
        assert isinstance(hooks[event], list) and hooks[event]

    matcher = hooks["PostToolUse"][0]["matcher"]
    assert "mcp__plugin_.*emerge.*__icc_(read|write|exec)" == matcher
