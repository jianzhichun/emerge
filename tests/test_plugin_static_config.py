import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_plugin_manifest_exists_and_has_required_keys():
    manifest_path = ROOT / ".claude-plugin" / "plugin.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert data["name"] == "emerge"
    assert "version" in data
    # mcpServers tells CC how to start the daemon (required for plugin mode)
    assert "mcpServers" in data, "plugin.json must declare mcpServers so CC can start the daemon"
    server = data["mcpServers"].get("emerge", {})
    assert server.get("command") == "python3"
    assert any("emerge_daemon.py" in arg for arg in server.get("args", []))
    # Must use ${CLAUDE_PLUGIN_ROOT} so path resolves correctly regardless of CWD
    assert any("${CLAUDE_PLUGIN_ROOT}" in arg for arg in server.get("args", []))



def test_hooks_json_has_required_events_and_post_tool_matcher():
    hooks_path = ROOT / "hooks" / "hooks.json"
    data = json.loads(hooks_path.read_text(encoding="utf-8"))
    hooks = data["hooks"]
    for event in ("Setup", "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure", "PreCompact"):
        assert event in hooks
        assert isinstance(hooks[event], list) and hooks[event]

    # PostToolUse fires for all tools (".*") so general CC tool calls
    # (Bash, Read, Grep, etc.) are recorded in tool-events.jsonl for audit.
    matcher = hooks["PostToolUse"][0]["matcher"]
    assert matcher == ".*"


def test_hooks_json_commands_use_claude_plugin_root():
    """All hook commands must use ${CLAUDE_PLUGIN_ROOT} so they work regardless of CWD.

    CC substitutes ${CLAUDE_PLUGIN_ROOT} before spawning (hooks.ts:845).
    Without it, hooks fail when CC is run from a project other than the plugin root.
    """
    hooks_path = ROOT / "hooks" / "hooks.json"
    data = json.loads(hooks_path.read_text(encoding="utf-8"))
    for event, matchers in data["hooks"].items():
        for entry in matchers:
            for hook in entry.get("hooks", []):
                cmd = hook.get("command", "")
                if not cmd:
                    continue
                assert "${CLAUDE_PLUGIN_ROOT}" in cmd, (
                    f"Hook {event!r} command {cmd!r} must use ${{CLAUDE_PLUGIN_ROOT}} "
                    "to work when CC is run from outside the plugin directory"
                )


def test_cockpit_command_uses_plugin_root_for_repl_admin():
    cockpit_md = (ROOT / "commands" / "cockpit.md").read_text(encoding="utf-8")
    assert "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" in cockpit_md
    assert "policy-status" in cockpit_md


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


def test_marketplace_json_has_valid_structure():
    marketplace_path = ROOT / ".claude-plugin" / "marketplace.json"
    data = json.loads(marketplace_path.read_text(encoding="utf-8"))
    # Required top-level fields
    assert "name" in data
    assert " " not in data["name"], "marketplace name must not contain spaces"
    assert "owner" in data and "name" in data["owner"]
    assert "plugins" in data and data["plugins"]
    # Plugin entry for "emerge"
    plugin = next((p for p in data["plugins"] if p["name"] == "emerge"), None)
    assert plugin is not None, "marketplace.json must contain an 'emerge' plugin entry"
    assert "source" in plugin
    # Self-hosted relative path: must start with "./" so CC treats it as local
    source = plugin["source"]
    assert isinstance(source, str) and source.startswith("./"), (
        "emerge plugin source must be a relative path starting with './' "
        f"(got {source!r})"
    )
