# CC MCP 2026 Feature Adoption Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Adopt MCP `2025-11-25` features and latest CC hook capabilities to make emerge's tool surface semantically rich, protocol-compliant, and resilient against session termination with open spans.

**Architecture:** Seven independent improvements to `emerge_daemon.py`, `hooks/pre_tool_use.py`, `hooks/post_tool_use.py`, `.claude-plugin/plugin.json`, and a new `hooks/stop.py`. Each task is fully self-contained with no ordering dependencies except Task 7 (docs), which comes last.

**Tech Stack:** Python 3.11+, MCP `2025-11-25` spec, CC hooks protocol (Stop/SubagentStop), pytest 441-test baseline.

---

## File Map

| File | Change |
|------|--------|
| `scripts/emerge_daemon.py:1384–1607` | Add `title` + `annotations` + `outputSchema` to all tool defs |
| `scripts/emerge_daemon.py:1351–1366` | MCP version negotiation (`min(client, "2025-11-25")`) |
| `hooks/pre_tool_use.py:130–132` | Migrate block output to `permissionDecision` + `systemMessage` |
| `hooks/post_tool_use.py:207–215` | Add `updatedMCPToolOutput` for icc_exec/icc_span_open flywheel metadata |
| `hooks/stop.py` | New file — span sentinel, blocks CC stop when span is open |
| `.claude-plugin/plugin.json:22–44` | Register `Stop` + `SubagentStop` hooks |
| `CLAUDE.md` + `README.md` | Update protocol version, hook docs |

---

### Task 1: Add `title` + `annotations` to all tool definitions

**Files:**
- Modify: `scripts/emerge_daemon.py:1384–1607`
- Test: `tests/test_mcp_tools_integration.py`

CC planner uses `annotations` to decide: `readOnlyHint: true` → no confirmation; `destructiveHint: true` → extra caution. `title` replaces machine name in CC tool picker display.

- [ ] **Step 1: Write failing test**

```python
# tests/test_mcp_tools_integration.py — add at end of file

def test_tool_list_has_title_and_annotations(daemon):
    """Every tool must declare title and annotations."""
    import json
    response = daemon.handle_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    tools = {t["name"]: t for t in response["result"]["tools"]}

    # title present on all tools
    for name, tool in tools.items():
        assert "title" in tool, f"{name} missing 'title'"

    # annotations present on all tools
    for name, tool in tools.items():
        assert "annotations" in tool, f"{name} missing 'annotations'"

    # spot-check specific annotations
    assert tools["icc_goal_read"]["annotations"]["readOnlyHint"] is True
    assert tools["icc_goal_rollback"]["annotations"]["destructiveHint"] is True
    assert tools["icc_reconcile"]["annotations"]["idempotentHint"] is True
    assert tools["icc_span_open"]["annotations"]["openWorldHint"] is False
    assert tools["icc_span_close"]["annotations"]["openWorldHint"] is False
    assert tools["icc_span_approve"]["annotations"]["openWorldHint"] is False
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/apple/Documents/workspace/emerge
python -m pytest tests/test_mcp_tools_integration.py::test_tool_list_has_title_and_annotations -v
```

Expected: FAIL with `KeyError` or `AssertionError` — `title`/`annotations` not present.

- [ ] **Step 3: Add `title` + `annotations` to each tool in `emerge_daemon.py:1384–1607`**

In `handle_jsonrpc`, find the `"tools": [...]` list (around line 1384). Add `"title"` and `"annotations"` to each tool dict. The full replacement for the 10 tools:

```python
# icc_span_open — line ~1386
{
    "name": "icc_span_open",
    "title": "Open Intent Span",
    "annotations": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "description": (
        "Open an intent span to track a multi-step MCP tool call sequence "
        "in the flywheel. Use before any sequence of Lark/context7/skill tool calls "
        "that represents a reusable intent. When the intent pipeline is stable, "
        "returns the pipeline result directly (bridge) with zero LLM overhead. "
        "Blocked if another span is already open."
    ),
    "inputSchema": { ... },  # unchanged
},
{
    "name": "icc_span_close",
    "title": "Close Intent Span",
    "annotations": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "description": ( ... ),  # unchanged
    "inputSchema": { ... },  # unchanged
},
{
    "name": "icc_span_approve",
    "title": "Approve Pipeline Skeleton",
    "annotations": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "description": ( ... ),  # unchanged
    "inputSchema": { ... },  # unchanged
},
{
    "name": "icc_exec",
    "title": "Execute Intent",
    "annotations": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "description": "...",  # unchanged
    "inputSchema": { ... },  # unchanged
},
{
    "name": "icc_goal_ingest",
    "title": "Submit Goal",
    "annotations": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    ...
},
{
    "name": "icc_goal_read",
    "title": "Read Goal",
    "annotations": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    ...
},
{
    "name": "icc_goal_rollback",
    "title": "Rollback Goal",
    "annotations": {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    ...
},
{
    "name": "icc_reconcile",
    "title": "Reconcile Delta",
    "annotations": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    ...
},
{
    "name": "icc_crystallize",
    "title": "Crystallize Pipeline",
    "annotations": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    ...
},
{
    "name": "icc_hub",
    "title": "Memory Hub",
    "annotations": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    ...
},
```

**Important:** only add the two new fields — `"title"` and `"annotations"` — to each existing dict. Do not change `"name"`, `"description"`, or `"inputSchema"`.

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_tool_list_has_title_and_annotations -v
```

Expected: PASS

- [ ] **Step 5: Run full suite baseline**

```bash
python -m pytest tests -q
```

Expected: 441 passed (same as before, no regressions)

- [ ] **Step 6: Commit**

```bash
git add scripts/emerge_daemon.py tests/test_mcp_tools_integration.py
git commit -m "feat: add MCP title and annotations to all emerge tools"
```

---

### Task 2: Add `outputSchema` to key tools

**Files:**
- Modify: `scripts/emerge_daemon.py:1386–1448` (icc_span_open, icc_span_close, icc_span_approve) and `1450–1468` (icc_exec)
- Test: `tests/test_mcp_tools_integration.py`

`outputSchema` lets CC know the exact shape of `structuredContent`, enabling typed access without text parsing.

- [ ] **Step 1: Write failing test**

```python
def test_tool_list_key_tools_have_output_schema(daemon):
    """icc_exec, icc_span_open, icc_span_close, icc_span_approve must declare outputSchema."""
    response = daemon.handle_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    tools = {t["name"]: t for t in response["result"]["tools"]}

    for name in ("icc_exec", "icc_span_open", "icc_span_close", "icc_span_approve"):
        assert "outputSchema" in tools[name], f"{name} missing 'outputSchema'"
        schema = tools[name]["outputSchema"]
        assert schema.get("type") == "object", f"{name} outputSchema must be object type"
        assert "properties" in schema, f"{name} outputSchema missing 'properties'"

    # spot-check specific fields
    exec_props = tools["icc_exec"]["outputSchema"]["properties"]
    assert "bridge_promoted" in exec_props
    assert "synthesis_ready" in exec_props
    assert "policy_status" in exec_props

    span_open_props = tools["icc_span_open"]["outputSchema"]["properties"]
    assert "span_id" in span_open_props
    assert "bridge" in span_open_props
    assert "policy_status" in span_open_props

    span_close_props = tools["icc_span_close"]["outputSchema"]["properties"]
    assert "span_id" in span_close_props
    assert "synthesis_ready" in span_close_props
    assert "skeleton_path" in span_close_props
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_tool_list_key_tools_have_output_schema -v
```

Expected: FAIL — `outputSchema` not present.

- [ ] **Step 3: Add `outputSchema` to icc_exec tool dict (around line 1450)**

```python
{
    "name": "icc_exec",
    "title": "Execute Intent",
    "annotations": { ... },  # from Task 1
    "description": "...",
    "outputSchema": {
        "type": "object",
        "properties": {
            "bridge_promoted": {
                "type": "boolean",
                "description": "True when flywheel bridge short-circuited to pipeline result",
            },
            "synthesis_ready": {
                "type": "boolean",
                "description": "True when enough execs have been recorded to crystallize a pipeline",
            },
            "policy_status": {
                "type": "string",
                "description": "Current flywheel policy status: explore | canary | stable",
            },
            "result": {
                "description": "Exec result payload (stdout, result_var extraction, or pipeline data)",
            },
            "error": {
                "type": "string",
                "description": "Error message if isError=true",
            },
        },
    },
    "inputSchema": { ... },
},
```

- [ ] **Step 4: Add `outputSchema` to icc_span_open (around line 1386)**

```python
"outputSchema": {
    "type": "object",
    "properties": {
        "span_id": {
            "type": "string",
            "description": "Unique identifier for this span — pass to icc_span_close",
        },
        "intent_signature": {"type": "string"},
        "status": {
            "type": "string",
            "description": "opened | bridge (when pipeline bridged directly)",
        },
        "policy_status": {
            "type": "string",
            "description": "explore | canary | stable",
        },
        "bridge": {
            "type": "boolean",
            "description": "True when span was bridged — no span_id returned, result is in 'result'",
        },
        "result": {
            "description": "Pipeline result when bridge=true",
        },
    },
},
```

- [ ] **Step 5: Add `outputSchema` to icc_span_close (around line 1410)**

```python
"outputSchema": {
    "type": "object",
    "properties": {
        "span_id": {"type": "string"},
        "intent_signature": {"type": "string"},
        "outcome": {
            "type": "string",
            "description": "success | failure | aborted",
        },
        "policy_status": {
            "type": "string",
            "description": "explore | canary | stable",
        },
        "synthesis_ready": {
            "type": "boolean",
            "description": "True when the pipeline skeleton is ready for icc_span_approve",
        },
        "is_read_only": {"type": "boolean"},
        "skeleton_path": {
            "type": "string",
            "description": "Path to generated _pending/<name>.py — present when synthesis_ready=true",
        },
        "next_step": {
            "type": "string",
            "description": "Human-readable action hint when skeleton is ready",
        },
    },
},
```

- [ ] **Step 6: Add `outputSchema` to icc_span_approve (around line 1431)**

```python
"outputSchema": {
    "type": "object",
    "properties": {
        "intent_signature": {"type": "string"},
        "pipeline_path": {
            "type": "string",
            "description": "Path to activated .py pipeline",
        },
        "yaml_path": {
            "type": "string",
            "description": "Path to generated .yaml metadata",
        },
        "activated": {
            "type": "boolean",
            "description": "True when bridge is now active",
        },
    },
},
```

- [ ] **Step 7: Run test to verify it passes**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_tool_list_key_tools_have_output_schema -v
```

Expected: PASS

- [ ] **Step 8: Run full suite**

```bash
python -m pytest tests -q
```

Expected: 441+ passed

- [ ] **Step 9: Commit**

```bash
git add scripts/emerge_daemon.py tests/test_mcp_tools_integration.py
git commit -m "feat: add outputSchema to icc_exec, icc_span_open/close/approve"
```

---

### Task 3: Migrate PreToolUse block output to `permissionDecision` format

**Files:**
- Modify: `hooks/pre_tool_use.py:130–132`
- Test: `tests/test_hooks.py` (or create if not present)

Old format `{"decision": "block", "reason": "..."}` merges user-facing and Claude-facing messages. New format splits them: `permissionDecisionReason` is shown to the user in CC UI; `systemMessage` is injected into Claude's context window.

- [ ] **Step 1: Find or create test file**

```bash
python -m pytest tests/ -q --collect-only 2>&1 | grep hook
```

- [ ] **Step 2: Write failing test**

Create `tests/test_hooks_pre_tool_use.py`:

```python
"""Tests for pre_tool_use.py hook output format."""
from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run_hook(payload: dict) -> dict:
    result = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "pre_tool_use.py")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_block_uses_permission_decision_format():
    """Blocking output must use permissionDecision, not legacy 'decision: block'."""
    payload = {
        "tool_name": "mcp__plugin_emerge_emerge__icc_exec",
        "tool_input": {"mode": "inline_code", "code": "x=1"},
        # missing intent_signature → triggers block
    }
    out = _run_hook(payload)
    # Must NOT use legacy format
    assert "decision" not in out, "legacy 'decision' key must not be used"
    # Must use new hookSpecificOutput format
    assert "hookSpecificOutput" in out
    hook_out = out["hookSpecificOutput"]
    assert hook_out.get("hookEventName") == "PreToolUse"
    assert hook_out.get("permissionDecision") == "deny"
    assert "permissionDecisionReason" in hook_out
    # systemMessage must be present and Claude-facing (different from reason)
    assert "systemMessage" in out
    assert "intent_signature" in out["systemMessage"].lower() or "blocked" in out["systemMessage"].lower()


def test_approve_format_unchanged():
    """Successful calls still use additionalContext format."""
    payload = {
        "tool_name": "mcp__plugin_emerge_emerge__icc_exec",
        "tool_input": {
            "mode": "inline_code",
            "code": "x=1",
            "intent_signature": "zwcad.read.state",
        },
    }
    out = _run_hook(payload)
    assert "hookSpecificOutput" in out
    hook_out = out["hookSpecificOutput"]
    assert hook_out.get("hookEventName") == "PreToolUse"
    # approve path keeps additionalContext
    assert "additionalContext" in hook_out or hook_out.get("permissionDecision") == "allow"


def test_icc_reconcile_block_uses_permission_decision():
    """Reconcile validation errors also use permissionDecision format."""
    payload = {
        "tool_name": "mcp__plugin_emerge_emerge__icc_reconcile",
        "tool_input": {"delta_id": "", "outcome": "confirm"},
    }
    out = _run_hook(payload)
    assert "decision" not in out
    hook_out = out["hookSpecificOutput"]
    assert hook_out.get("permissionDecision") == "deny"
    assert "delta_id" in hook_out["permissionDecisionReason"].lower()
```

- [ ] **Step 3: Run test to verify it fails**

```bash
python -m pytest tests/test_hooks_pre_tool_use.py -v
```

Expected: FAIL — `out` still has `"decision": "block"`.

- [ ] **Step 4: Update `hooks/pre_tool_use.py` lines 130–132**

Replace:
```python
if error_msg:
    # Return a block decision to reject the tool call
    out = {"decision": "block", "reason": error_msg}
```

With:
```python
if error_msg:
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": error_msg,
        },
        "systemMessage": f"Tool call blocked by emerge PreToolUse validator: {error_msg}",
    }
```

- [ ] **Step 5: Run test to verify it passes**

```bash
python -m pytest tests/test_hooks_pre_tool_use.py -v
```

Expected: PASS

- [ ] **Step 6: Run full suite**

```bash
python -m pytest tests -q
```

Expected: 441+ passed

- [ ] **Step 7: Commit**

```bash
git add hooks/pre_tool_use.py tests/test_hooks_pre_tool_use.py
git commit -m "fix: migrate PreToolUse block to permissionDecision format with systemMessage"
```

---

### Task 4: Stop/SubagentStop hook — span sentinel

**Files:**
- Create: `hooks/stop.py`
- Modify: `.claude-plugin/plugin.json:22–44`
- Test: `tests/test_hooks_stop.py`

When CC is about to stop, if an emerge span is open, block with a clear reason so CC calls `icc_span_close` first. This prevents the silent span leak that currently only gets cleaned up by SessionEnd/SessionStart.

- [ ] **Step 1: Write failing test**

Create `tests/test_hooks_stop.py`:

```python
"""Tests for stop.py span sentinel hook."""
from __future__ import annotations
import json
import subprocess
import sys
import tempfile
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STOP_HOOK = ROOT / "hooks" / "stop.py"


def _run_stop_hook(state: dict | None = None, env_override: dict | None = None) -> dict:
    """Run stop.py with a temporary state.json."""
    with tempfile.TemporaryDirectory() as tmpdir:
        env = {**os.environ}
        env["EMERGE_DATA_ROOT"] = tmpdir
        if env_override:
            env.update(env_override)
        if state is not None:
            state_path = Path(tmpdir) / "state.json"
            state_path.write_text(json.dumps(state), encoding="utf-8")
        payload = {"hook_event_name": "Stop", "session_id": "test-session"}
        result = subprocess.run(
            [sys.executable, str(STOP_HOOK)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, f"stop.py exited non-zero: {result.stderr}"
        return json.loads(result.stdout) if result.stdout.strip() else {}


def test_stop_blocks_when_span_open():
    """Stop hook must block when active_span_id is in state.json."""
    state = {
        "active_span_id": "span-abc123",
        "active_span_intent": "zwcad.read.state",
    }
    out = _run_stop_hook(state=state)
    assert out.get("decision") == "block", f"Expected block, got: {out}"
    assert "zwcad.read.state" in out.get("reason", "")
    assert "icc_span_close" in out.get("reason", "")


def test_stop_allows_when_no_span():
    """Stop hook must not block when no active span."""
    out = _run_stop_hook(state={"active_span_id": None})
    assert out.get("decision") != "block"


def test_stop_allows_when_no_state_file():
    """Stop hook must not block when state.json doesn't exist."""
    out = _run_stop_hook(state=None)
    assert out.get("decision") != "block"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_hooks_stop.py -v
```

Expected: FAIL — `hooks/stop.py` does not exist.

- [ ] **Step 3: Create `hooks/stop.py`**

```python
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> None:
    payload_text = sys.stdin.read().strip()
    try:
        payload = json.loads(payload_text) if payload_text else {}
    except Exception:
        payload = {}

    # Locate state.json — respect EMERGE_DATA_ROOT override for tests
    import os
    data_root_env = os.environ.get("EMERGE_DATA_ROOT", "")
    if data_root_env:
        state_path = Path(data_root_env) / "state.json"
    else:
        try:
            from scripts.policy_config import default_hook_state_root, pin_plugin_data_path_if_present
            pin_plugin_data_path_if_present()
            state_path = Path(default_hook_state_root()) / "state.json"
        except Exception:
            state_path = Path.home() / ".emerge" / "state.json"

    active_span_id: str = ""
    active_span_intent: str = ""
    try:
        if state_path.exists():
            raw = json.loads(state_path.read_text(encoding="utf-8"))
            active_span_id = str(raw.get("active_span_id") or "")
            active_span_intent = str(raw.get("active_span_intent") or "")
    except Exception:
        pass

    if active_span_id:
        sig = active_span_intent or active_span_id
        out = {
            "decision": "block",
            "reason": (
                f"emerge: active span for '{sig}' is still open. "
                "Call icc_span_close(outcome='aborted') before stopping, "
                "or the flywheel WAL will have an incomplete record."
            ),
        }
    else:
        out = {
            "hookSpecificOutput": {
                "hookEventName": "Stop",
                "additionalContext": "emerge: no active span — safe to stop",
            }
        }
    print(json.dumps(out))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_hooks_stop.py -v
```

Expected: PASS

- [ ] **Step 5: Register `Stop` + `SubagentStop` in `.claude-plugin/plugin.json`**

Current `hooks` section (lines 22–44):
```json
"hooks": {
    "SessionStart": [...],
    "SessionEnd": [...]
}
```

New `hooks` section:
```json
"hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 ${CLAUDE_PLUGIN_ROOT}/scripts/runner_sync.py"
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/session_end.py",
            "timeout": 10
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/stop.py",
            "timeout": 10
          }
        ]
      }
    ],
    "SubagentStop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/stop.py",
            "timeout": 10
          }
        ]
      }
    ]
  }
```

- [ ] **Step 6: Run full suite**

```bash
python -m pytest tests -q
```

Expected: 441+ passed

- [ ] **Step 7: Commit**

```bash
git add hooks/stop.py .claude-plugin/plugin.json tests/test_hooks_stop.py
git commit -m "feat: add Stop/SubagentStop hook as span sentinel"
```

---

### Task 5: MCP version negotiation

**Files:**
- Modify: `scripts/emerge_daemon.py:1351–1366`
- Test: `tests/test_mcp_tools_integration.py`

Server must respond with `min(client_version, "2025-11-25")` rather than hardcoding `"2025-03-26"`. This unlocks `title`, `annotations`, `outputSchema` on newer CC clients that negotiate `2025-11-25`.

Version comparison: date-based strings — compare by parsing `YYYY-MM-DD`.

- [ ] **Step 1: Write failing test**

```python
def test_initialize_version_negotiation_new_client(daemon):
    """When client sends 2025-11-25, server must respond 2025-11-25."""
    response = daemon.handle_jsonrpc({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "claude-code", "version": "1.0"},
        },
    })
    assert response["result"]["protocolVersion"] == "2025-11-25"


def test_initialize_version_negotiation_old_client(daemon):
    """When client sends 2025-03-26, server responds 2025-03-26 (min)."""
    response = daemon.handle_jsonrpc({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "claude-code", "version": "0.9"},
        },
    })
    assert response["result"]["protocolVersion"] == "2025-03-26"


def test_initialize_version_negotiation_no_version(daemon):
    """When client sends no version, default to 2025-03-26 for compatibility."""
    response = daemon.handle_jsonrpc({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"capabilities": {}},
    })
    assert response["result"]["protocolVersion"] == "2025-03-26"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_initialize_version_negotiation_new_client tests/test_mcp_tools_integration.py::test_initialize_version_negotiation_old_client tests/test_mcp_tools_integration.py::test_initialize_version_negotiation_no_version -v
```

Expected: FAIL — hardcoded `"2025-03-26"` is always returned.

- [ ] **Step 3: Add `_SERVER_MAX_PROTOCOL_VERSION` constant and update `initialize` handler**

At the top of `EmergeDaemon` class (or just above `handle_jsonrpc`), add:

```python
_SERVER_MAX_PROTOCOL_VERSION = "2025-11-25"
```

Then in `handle_jsonrpc`, replace lines 1351–1366:

```python
if method == "initialize":
    client_version = str(params.get("protocolVersion", "") or "").strip()
    # Version negotiation: respond with min(client, server_max).
    # Versions are date-based (YYYY-MM-DD) — compare lexicographically.
    _server_max = self._SERVER_MAX_PROTOCOL_VERSION
    if client_version and client_version <= _server_max:
        negotiated_version = client_version
    elif client_version and client_version > _server_max:
        negotiated_version = _server_max
    else:
        negotiated_version = "2025-03-26"  # fallback for clients that omit version
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "protocolVersion": negotiated_version,
            "capabilities": {
                "tools": {},
                "resources": {"subscribe": True},
                "prompts": {},
                "logging": {},
                "elicitation": {},
            },
            "serverInfo": {"name": "emerge", "version": self._version},
        },
    }
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_initialize_version_negotiation_new_client tests/test_mcp_tools_integration.py::test_initialize_version_negotiation_old_client tests/test_mcp_tools_integration.py::test_initialize_version_negotiation_no_version -v
```

Expected: PASS

- [ ] **Step 5: Run full suite**

```bash
python -m pytest tests -q
```

Expected: 441+ passed

- [ ] **Step 6: Commit**

```bash
git add scripts/emerge_daemon.py tests/test_mcp_tools_integration.py
git commit -m "feat: MCP version negotiation — server responds min(client, 2025-11-25)"
```

---

### Task 6: PostToolUse `updatedMCPToolOutput` for icc_exec span context injection

**Files:**
- Modify: `hooks/post_tool_use.py:207–215`
- Test: `tests/test_hooks_post_tool_use.py`

When icc_exec runs inside an active span, PostToolUse can inject `span_id` and `active_span_intent` into the structured result via `updatedMCPToolOutput`. This means CC always knows which flywheel span context an exec happened in — without reading `state.json` separately.

Only enrich for `icc_exec` tool calls where `active_span_id` is present. For all other tools, leave output unchanged (no `updatedMCPToolOutput` key).

- [ ] **Step 1: Write failing test**

Create `tests/test_hooks_post_tool_use.py`:

```python
"""Tests for post_tool_use.py updatedMCPToolOutput injection."""
from __future__ import annotations
import json
import subprocess
import sys
import tempfile
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POST_HOOK = ROOT / "hooks" / "post_tool_use.py"


def _run_post_hook(payload: dict, state: dict | None = None) -> dict:
    with tempfile.TemporaryDirectory() as tmpdir:
        env = {**os.environ, "EMERGE_DATA_ROOT": tmpdir}
        if state is not None:
            (Path(tmpdir) / "state.json").write_text(json.dumps(state), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(POST_HOOK)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, f"post_tool_use.py stderr: {result.stderr}"
        return json.loads(result.stdout) if result.stdout.strip() else {}


def test_icc_exec_with_active_span_injects_span_context():
    """When icc_exec runs inside an active span, hook injects span_id into updatedMCPToolOutput."""
    state = {
        "active_span_id": "span-abc",
        "active_span_intent": "zwcad.read.state",
    }
    tool_result = {
        "isError": False,
        "content": [{"type": "text", "text": '{"result": [{"layers": 3}]}'}],
        "structuredContent": {"result": [{"layers": 3}]},
    }
    payload = {
        "tool_name": "mcp__plugin_emerge_emerge__icc_exec",
        "tool_input": {"intent_signature": "zwcad.read.state", "code": "x=1"},
        "tool_response": tool_result,
    }
    out = _run_post_hook(payload, state=state)
    assert "hookSpecificOutput" in out
    hook_out = out["hookSpecificOutput"]
    # When span is active, updatedMCPToolOutput must be present
    assert "updatedMCPToolOutput" in hook_out, "Missing updatedMCPToolOutput for icc_exec with active span"
    updated = hook_out["updatedMCPToolOutput"]
    sc = updated.get("structuredContent", {})
    assert sc.get("_span_id") == "span-abc"
    assert sc.get("_span_intent") == "zwcad.read.state"


def test_icc_exec_without_active_span_no_injection():
    """When no span is active, icc_exec hook must NOT add updatedMCPToolOutput."""
    payload = {
        "tool_name": "mcp__plugin_emerge_emerge__icc_exec",
        "tool_input": {"intent_signature": "zwcad.read.state", "code": "x=1"},
        "tool_response": {"isError": False, "content": [{"type": "text", "text": "ok"}]},
    }
    out = _run_post_hook(payload, state={"active_span_id": None})
    hook_out = out.get("hookSpecificOutput", {})
    assert "updatedMCPToolOutput" not in hook_out


def test_non_exec_tool_never_injects():
    """icc_span_close and other tools must never emit updatedMCPToolOutput."""
    state = {"active_span_id": "span-xyz", "active_span_intent": "foo.read.bar"}
    payload = {
        "tool_name": "mcp__plugin_emerge_emerge__icc_span_close",
        "tool_input": {"outcome": "success"},
        "tool_response": {"isError": False, "content": [{"type": "text", "text": "ok"}]},
    }
    out = _run_post_hook(payload, state=state)
    hook_out = out.get("hookSpecificOutput", {})
    assert "updatedMCPToolOutput" not in hook_out
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_hooks_post_tool_use.py::test_icc_exec_with_active_span_injects_span_context -v
```

Expected: FAIL — `updatedMCPToolOutput` not in hook output.

- [ ] **Step 3: Check where `_active_span_id` is read in `post_tool_use.py`**

Around line 86–92, the hook reads `active_span_id` from `state.json`. It's already available as `_active_span_id`. Now replace the output section at lines 207–215:

```python
# Build hookSpecificOutput — inject span context into icc_exec output when span is active
hook_specific: dict[str, Any] = {
    "hookEventName": "PostToolUse",
    "additionalContext": context_text,
}

# For icc_exec with an active span: inject _span_id/_span_intent into structuredContent
# so CC can correlate the exec result with the flywheel span without a separate state read.
_short = _short_tool_name(payload.get("tool_name", ""))
if _short == "icc_exec" and _active_span_id:
    _tool_resp = payload.get("tool_response") or {}
    _sc = dict(_tool_resp.get("structuredContent") or {})
    _sc["_span_id"] = _active_span_id
    _sc["_span_intent"] = _active_span_intent
    _updated_resp = dict(_tool_resp)
    _updated_resp["structuredContent"] = _sc
    hook_specific["updatedMCPToolOutput"] = _updated_resp

output = {
    "hookSpecificOutput": hook_specific,
}
print(json.dumps(output))
```

**Important:** `_active_span_id` and `_active_span_intent` are already set earlier in `main()` from `state.json` (lines 86–92). The variable `payload` is the full hook payload dict — make sure it's accessible at this point (it's the `payload` from the top of `main()`).

Also add `from typing import Any` import if not already present (check existing imports at top of file).

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_hooks_post_tool_use.py -v
```

Expected: PASS

- [ ] **Step 5: Verify post_tool_use reads `tool_response` from payload**

Check that the hook actually has `payload.get("tool_response")` accessible at that line. If the variable isn't `payload` at that scope, use whatever variable is set in `main()` holding the full hook input dict.

Run:
```bash
python -m pytest tests/test_hooks_post_tool_use.py tests/test_mcp_tools_integration.py -q
```

Expected: all pass

- [ ] **Step 6: Run full suite**

```bash
python -m pytest tests -q
```

Expected: 441+ passed

- [ ] **Step 7: Commit**

```bash
git add hooks/post_tool_use.py tests/test_hooks_post_tool_use.py
git commit -m "feat: inject span context into icc_exec output via updatedMCPToolOutput"
```

---

### Task 7: Update documentation

**Files:**
- Modify: `CLAUDE.md`
- Modify: `README.md`

- [ ] **Step 1: Update `CLAUDE.md` Architecture section**

In the `## Architecture` section, find the line about MCP protocol version. Update:

```markdown
**MCP protocol version**: daemon negotiates with client — responds `min(client_version, "2025-11-25")`. Fallback: `"2025-03-26"` when client omits version.
```

Also update the Key Invariants section — find the existing protocol invariant:
> `**MCP protocol version**: daemon advertises `2025-03-26` with `elicitation: {}` capability.`

Change to:
```markdown
**MCP protocol version**: daemon negotiates version — responds `min(client_version, "2025-11-25")`. Server max is `_SERVER_MAX_PROTOCOL_VERSION = "2025-11-25"`. Tools include `title`, `annotations`, and `outputSchema` per MCP 2025-11-25 spec. `_elicit()` must only be called from worker threads (ThreadPoolExecutor), never from the main stdin loop.
```

- [ ] **Step 2: Update `CLAUDE.md` Hooks section**

Find the hook registration note (near the plugin.json reference) and add:

```markdown
- **Stop + SubagentStop hooks** (`hooks/stop.py`): blocks CC stop if `active_span_id` is present in state.json. Returns `{"decision": "block", "reason": "...call icc_span_close first"}`.
- **PreToolUse** (`hooks/pre_tool_use.py`): uses `permissionDecision: deny` + `systemMessage` format (MCP 2025-11-25) for blocks. Legacy `{"decision": "block"}` format removed.
- **PostToolUse** (`hooks/post_tool_use.py`): injects `_span_id`/`_span_intent` into `updatedMCPToolOutput.structuredContent` when icc_exec runs inside an active span.
```

- [ ] **Step 3: Update Documentation Update Rules table in CLAUDE.md**

The existing table has:
```
| Hook behavior change | README.md component table (Hooks row) + hook flow diagram |
```

Add a row:
```
| MCP server_max_version bump | CLAUDE.md Key Invariants (protocol version line) + README.md if present |
```

- [ ] **Step 4: Update README.md if it has a protocol version reference**

```bash
grep -n "2025-03-26\|protocolVersion\|MCP.*version\|protocol.*version" README.md | head -20
```

If found, update to reference `2025-11-25` negotiation. If not found, skip.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md README.md
git commit -m "docs: update protocol version, hook docs for MCP 2025-11-25 adoption"
```

---

## Self-Review

### Spec coverage

- P1 Tool annotations → Task 1 ✅
- P2 Stop hook → Task 4 ✅
- P3 Tool title → Task 1 (combined with annotations) ✅
- P4 outputSchema → Task 2 ✅
- P5 PreToolUse format → Task 3 ✅
- P6 MCP version negotiation → Task 5 ✅
- P7 PostToolUse updatedMCPToolOutput → Task 6 ✅
- Docs update → Task 7 ✅

### Notes for implementer

- **Task 3** (`permissionDecision`): The `_active_span_id` variable in `post_tool_use.py` is set at line ~89 from `_record_span_action`. Make sure it's accessible at the output section. If not (different scope), read it directly from the payload's tool_response or re-read from `state_path`.
- **Task 6** (`updatedMCPToolOutput`): `post_tool_use.py` uses `EMERGE_DATA_ROOT` env var only for tests. In production the hook uses `default_hook_state_root()`. The test fixture sets `EMERGE_DATA_ROOT` — but the hook's `main()` doesn't read that env var. You'll need to add that env var check to `post_tool_use.py` for the tests to work (similar to how `stop.py` does it). Alternatively, mock the state path differently. The easiest path: add `EMERGE_DATA_ROOT` env override to `post_tool_use.py` the same way `stop.py` does it, and use it to build `state_path` for testing.
- **Task 4 test**: `_run_stop_hook` sets `EMERGE_DATA_ROOT` to control state path. `stop.py` must read this env var when present. The implementation in Step 3 already handles this.
- **All tasks**: run `python -m pytest tests -q` after every task to catch regressions early.
