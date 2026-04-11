"""Tests verifying hooks/hooks.json regex matchers are correct."""
from __future__ import annotations
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOKS_JSON = ROOT / "hooks" / "hooks.json"


def _load() -> dict:
    return json.loads(HOOKS_JSON.read_text(encoding="utf-8"))


def _matchers_for(event: str) -> list[str]:
    hooks = _load()["hooks"]
    return [entry["matcher"] for entry in hooks.get(event, [])]


def test_pre_tool_use_matches_span_tools():
    """PreToolUse must fire for icc_span_open, icc_span_close, icc_span_approve."""
    matchers = _matchers_for("PreToolUse")
    for tool in ("icc_span_open", "icc_span_close", "icc_span_approve"):
        tool_name = f"mcp__plugin_emerge_emerge__{tool}"
        matched = any(re.search(m, tool_name) for m in matchers)
        assert matched, f"PreToolUse does not match {tool_name!r}. Matchers: {matchers}"


def test_pre_tool_use_still_matches_legacy_tools():
    """PreToolUse must still fire for icc_exec, icc_reconcile, icc_crystallize."""
    matchers = _matchers_for("PreToolUse")
    for tool in ("icc_exec", "icc_reconcile", "icc_crystallize"):
        tool_name = f"mcp__plugin_emerge_emerge__{tool}"
        matched = any(re.search(m, tool_name) for m in matchers)
        assert matched, f"PreToolUse does not match {tool_name!r}. Matchers: {matchers}"


def test_post_tool_use_emerge_matches_span_tools():
    """PostToolUse post_tool_use.py entry must fire for icc_span_open/close/approve."""
    hooks = _load()["hooks"]
    post_hooks = hooks.get("PostToolUse", [])
    emerge_matchers = [
        e["matcher"]
        for e in post_hooks
        if any("post_tool_use.py" in h.get("command", "") for h in e.get("hooks", []))
    ]
    assert emerge_matchers, "No PostToolUse entry for post_tool_use.py found"
    for tool in ("icc_span_open", "icc_span_close", "icc_span_approve"):
        tool_name = f"mcp__plugin_emerge_emerge__{tool}"
        matched = any(re.search(m, tool_name) for m in emerge_matchers)
        assert matched, f"PostToolUse (post_tool_use.py) does not match {tool_name!r}"


def test_tool_audit_does_not_match_emerge_tools():
    """tool_audit.py must NOT fire for emerge icc_ tools."""
    hooks = _load()["hooks"]
    post_hooks = hooks.get("PostToolUse", [])
    audit_matchers = [
        e["matcher"]
        for e in post_hooks
        if any("tool_audit.py" in h.get("command", "") for h in e.get("hooks", []))
    ]
    assert audit_matchers, "No PostToolUse entry for tool_audit.py found"
    for tool in ("icc_exec", "icc_span_open", "icc_span_close"):
        tool_name = f"mcp__plugin_emerge_emerge__{tool}"
        matched = any(re.search(m, tool_name) for m in audit_matchers)
        assert not matched, f"tool_audit.py must not match emerge tool {tool_name!r}"

    # Positive half: standard CC tools MUST be matched by tool_audit.py
    for tool in ("Bash", "Read", "Write", "Grep"):
        matched = any(re.search(m, tool) for m in audit_matchers)
        assert matched, f"tool_audit.py must match non-emerge tool {tool!r}"


def test_hooks_json_has_session_end():
    """SessionEnd must be registered in hooks.json."""
    hooks = _load()["hooks"]
    assert "SessionEnd" in hooks, "SessionEnd missing from hooks.json"
    commands = [
        h.get("command", "")
        for e in hooks["SessionEnd"]
        for h in e.get("hooks", [])
    ]
    assert any("session_end.py" in c for c in commands), "SessionEnd must point to session_end.py"


def test_hooks_json_has_stop():
    """Stop must be registered in hooks.json."""
    hooks = _load()["hooks"]
    assert "Stop" in hooks, "Stop missing from hooks.json"
    commands = [
        h.get("command", "")
        for e in hooks["Stop"]
        for h in e.get("hooks", [])
    ]
    assert any("stop.py" in c for c in commands), "Stop must point to stop.py"


def test_hooks_json_has_subagent_stop():
    """SubagentStop must be registered in hooks.json and point to stop.py."""
    hooks = _load()["hooks"]
    assert "SubagentStop" in hooks, "SubagentStop missing from hooks.json"
    commands = [
        h.get("command", "")
        for e in hooks["SubagentStop"]
        for h in e.get("hooks", [])
    ]
    assert any("stop.py" in c for c in commands), "SubagentStop must point to stop.py"


def test_plugin_json_no_session_end():
    """SessionEnd should not be in plugin.json after consolidation."""
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert "SessionEnd" not in plugin.get("hooks", {}), \
        "SessionEnd should be moved to hooks.json, not in plugin.json"


def test_plugin_json_no_stop():
    """Stop/SubagentStop should not be in plugin.json after consolidation."""
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert "Stop" not in plugin.get("hooks", {}), \
        "Stop should be moved to hooks.json, not in plugin.json"
    assert "SubagentStop" not in plugin.get("hooks", {}), \
        "SubagentStop should be moved to hooks.json, not in plugin.json"
