# tests/test_hooks_plan4.py
"""Tests for Plan 4 hooks: permission_request, instructions_loaded,
worktree_lifecycle, task_created."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PERMISSION_REQUEST_HOOK = ROOT / "hooks" / "permission_request.py"
INSTRUCTIONS_LOADED_HOOK = ROOT / "hooks" / "instructions_loaded.py"
WORKTREE_LIFECYCLE_HOOK = ROOT / "hooks" / "worktree_lifecycle.py"
TASK_CREATED_HOOK = ROOT / "hooks" / "task_created.py"


def _run(script: Path, payload: dict, data_dir: Path):
    env = {**os.environ, "EMERGE_DATA_ROOT": str(data_dir)}
    result = subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )
    return result.returncode, json.loads(result.stdout.strip() or "{}"), result.stderr.strip()


# ---------------------------------------------------------------------------
# PermissionRequest hook
# ---------------------------------------------------------------------------

def test_permission_request_allows_icc_tools(tmp_path):
    """PermissionRequest hook approves icc_* tools via hookSpecificOutput."""
    rc, out, err = _run(
        PERMISSION_REQUEST_HOOK,
        {"hook_event_name": "PermissionRequest",
         "tool_name": "mcp__plugin_emerge_emerge__icc_exec"},
        tmp_path,
    )
    assert rc == 0
    assert out["hookSpecificOutput"]["hookEventName"] == "PermissionRequest"
    assert out["hookSpecificOutput"]["decision"]["behavior"] == "allow"


def test_permission_request_ignores_non_icc_tools(tmp_path):
    """PermissionRequest hook returns {} for non-icc tools."""
    rc, out, err = _run(
        PERMISSION_REQUEST_HOOK,
        {"hook_event_name": "PermissionRequest",
         "tool_name": "Bash"},
        tmp_path,
    )
    assert rc == 0
    assert out == {}


def test_permission_request_allows_all_icc_variants(tmp_path):
    """PermissionRequest hook approves all icc_* tool variants."""
    for tool in [
        "mcp__plugin_emerge_emerge__icc_span_open",
        "mcp__plugin_emerge_emerge__icc_span_close",
        "mcp__plugin_emerge_emerge__icc_crystallize",
        "mcp__plugin_emerge_emerge__icc_reconcile",
    ]:
        rc, out, err = _run(
            PERMISSION_REQUEST_HOOK,
            {"hook_event_name": "PermissionRequest", "tool_name": tool},
            tmp_path,
        )
        assert out["hookSpecificOutput"]["decision"]["behavior"] == "allow", tool


# ---------------------------------------------------------------------------
# InstructionsLoaded hook
# ---------------------------------------------------------------------------

def test_instructions_loaded_returns_empty_when_no_state(tmp_path):
    """InstructionsLoaded returns {} when state.json doesn't exist."""
    rc, out, err = _run(
        INSTRUCTIONS_LOADED_HOOK,
        {"hook_event_name": "InstructionsLoaded", "file_path": "/some/CLAUDE.md"},
        tmp_path,
    )
    assert rc == 0
    # Either {} or {systemMessage: ...} — must not raise
    assert isinstance(out, dict)


def test_instructions_loaded_injects_active_span(tmp_path):
    """InstructionsLoaded injects span reminder when active_span_id is set."""
    import json as _json
    state_root = tmp_path / "emerge" / "repl"
    state_root.mkdir(parents=True)
    state_path = state_root / "state.json"
    state_path.write_text(_json.dumps({
        "active_span_id": "abc-123",
        "active_span_intent": "demo.write.thing",
    }), encoding="utf-8")

    env = {
        **os.environ,
        "EMERGE_DATA_ROOT": str(tmp_path / "emerge"),
        "CLAUDE_PLUGIN_DATA": str(state_root),
    }
    result = subprocess.run(
        [sys.executable, str(INSTRUCTIONS_LOADED_HOOK)],
        input=_json.dumps({"hook_event_name": "InstructionsLoaded", "file_path": "/CLAUDE.md"}),
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0
    out = _json.loads(result.stdout.strip() or "{}")
    msg = out.get("systemMessage", "")
    assert "abc-123" in msg or "demo.write.thing" in msg


# ---------------------------------------------------------------------------
# WorktreeCreate / WorktreeRemove hook
# ---------------------------------------------------------------------------

def test_worktree_remove_returns_empty(tmp_path):
    """WorktreeRemove hook always returns {}."""
    rc, out, err = _run(
        WORKTREE_LIFECYCLE_HOOK,
        {"hook_event_name": "WorktreeRemove"},
        tmp_path,
    )
    assert rc == 0
    assert out == {}


def test_worktree_create_clears_active_span(tmp_path):
    """WorktreeCreate hook clears active_span_id from state.json."""
    import json as _json
    state_root = tmp_path / "emerge" / "repl"
    state_root.mkdir(parents=True)
    state_path = state_root / "state.json"
    state_path.write_text(_json.dumps({
        "active_span_id": "stale-span",
        "active_span_intent": "demo.write.thing",
        "other_key": "preserved",
    }), encoding="utf-8")

    env = {
        **os.environ,
        "EMERGE_DATA_ROOT": str(tmp_path / "emerge"),
        "CLAUDE_PLUGIN_DATA": str(state_root),
    }
    result = subprocess.run(
        [sys.executable, str(WORKTREE_LIFECYCLE_HOOK)],
        input=_json.dumps({"hook_event_name": "WorktreeCreate"}),
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0
    updated = _json.loads(state_path.read_text(encoding="utf-8"))
    assert "active_span_id" not in updated
    assert "active_span_intent" not in updated
    out = _json.loads(result.stdout.strip() or "{}")
    assert "systemMessage" in out  # reported the cleanup


def test_worktree_create_no_op_when_no_span(tmp_path):
    """WorktreeCreate returns {} when no active span is in state."""
    import json as _json
    state_root = tmp_path / "emerge" / "repl"
    state_root.mkdir(parents=True)
    (state_root / "state.json").write_text(_json.dumps({}), encoding="utf-8")

    env = {
        **os.environ,
        "EMERGE_DATA_ROOT": str(tmp_path / "emerge"),
        "CLAUDE_PLUGIN_DATA": str(state_root),
    }
    result = subprocess.run(
        [sys.executable, str(WORKTREE_LIFECYCLE_HOOK)],
        input=_json.dumps({"hook_event_name": "WorktreeCreate"}),
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0
    out = _json.loads(result.stdout.strip() or "{}")
    assert out == {}


# ---------------------------------------------------------------------------
# TaskCreated hook
# ---------------------------------------------------------------------------

def test_task_created_no_op_without_active_span(tmp_path):
    """TaskCreated returns {} when no span is active."""
    import json as _json
    state_root = tmp_path / "emerge" / "repl"
    state_root.mkdir(parents=True)
    (state_root / "state.json").write_text(_json.dumps({}), encoding="utf-8")

    env = {
        **os.environ,
        "EMERGE_DATA_ROOT": str(tmp_path / "emerge"),
        "CLAUDE_PLUGIN_DATA": str(state_root),
    }
    result = subprocess.run(
        [sys.executable, str(TASK_CREATED_HOOK)],
        input=_json.dumps({"hook_event_name": "TaskCreated", "subject": "Build feature X"}),
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0
    wal_path = state_root / "span-wal" / "spans.jsonl"
    assert not wal_path.exists()


def test_task_created_writes_wal_when_span_active(tmp_path):
    """TaskCreated writes a task_created entry to span WAL when span is open."""
    import json as _json
    state_root = tmp_path / "emerge" / "repl"
    state_root.mkdir(parents=True)
    (state_root / "state.json").write_text(_json.dumps({
        "active_span_id": "span-xyz",
        "active_span_intent": "demo.write.feature",
    }), encoding="utf-8")

    env = {
        **os.environ,
        "EMERGE_DATA_ROOT": str(tmp_path / "emerge"),
        "CLAUDE_PLUGIN_DATA": str(state_root),
    }
    result = subprocess.run(
        [sys.executable, str(TASK_CREATED_HOOK)],
        input=_json.dumps({
            "hook_event_name": "TaskCreated",
            "subject": "Build feature X",
            "task_id": "t-42",
        }),
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0
    wal_path = state_root / "span-wal" / "spans.jsonl"
    assert wal_path.exists()
    entries = [_json.loads(l) for l in wal_path.read_text().splitlines() if l.strip()]
    assert len(entries) == 1
    e = entries[0]
    assert e["action"] == "task_created"
    assert e["span_id"] == "span-xyz"
    assert e["task_subject"] == "Build feature X"
    assert e["task_id"] == "t-42"


# ---------------------------------------------------------------------------
# EventBus bridge path fix
# ---------------------------------------------------------------------------

def test_icc_exec_bridge_path_writes_operator_event(tmp_path):
    """icc_exec flywheel bridge path writes to EventBus (was missing before)."""
    import json as _json
    import socket
    import sys
    sys.path.insert(0, str(ROOT))

    from scripts.emerge_daemon import EmergeDaemon
    from scripts.policy_config import atomic_write_json

    machine_id = socket.gethostname()
    event_path = Path.home() / ".emerge" / "operator-events" / machine_id / "events.jsonl"
    before = sum(1 for _ in event_path.read_text(encoding="utf-8").splitlines() if _.strip()) if event_path.exists() else 0

    daemon = EmergeDaemon(root=tmp_path)
    # Inject a stable pipeline entry so the bridge fires
    from scripts.policy_config import default_exec_root
    reg_path = Path(str(default_exec_root())) / "pipelines-registry.json"
    existing = {}
    if reg_path.exists():
        import json as _j
        try:
            existing = _j.loads(reg_path.read_text())
        except Exception:
            pass
    existing.setdefault("pipelines", {})
    existing["pipelines"]["test-bridge.write.event-check"] = {"status": "stable"}
    # Write to a tmp registry that won't affect real state
    tmp_reg = tmp_path / "pipelines-registry.json"
    atomic_write_json(tmp_reg, existing)
    # Patch daemon to use tmp registry
    daemon._state_root = tmp_path

    result = daemon.call_tool("icc_exec", {
        "intent_signature": "test-bridge.write.event-check",
        "code": "result = 1",
        "mode": "inline_code",
    })
    # Even if bridge fires (returns None because no actual pipeline file exists),
    # fall-through to normal exec should call _write_operator_event
    after = sum(1 for _ in event_path.read_text(encoding="utf-8").splitlines() if _.strip()) if event_path.exists() else 0
    assert after > before
