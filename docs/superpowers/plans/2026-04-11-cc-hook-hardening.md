# CC Hook Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix silent hook gaps — PreToolUse/PostToolUse validation never fires for span tools — and add normalization, rollback confirmation, and hook consolidation.

**Architecture:** Four independent improvements to `hooks/hooks.json`, `hooks/pre_tool_use.py`, `hooks/pre_compact.py`, and `.claude-plugin/plugin.json`. No daemon changes needed. All fixes are in the hook layer.

**Tech Stack:** Python 3.11+, CC hooks protocol, pytest 457-test baseline.

---

## Background

`hooks/hooks.json` is the primary hook registration file loaded by CC (separate from `plugin.json`). Its PreToolUse/PostToolUse matchers only cover the **deprecated** `icc_read/write` path plus `icc_exec/reconcile/crystallize`. The **primary span workflow** (`icc_span_open/close/approve`) has always been excluded — validation code exists in `pre_tool_use.py` but the hook never fires for those tools. Similarly, `plugin.json` accumulated `SessionEnd`, `Stop`, `SubagentStop` hooks that belong in `hooks/hooks.json`.

---

## File Map

| File | Change |
|------|--------|
| `hooks/hooks.json` | Expand PreToolUse/PostToolUse/PostToolUseFailure matchers to `icc_.*`; add SessionEnd/Stop/SubagentStop |
| `hooks/pre_tool_use.py` | Add intent_signature normalization (lowercase) with `updatedInput`; add `ask` confirmation for `icc_goal_rollback` |
| `hooks/pre_compact.py` | Add `pin_plugin_data_path_if_present()` before `default_hook_state_root()` |
| `.claude-plugin/plugin.json` | Remove SessionEnd, Stop, SubagentStop (moved to hooks.json) |
| `tests/test_hooks_pre_tool_use.py` | New tests for normalization and rollback confirmation |
| `tests/test_hooks_json_matchers.py` | New tests verifying hooks.json regex patterns |

---

### Task 1: Fix hooks.json hook matchers

**Files:**
- Modify: `hooks/hooks.json`
- Modify: `.claude-plugin/plugin.json`
- Create: `tests/test_hooks_json_matchers.py`

The PreToolUse, PostToolUse, and PostToolUseFailure matchers in `hooks/hooks.json` currently only match `icc_(read|write|exec|reconcile|crystallize)`. They must match all emerge tools so that validation in `pre_tool_use.py` fires for `icc_span_open`, `icc_span_close`, `icc_span_approve`, and future tools. The simplest fix is `icc_.*`. At the same time, consolidate `SessionEnd`, `Stop`, `SubagentStop` from `plugin.json` into `hooks/hooks.json` (where all other hooks live), and remove duplicates from `plugin.json`.

- [ ] **Step 1: Write failing test**

Create `tests/test_hooks_json_matchers.py`:

```python
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
    # Find the entry pointing to post_tool_use.py (not tool_audit.py)
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
    assert any("stop.py" in c for c in commands)


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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/apple/Documents/workspace/emerge
python -m pytest tests/test_hooks_json_matchers.py -v
```

Expected: Multiple FAIL — matchers too narrow, SessionEnd/Stop not in hooks.json.

- [ ] **Step 3: Update `hooks/hooks.json`**

Replace the entire content of `hooks/hooks.json` with:

```json
{
  "hooks": {
    "Setup": [
      {
        "matcher": ".*",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/setup.py"
          }
        ]
      }
    ],
    "SessionStart": [
      {
        "matcher": ".*",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/session_start.py"
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "matcher": ".*",
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
        "matcher": ".*",
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
        "matcher": ".*",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/stop.py",
            "timeout": 10
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "matcher": ".*",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/user_prompt_submit.py"
          }
        ]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "mcp__plugin_.*emerge.*__icc_.*",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/pre_tool_use.py"
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "mcp__plugin_.*emerge.*__icc_.*",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/post_tool_use.py"
          }
        ]
      },
      {
        "matcher": "^(?!mcp__plugin_.*emerge.*__icc_).*",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/tool_audit.py"
          }
        ]
      }
    ],
    "PostToolUseFailure": [
      {
        "matcher": "mcp__plugin_.*emerge.*__icc_.*",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/post_tool_use_failure.py"
          }
        ]
      }
    ],
    "PreCompact": [
      {
        "matcher": ".*",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/pre_compact.py"
          }
        ]
      }
    ]
  }
}
```

- [ ] **Step 4: Update `.claude-plugin/plugin.json` — remove SessionEnd, Stop, SubagentStop**

The `hooks` section of `plugin.json` should only keep `SessionStart` (runner_sync.py is separate from session_start.py — both must run). Remove `SessionEnd`, `Stop`, `SubagentStop` which are now in hooks.json:

```json
{
  "name": "emerge",
  "version": "0.3.47",
  "description": "Emerge — policy-driven crystallization flywheel for Claude Code: exec patterns promote to stable pipelines, PreToolUse enforcement, optional remote runner",
  "mcpServers": {
    "emerge": {
      "command": "python3",
      "args": [
        "${CLAUDE_PLUGIN_ROOT}/scripts/emerge_daemon.py"
      ]
    }
  },
  "permissions": {
    "filesystem": [
      "~/.emerge/"
    ],
    "network": [
      "localhost",
      "192.168.122.0/24"
    ]
  },
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
    ]
  }
}
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
python -m pytest tests/test_hooks_json_matchers.py -v
```

Expected: All 8 tests PASS.

- [ ] **Step 6: Run full suite**

```bash
python -m pytest tests -q
```

Expected: 457+ passed.

- [ ] **Step 7: Commit**

```bash
git add hooks/hooks.json .claude-plugin/plugin.json tests/test_hooks_json_matchers.py
git commit -m "fix: expand hook matchers to cover icc_.* — span tools now get PreToolUse enforcement"
```

---

### Task 2: intent_signature normalization via `updatedInput`

**Files:**
- Modify: `hooks/pre_tool_use.py:30–74` (icc_exec block), `:103–116` (icc_span_open block), `:125–128` (icc_span_approve block)
- Modify: `hooks/pre_tool_use.py:130–145` (output section)
- Modify: `tests/test_hooks_pre_tool_use.py`

When `intent_signature` has uppercase letters (e.g. `"ZWCAD.Read.State"`), the current code blocks with an error. Instead, normalize to lowercase and return `updatedInput` with the corrected value, allowing the tool call to proceed with the fixed argument. This only applies when the normalized value would be valid.

The logic:
1. Extract `intent_signature` — already `.strip()`ped in current code
2. After stripping, also `.lower()` it
3. Track whether normalization changed anything (`normalized = lowered != original`)
4. Run all existing validation on the **lowercased** value
5. If valid AND normalized: return allow + `updatedInput: {"intent_signature": lowercased}`
6. If valid AND not normalized: return normal allow
7. If invalid: return deny (same as before)

A new module-level variable `_normalized_sig: dict[str, str]` is not needed — just track per-tool whether a normalization was done. Use a simple boolean + the corrected value at the final output step.

- [ ] **Step 1: Write failing tests**

Add to `tests/test_hooks_pre_tool_use.py`:

```python
def test_intent_signature_uppercase_normalized():
    """Uppercase intent_signature is auto-normalized via updatedInput, not blocked."""
    payload = {
        "tool_name": "mcp__plugin_emerge_emerge__icc_exec",
        "tool_input": {
            "mode": "inline_code",
            "code": "x=1",
            "intent_signature": "ZWCAD.READ.State",
        },
    }
    out = _run_hook(payload)
    # Must NOT be a deny
    hook_out = out.get("hookSpecificOutput", {})
    assert hook_out.get("permissionDecision") != "deny", \
        f"Should normalize not block uppercase sig, got: {out}"
    # Must have updatedInput with normalized value
    assert "updatedInput" in hook_out, f"Expected updatedInput, got: {hook_out}"
    assert hook_out["updatedInput"]["intent_signature"] == "zwcad.read.state"


def test_intent_signature_mixed_case_normalized():
    """Mixed case intent_signature is auto-normalized for icc_span_open."""
    payload = {
        "tool_name": "mcp__plugin_emerge_emerge__icc_span_open",
        "tool_input": {"intent_signature": "Lark.Read.Get-Doc"},
    }
    out = _run_hook(payload)
    hook_out = out.get("hookSpecificOutput", {})
    assert hook_out.get("permissionDecision") != "deny"
    assert hook_out.get("updatedInput", {}).get("intent_signature") == "lark.read.get-doc"


def test_intent_signature_already_lowercase_no_updated_input():
    """Already-lowercase intent_signature must NOT produce updatedInput."""
    payload = {
        "tool_name": "mcp__plugin_emerge_emerge__icc_exec",
        "tool_input": {
            "mode": "inline_code",
            "code": "x=1",
            "intent_signature": "zwcad.read.state",
        },
    }
    out = _run_hook(payload)
    hook_out = out.get("hookSpecificOutput", {})
    assert hook_out.get("permissionDecision") != "deny"
    assert "updatedInput" not in hook_out, "No updatedInput needed when already correct"


def test_intent_signature_uppercase_invalid_structure_still_blocks():
    """Uppercased sig with wrong structure (e.g. 2 parts) still blocks."""
    payload = {
        "tool_name": "mcp__plugin_emerge_emerge__icc_exec",
        "tool_input": {
            "mode": "inline_code",
            "code": "x=1",
            "intent_signature": "ZWCAD.STATE",  # only 2 parts even after lowercase
        },
    }
    out = _run_hook(payload)
    hook_out = out.get("hookSpecificOutput", {})
    assert hook_out.get("permissionDecision") == "deny"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_hooks_pre_tool_use.py::test_intent_signature_uppercase_normalized tests/test_hooks_pre_tool_use.py::test_intent_signature_mixed_case_normalized tests/test_hooks_pre_tool_use.py::test_intent_signature_already_lowercase_no_updated_input tests/test_hooks_pre_tool_use.py::test_intent_signature_uppercase_invalid_structure_still_blocks -v
```

Expected: First 3 FAIL (currently blocks uppercase instead of normalizing), last one PASS.

- [ ] **Step 3: Refactor intent_signature extraction in `hooks/pre_tool_use.py`**

At the very top of `main()`, after extracting `tool_name` and `arguments`, add tracking variables for normalization:

```python
# Normalization tracking: if intent_signature is normalized to lowercase,
# we return updatedInput instead of blocking on case issues.
_sig_normalized_from: str | None = None  # original value if normalization occurred
_sig_normalized_to: str | None = None    # lowercased value
```

Then in the `icc_exec` block (around line 32), change:
```python
intent_signature = str(arguments.get("intent_signature", "")).strip()
```
to:
```python
_sig_raw = str(arguments.get("intent_signature", "")).strip()
intent_signature = _sig_raw.lower()
if intent_signature != _sig_raw:
    _sig_normalized_from = _sig_raw
    _sig_normalized_to = intent_signature
```

In the `icc_span_open` block (around line 105), change:
```python
intent_signature = str(arguments.get("intent_signature", "")).strip()
```
to:
```python
_sig_raw = str(arguments.get("intent_signature", "")).strip()
intent_signature = _sig_raw.lower()
if intent_signature != _sig_raw:
    _sig_normalized_from = _sig_raw
    _sig_normalized_to = intent_signature
```

In the `icc_span_approve` block (around line 126), change:
```python
intent_signature = str(arguments.get("intent_signature", "")).strip()
```
to:
```python
_sig_raw = str(arguments.get("intent_signature", "")).strip()
intent_signature = _sig_raw.lower()
if intent_signature != _sig_raw:
    _sig_normalized_from = _sig_raw
    _sig_normalized_to = intent_signature
```

In the `icc_crystallize` block (around line 85), change:
```python
intent_signature = str(arguments.get("intent_signature", "")).strip()
```
to:
```python
_sig_raw = str(arguments.get("intent_signature", "")).strip()
intent_signature = _sig_raw.lower()
if intent_signature != _sig_raw:
    _sig_normalized_from = _sig_raw
    _sig_normalized_to = intent_signature
```

- [ ] **Step 4: Update the output section in `hooks/pre_tool_use.py` to emit `updatedInput`**

Replace the final output block (lines 130–145):

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
elif _sig_normalized_to is not None:
    # Auto-fix: return normalized intent_signature via updatedInput
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": {"intent_signature": _sig_normalized_to},
        },
        "systemMessage": (
            f"pre_tool_use: normalized intent_signature "
            f"from {_sig_normalized_from!r} to {_sig_normalized_to!r}"
        ),
    }
else:
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": f"pre_tool_use: {tool_name} approved",
        }
    }
print(json.dumps(out))
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
python -m pytest tests/test_hooks_pre_tool_use.py -v
```

Expected: All PASS.

- [ ] **Step 6: Run full suite**

```bash
python -m pytest tests -q
```

Expected: 457+ passed.

- [ ] **Step 7: Commit**

```bash
git add hooks/pre_tool_use.py tests/test_hooks_pre_tool_use.py
git commit -m "feat: normalize intent_signature via updatedInput instead of blocking on case"
```

---

### Task 3: Rollback confirmation + pre_compact pin fix

**Files:**
- Modify: `hooks/pre_tool_use.py:125–128` (after icc_span_approve block)
- Modify: `hooks/pre_compact.py:25–27`
- Modify: `tests/test_hooks_pre_tool_use.py`

Two independent changes: (1) `icc_goal_rollback` returns `permissionDecision: "ask"` to prompt the user before an irreversible goal state change; (2) `pre_compact.py` is missing `pin_plugin_data_path_if_present()` which every other hook calls before `default_hook_state_root()`.

- [ ] **Step 1: Write failing tests**

Add to `tests/test_hooks_pre_tool_use.py`:

```python
def test_icc_goal_rollback_returns_ask():
    """icc_goal_rollback must return permissionDecision: ask for user confirmation."""
    payload = {
        "tool_name": "mcp__plugin_emerge_emerge__icc_goal_rollback",
        "tool_input": {"target_event_id": "evt-abc123", "actor": "claude"},
    }
    out = _run_hook(payload)
    hook_out = out.get("hookSpecificOutput", {})
    assert hook_out.get("hookEventName") == "PreToolUse"
    assert hook_out.get("permissionDecision") == "ask", \
        f"icc_goal_rollback must ask for confirmation, got: {out}"
    # systemMessage should explain what's being rolled back
    assert "systemMessage" in out
    assert "rollback" in out["systemMessage"].lower() or "evt-abc123" in out["systemMessage"]


def test_icc_goal_rollback_missing_target_blocks():
    """icc_goal_rollback without target_event_id should deny (schema enforcement)."""
    payload = {
        "tool_name": "mcp__plugin_emerge_emerge__icc_goal_rollback",
        "tool_input": {"actor": "claude"},
    }
    out = _run_hook(payload)
    hook_out = out.get("hookSpecificOutput", {})
    assert hook_out.get("permissionDecision") == "deny"
    assert "target_event_id" in hook_out.get("permissionDecisionReason", "").lower()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_hooks_pre_tool_use.py::test_icc_goal_rollback_returns_ask tests/test_hooks_pre_tool_use.py::test_icc_goal_rollback_missing_target_blocks -v
```

Expected: Both FAIL — no icc_goal_rollback handling in pre_tool_use.py.

- [ ] **Step 3: Add `icc_goal_rollback` handling to `hooks/pre_tool_use.py`**

After the `icc_span_approve` block (around line 128), add:

```python
    if tool_name.endswith("__icc_goal_rollback"):
        target_event_id = str(arguments.get("target_event_id", "")).strip()
        if not target_event_id:
            error_msg = "icc_goal_rollback: 'target_event_id' is required"
```

Then in the output section, after the `elif _sig_normalized_to is not None:` block, add a new condition before the final `else`. The full updated output section:

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
    elif tool_name.endswith("__icc_goal_rollback"):
        target_event_id = str(arguments.get("target_event_id", "")).strip()
        out = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
            },
            "systemMessage": (
                f"emerge: icc_goal_rollback to target_event_id={target_event_id!r}. "
                "This is irreversible — it will overwrite the active goal state. "
                "Confirm only if the user explicitly requested this rollback."
            ),
        }
    elif _sig_normalized_to is not None:
        out = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "updatedInput": {"intent_signature": _sig_normalized_to},
            },
            "systemMessage": (
                f"pre_tool_use: normalized intent_signature "
                f"from {_sig_normalized_from!r} to {_sig_normalized_to!r}"
            ),
        }
    else:
        out = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": f"pre_tool_use: {tool_name} approved",
            }
        }
    print(json.dumps(out))
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_hooks_pre_tool_use.py::test_icc_goal_rollback_returns_ask tests/test_hooks_pre_tool_use.py::test_icc_goal_rollback_missing_target_blocks -v
```

Expected: Both PASS.

- [ ] **Step 5: Fix `hooks/pre_compact.py` — add `pin_plugin_data_path_if_present()`**

In `hooks/pre_compact.py`, the current `main()` (line 18) uses `default_hook_state_root()` without first calling `pin_plugin_data_path_if_present()`. Add the pin call before the imports:

Change the imports section at the top of the file from:
```python
from scripts.goal_control_plane import GoalControlPlane  # noqa: E402
from scripts.policy_config import default_hook_state_root  # noqa: E402
from scripts.state_tracker import StateTracker, load_tracker, save_tracker  # noqa: E402
```

to:
```python
from scripts.goal_control_plane import GoalControlPlane  # noqa: E402
from scripts.policy_config import default_hook_state_root, pin_plugin_data_path_if_present  # noqa: E402
from scripts.state_tracker import StateTracker, load_tracker, save_tracker  # noqa: E402
```

Then at the start of `main()`, after `payload` is parsed, add `pin_plugin_data_path_if_present()` before `state_root = Path(default_hook_state_root())`:

```python
def main() -> None:
    payload_text = sys.stdin.read().strip()
    try:
        payload = json.loads(payload_text) if payload_text else {}
    except Exception:
        payload = {}

    pin_plugin_data_path_if_present()  # ADD THIS LINE
    state_root = Path(default_hook_state_root())
    state_path = state_root / "state.json"
    ...
```

- [ ] **Step 6: Verify pre_compact.py still importable**

```bash
cd /Users/apple/Documents/workspace/emerge
python3 -c "import hooks.pre_compact; print('ok')"
```

Expected: `ok`

- [ ] **Step 7: Run full suite**

```bash
python -m pytest tests -q
```

Expected: 457+ passed.

- [ ] **Step 8: Commit**

```bash
git add hooks/pre_tool_use.py hooks/pre_compact.py tests/test_hooks_pre_tool_use.py
git commit -m "feat: add goal rollback confirmation and fix pre_compact.py state path pin"
```

---

### Task 4: Update documentation

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update hooks.json Key Invariants in CLAUDE.md**

Find the `PreToolUse format` invariant line in CLAUDE.md Key Invariants. After the PreToolUse block, add:

```markdown
- **PreToolUse `updatedInput` normalization**: when `intent_signature` contains uppercase letters, `pre_tool_use.py` normalizes to lowercase and returns `updatedInput: {"intent_signature": lowercased}` with `permissionDecision: allow` instead of blocking. Only applied when the normalized value would be valid. Tracked via `_sig_normalized_from`/`_sig_normalized_to` in `main()`.
- **PreToolUse `ask` for `icc_goal_rollback`**: returns `permissionDecision: ask` with a `systemMessage` warning about irreversibility. Blocks calls missing `target_event_id` with `deny`. Requires hooks.json PreToolUse matcher to cover `icc_goal_rollback` (matcher: `icc_.*`).
- **hooks.json hook matchers**: `PreToolUse`, `PostToolUse`, `PostToolUseFailure` all use `mcp__plugin_.*emerge.*__icc_.*` to cover all current and future icc_ tools. `tool_audit.py` uses the inverse negative-lookahead. `SessionEnd`, `Stop`, `SubagentStop` are registered in `hooks/hooks.json` (matcher format). `plugin.json` only keeps `SessionStart → runner_sync.py` (runner sync runs separately from session_start.py).
```

- [ ] **Step 2: Update Documentation Update Rules table in CLAUDE.md**

Find the hooks row in the Documentation Update Rules table:
```
| Hook behavior change | README.md component table (Hooks row) + hook flow diagram |
```

Add a row:
```
| New hook matcher pattern or hooks.json entry | `CLAUDE.md` Key Invariants (hooks.json matchers line) |
```

- [ ] **Step 3: Run full suite one last time**

```bash
python -m pytest tests -q
```

Expected: 457+ passed.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document hook matcher patterns, updatedInput normalization, rollback ask"
```

---

## Self-Review

### Spec coverage

- C1 (PreToolUse matchers) → Task 1 ✅
- C2 (PostToolUse matchers) → Task 1 ✅
- H1 (updatedInput normalization) → Task 2 ✅
- M1 (ask for rollback) → Task 3 ✅
- M2 (pre_compact pin fix) → Task 3 ✅
- L1 (consolidate hooks) → Task 1 ✅
- Docs → Task 4 ✅

### Notes for implementer

- **Task 2 variable scope**: `_sig_normalized_from` and `_sig_normalized_to` are set inside each per-tool `if` block. They must be initialized to `None` BEFORE all the per-tool blocks (early in `main()`), otherwise Python will raise `UnboundLocalError` if a tool block is skipped.

- **Task 3 output section ordering**: The `elif tool_name.endswith("__icc_goal_rollback"):` check must come AFTER the `if error_msg:` check. If `target_event_id` is missing, `error_msg` is set and the deny path fires first — the `ask` branch is only reached when the call is structurally valid.

- **Task 1 regex**: `icc_.*` is a positive match. The negative-lookahead for `tool_audit.py` (`^(?!mcp__plugin_.*emerge.*__icc_).*`) does NOT need to change — it already correctly excludes all `icc_` tools regardless of the new span/goal tools.

- **Task 1 plugin.json**: After removing SessionEnd/Stop/SubagentStop from plugin.json, those hooks will only be registered via hooks.json. CC loads both files so nothing is lost — hooks.json is the authoritative registry going forward.
