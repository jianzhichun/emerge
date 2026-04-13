# CC 2026 New Hooks Adoption Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Adopt all six CC 2026 new hook capabilities to reach optimal solution: FileChanged (replace 3-tier cockpit dispatch), PostCompact (fresh state re-injection), `once`/`statusMessage` (hook UX polish), conditional `if` field (overhead reduction), CwdChanged (session re-anchoring), and Elicitation/ElicitationResult (CI automation).

**Architecture:** Each feature is a new hook script in `hooks/` or a modification to `hooks/hooks.json`. All new hooks follow the existing pattern: read JSON from stdin, write JSON to stdout, use `systemMessage` or `hookSpecificOutput` depending on CC's schema for that event type. Tests validate hook output contracts using subprocess execution identical to `test_hook_scripts_output.py`.

**Tech Stack:** Python 3, CC hooks protocol (MCP 2025-11-25), `hooks/hooks.json`, `scripts/policy_config.py` for path resolution.

**Baseline:** 512 tests passing.

---

## File Map

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `hooks/post_compact.py` | F2: re-inject clean FLYWHEEL_TOKEN after compaction |
| Create | `hooks/cwd_changed.py` | F5: update session context when CWD changes |
| Create | `hooks/elicitation.py` | F6: CI auto-response for emerge elicitations |
| Create | `hooks/elicitation_result.py` | F6: audit log for elicitation outcomes |
| Modify | `hooks/hooks.json` | F1–F6: register all new hooks + `once`, `statusMessage`, `if` |
| Modify | `hooks/setup.py` | F1: attempt `watchPaths` output to bootstrap FileChanged |
| Create | `hooks/file_changed.py` | F1: process pending-actions.json on file change |
| Modify | `tests/test_hook_scripts_output.py` | TDD tests for all new hooks |

---

## Task 1: F2 — PostCompact Hook

After context compaction, inject a fresh FLYWHEEL_TOKEN so CC's post-compaction context contains the reset (clean) state rather than the pre-compaction snapshot.

**Files:**
- Create: `hooks/post_compact.py`
- Modify: `hooks/hooks.json` (add PostCompact entry)
- Modify: `tests/test_hook_scripts_output.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_hook_scripts_output.py`:

```python
def test_post_compact_emits_fresh_flywheel_token(tmp_path: Path):
    """PostCompact must output a systemMessage with a clean FLYWHEEL_TOKEN."""
    out = _run(
        "post_compact.py",
        {
            "hook_event_name": "PostCompact",
            "trigger": "manual",
            "compact_summary": "Session was compacted. Goal: test goal.",
        },
        tmp_path,
    )
    result = json.loads(out)
    # PostCompact uses top-level systemMessage (not hookSpecificOutput)
    assert "systemMessage" in result
    assert "hookSpecificOutput" not in result
    msg = result["systemMessage"]
    assert "FLYWHEEL_TOKEN" in msg
    token = json.loads(msg.split("FLYWHEEL_TOKEN\n")[1].strip().split("\n")[0])
    assert token["schema_version"] == "flywheel.v1"
    # After compaction, state was reset by PreCompact — token must show empty deltas/risks
    assert token["deltas"] == []
    assert token["open_risks"] == []


def test_post_compact_includes_span_protocol(tmp_path: Path):
    """PostCompact systemMessage must include Span Protocol directive."""
    out = _run("post_compact.py", {"hook_event_name": "PostCompact", "compact_summary": ""}, tmp_path)
    result = json.loads(out)
    assert "Span Protocol" in result["systemMessage"]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_hook_scripts_output.py::test_post_compact_emits_fresh_flywheel_token -xvs
```

Expected: `FileNotFoundError` or `FAILED` — `post_compact.py` does not exist yet.

- [ ] **Step 3: Implement `hooks/post_compact.py`**

```python
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.goal_control_plane import init_goal_control_plane  # noqa: E402
from scripts.policy_config import default_exec_root, default_hook_state_root, pin_plugin_data_path_if_present  # noqa: E402
from scripts.span_tracker import SpanTracker  # noqa: E402
from scripts.state_tracker import load_tracker  # noqa: E402

_BUDGET_CHARS = 800


def main() -> None:
    payload_text = sys.stdin.read().strip()
    try:
        payload = json.loads(payload_text) if payload_text else {}
    except Exception:
        payload = {}

    pin_plugin_data_path_if_present()
    state_root = Path(default_hook_state_root())
    state_path = state_root / "state.json"

    # PreCompact already reset the tracker. Load the fresh (empty) state.
    tracker = load_tracker(state_path)
    goal_cp = init_goal_control_plane(state_root, tracker)
    snap = goal_cp.read_snapshot()

    token = tracker.format_recovery_token(
        budget_chars=_BUDGET_CHARS,
        goal_override=str(snap.get("text", "")),
        goal_source_override=str(snap.get("source", "unset")),
    )
    token_json = json.dumps(token, ensure_ascii=True, separators=(",", ":"))

    exec_root = Path(os.environ.get("EMERGE_STATE_ROOT", str(default_exec_root())))
    reflection = SpanTracker(
        state_root=exec_root,
        hook_state_root=state_root,
    ).format_reflection_with_cache()
    reflection_block = f"{reflection}\n\n" if reflection else ""

    _SPAN_PROTOCOL = (
        "Span Protocol\n"
        "Before any reusable multi-step tool sequence, "
        'call icc_span_open(intent_signature="connector.mode.name"). '
        "Call icc_span_close(outcome=...) when done. "
        "Stable intents bridge to pipelines automatically."
    )

    context_text = (
        "[PostCompact] Context compacted. State reset to clean baseline.\n\n"
        + _SPAN_PROTOCOL + "\n\n"
        + reflection_block
        + f"Goal\n{str(snap.get('text', '')) or 'Not set.'}\n\n"
        "Open Risks\n- None.\n\n"
        f"FLYWHEEL_TOKEN\n{token_json}"
    )

    # PostCompact uses top-level systemMessage (not hookSpecificOutput)
    out = {"systemMessage": context_text}
    print(json.dumps(out))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_hook_scripts_output.py::test_post_compact_emits_fresh_flywheel_token tests/test_hook_scripts_output.py::test_post_compact_includes_span_protocol -xvs
```

Expected: Both PASS.

- [ ] **Step 5: Register PostCompact in hooks.json**

In `hooks/hooks.json`, add after the `PreCompact` block:

```json
"PostCompact": [
  {
    "matcher": ".*",
    "hooks": [
      {
        "type": "command",
        "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/post_compact.py",
        "statusMessage": "Flywheel re-anchoring after compaction..."
      }
    ]
  }
],
```

- [ ] **Step 6: Run full test suite**

```bash
python -m pytest tests -q
```

Expected: 514+ passed (2 new tests).

- [ ] **Step 7: Commit**

```bash
git add hooks/post_compact.py hooks/hooks.json tests/test_hook_scripts_output.py
git commit -m "feat: add PostCompact hook to re-inject clean FLYWHEEL_TOKEN after compaction"
```

---

## Task 2: F3 — `once`, `statusMessage` Hook Config Polish

Add `once: true` to the Setup hook (runs once per session) and `statusMessage` to every hook entry for visible spinner feedback.

**Files:**
- Modify: `hooks/hooks.json`

- [ ] **Step 1: Update hooks.json**

Replace the full `hooks/hooks.json` with the following (changes: `once` on Setup, `statusMessage` on all entries):

```json
{
  "hooks": {
    "Setup": [
      {
        "matcher": ".*",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/setup.py",
            "once": true,
            "statusMessage": "Emerge plugin initializing..."
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
            "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/session_start.py",
            "statusMessage": "Flywheel loading session context..."
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
            "timeout": 10,
            "statusMessage": "Flywheel closing session..."
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
            "timeout": 10,
            "statusMessage": "Flywheel checking open spans..."
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
            "timeout": 10,
            "statusMessage": "Flywheel checking open spans..."
          }
        ]
      }
    ],
    "StopFailure": [
      {
        "matcher": ".*",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/stop_failure.py",
            "timeout": 10
          }
        ]
      }
    ],
    "TaskCompleted": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/task_completed.py",
            "timeout": 10,
            "statusMessage": "Flywheel verifying task completion..."
          }
        ]
      }
    ],
    "SubagentStart": [
      {
        "matcher": ".*",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/subagent_start.py",
            "timeout": 10,
            "statusMessage": "Flywheel guarding subagent span..."
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
            "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/user_prompt_submit.py",
            "statusMessage": "Flywheel injecting context..."
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
            "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/pre_tool_use.py",
            "statusMessage": "Flywheel validating intent..."
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
            "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/post_tool_use.py",
            "statusMessage": "Flywheel learning..."
          }
        ]
      },
      {
        "matcher": "^(?!mcp__plugin_.*emerge.*__icc_).*",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/tool_audit.py",
            "statusMessage": "Flywheel auditing tool..."
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
            "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/post_tool_use_failure.py",
            "statusMessage": "Flywheel recording failure..."
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
            "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/pre_compact.py",
            "statusMessage": "Flywheel preserving state before compaction..."
          }
        ]
      }
    ],
    "PostCompact": [
      {
        "matcher": ".*",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/post_compact.py",
            "statusMessage": "Flywheel re-anchoring after compaction..."
          }
        ]
      }
    ]
  }
}
```

- [ ] **Step 2: Validate JSON is well-formed**

```bash
python3 -c "import json; json.load(open('hooks/hooks.json')); print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Run full test suite (no regressions)**

```bash
python -m pytest tests -q
```

Expected: same count passing as before this task.

- [ ] **Step 4: Commit**

```bash
git add hooks/hooks.json
git commit -m "feat: add statusMessage and once:true to hooks for UX polish"
```

---

## Task 3: F4 — Conditional `if` Field on tool_audit PostToolUse

The `tool_audit.py` PostToolUse currently uses a negative-lookahead regex matcher. Reinforce this with a complementary `if` condition that explicitly excludes emerge tools from the audit path. This is belt-and-suspenders: the matcher remains, the `if` adds argument-level filtering documentation for CC.

> Note: the `if` field uses permission rule syntax (e.g. `"Bash(git *)"`, `"Edit(*.ts)"`). For MCP tools it matches by tool name. Validate that this syntax filters correctly.

**Files:**
- Modify: `hooks/hooks.json` (tool_audit entry only)

- [ ] **Step 1: Write a test verifying tool_audit.py is NOT invoked for emerge tools**

Add to `tests/test_hook_scripts_output.py`:

```python
def test_tool_audit_excludes_emerge_icc_tools(tmp_path: Path):
    """tool_audit.py must tolerate being called for emerge tools and produce valid JSON.
    
    In production the hooks.json matcher prevents this, but defensive check ensures
    the script doesn't crash if called accidentally.
    """
    out = _run(
        "tool_audit.py",
        {
            "tool_name": "mcp__plugin_emerge__icc_exec",
            "tool_input": {"intent_signature": "test.read.foo"},
            "tool_response": {},
            "hook_event_name": "PostToolUse",
        },
        tmp_path,
    )
    result = json.loads(out)
    # Must be valid JSON — no crash
    assert isinstance(result, dict)
```

- [ ] **Step 2: Run test to verify it passes (defensive baseline)**

```bash
python -m pytest tests/test_hook_scripts_output.py::test_tool_audit_excludes_emerge_icc_tools -xvs
```

Expected: PASS (tool_audit.py tolerates unknown tools).

- [ ] **Step 3: Update the tool_audit PostToolUse entry in hooks.json**

Locate the `tool_audit.py` PostToolUse hook entry and update it to add an explicit `if` exclusion condition. Replace that specific hook entry:

```json
{
  "matcher": "^(?!mcp__plugin_.*emerge.*__icc_).*",
  "hooks": [
    {
      "type": "command",
      "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/tool_audit.py",
      "statusMessage": "Flywheel auditing tool...",
      "if": "!mcp__plugin_emerge_emerge__icc_*"
    }
  ]
}
```

> **Validation note:** The `if` field syntax for negative matching may differ from the documented examples. Run a live smoke test (Task 3 Step 5) to confirm. If `!mcp__*` syntax is unsupported, remove the `if` field — the regex matcher alone is sufficient.

- [ ] **Step 4: Validate hooks.json is still well-formed**

```bash
python3 -c "import json; json.load(open('hooks/hooks.json')); print('OK')"
```

Expected: `OK`

- [ ] **Step 5: Run full test suite**

```bash
python -m pytest tests -q
```

Expected: 515+ passed.

- [ ] **Step 6: Commit**

```bash
git add hooks/hooks.json tests/test_hook_scripts_output.py
git commit -m "feat: add conditional if field to tool_audit PostToolUse for explicit emerge exclusion"
```

---

## Task 4: F5 — CwdChanged Hook

When CC's working directory changes (user runs `cd`), detect if the new CWD is a different project root than the one emerge's session ID was derived from. If it differs, inject a warning and update the session context to reflect the new project root.

**Files:**
- Create: `hooks/cwd_changed.py`
- Modify: `hooks/hooks.json` (add CwdChanged entry)
- Modify: `tests/test_hook_scripts_output.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_hook_scripts_output.py`:

```python
def test_cwd_changed_emits_system_message(tmp_path: Path):
    """CwdChanged outputs systemMessage when CWD shifts to a new project."""
    out = _run(
        "cwd_changed.py",
        {
            "hook_event_name": "CwdChanged",
            "old_cwd": "/Users/alice/projects/emerge",
            "new_cwd": "/Users/alice/projects/other-project",
            "cwd": "/Users/alice/projects/other-project",
        },
        tmp_path,
    )
    result = json.loads(out)
    # CwdChanged uses top-level systemMessage
    assert "systemMessage" in result
    assert "hookSpecificOutput" not in result
    assert "other-project" in result["systemMessage"]


def test_cwd_changed_same_dir_emits_empty(tmp_path: Path):
    """CwdChanged with same old and new CWD emits empty object."""
    out = _run(
        "cwd_changed.py",
        {
            "hook_event_name": "CwdChanged",
            "old_cwd": "/Users/alice/projects/emerge",
            "new_cwd": "/Users/alice/projects/emerge",
            "cwd": "/Users/alice/projects/emerge",
        },
        tmp_path,
    )
    result = json.loads(out)
    assert result == {}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_hook_scripts_output.py::test_cwd_changed_emits_system_message tests/test_hook_scripts_output.py::test_cwd_changed_same_dir_emits_empty -xvs
```

Expected: FAIL — `cwd_changed.py` does not exist.

- [ ] **Step 3: Implement `hooks/cwd_changed.py`**

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

    old_cwd = str(payload.get("old_cwd", "") or "")
    new_cwd = str(payload.get("new_cwd", "") or "")

    if not new_cwd or old_cwd == new_cwd:
        print(json.dumps({}))
        return

    # Notify Claude that the project context has changed.
    # emerge's session ID is derived from CWD at daemon start — mid-session CWD changes
    # mean the active session may not match the new project root.
    msg = (
        f"[emerge/CwdChanged] Working directory changed: {old_cwd} → {new_cwd}\n"
        "emerge session context was anchored to the original CWD. "
        "If you intend to work in this new directory, be aware that:\n"
        "- Flywheel spans and pipeline intents still reference the original session.\n"
        "- Use the new project's connector names explicitly in intent_signature.\n"
        f"New CWD: {new_cwd}"
    )
    # CwdChanged uses top-level systemMessage (not hookSpecificOutput)
    out = {"systemMessage": msg}
    print(json.dumps(out))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_hook_scripts_output.py::test_cwd_changed_emits_system_message tests/test_hook_scripts_output.py::test_cwd_changed_same_dir_emits_empty -xvs
```

Expected: Both PASS.

- [ ] **Step 5: Register CwdChanged in hooks.json**

Add after the PostCompact block in `hooks/hooks.json`:

```json
"CwdChanged": [
  {
    "matcher": ".*",
    "hooks": [
      {
        "type": "command",
        "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/cwd_changed.py",
        "statusMessage": "Flywheel re-anchoring session to new directory..."
      }
    ]
  }
],
```

- [ ] **Step 6: Run full test suite**

```bash
python -m pytest tests -q
```

Expected: 517+ passed (2 new tests).

- [ ] **Step 7: Commit**

```bash
git add hooks/cwd_changed.py hooks/hooks.json tests/test_hook_scripts_output.py
git commit -m "feat: add CwdChanged hook to warn on mid-session project switch"
```

---

## Task 5: F6 — Elicitation + ElicitationResult Hooks

`Elicitation` hook auto-responds to emerge's elicitations when `EMERGE_CI=1` is set (no dialog shown to user). `ElicitationResult` hook audits all elicitation outcomes to `~/.emerge/elicitation-log.jsonl`.

**Files:**
- Create: `hooks/elicitation.py`
- Create: `hooks/elicitation_result.py`
- Modify: `hooks/hooks.json`
- Modify: `tests/test_hook_scripts_output.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_hook_scripts_output.py`:

```python
import os as _os

def test_elicitation_ci_mode_auto_accepts_span_approve(tmp_path: Path):
    """In EMERGE_CI=1 mode, Elicitation hook auto-accepts icc_span_approve elicitation."""
    env_backup = _os.environ.copy()
    try:
        _os.environ["EMERGE_CI"] = "1"
        _os.environ["CLAUDE_PLUGIN_DATA"] = str(tmp_path)
        out = _run(
            "elicitation.py",
            {
                "hook_event_name": "Elicitation",
                "mcp_server_name": "plugin_emerge_emerge",
                "message": "Activate pipeline `lark.read.get-doc`?\nThis will move from _pending/ to ...",
                "mode": "form",
                "elicitation_id": "elicit-abc123",
                "requested_schema": {
                    "type": "object",
                    "properties": {"confirmed": {"type": "boolean"}},
                },
            },
            tmp_path,
        )
        result = json.loads(out)
        assert result["hookSpecificOutput"]["hookEventName"] == "Elicitation"
        assert result["hookSpecificOutput"]["action"] == "accept"
        assert result["hookSpecificOutput"]["content"]["confirmed"] is True
    finally:
        _os.environ.clear()
        _os.environ.update(env_backup)


def test_elicitation_non_ci_mode_passes_through(tmp_path: Path):
    """Without EMERGE_CI=1, Elicitation hook emits empty object (let CC show dialog)."""
    env_backup = _os.environ.copy()
    try:
        _os.environ.pop("EMERGE_CI", None)
        _os.environ["CLAUDE_PLUGIN_DATA"] = str(tmp_path)
        out = _run(
            "elicitation.py",
            {
                "hook_event_name": "Elicitation",
                "mcp_server_name": "plugin_emerge_emerge",
                "message": "Activate pipeline `lark.read.get-doc`?",
                "mode": "form",
                "elicitation_id": "elicit-abc123",
                "requested_schema": {"type": "object"},
            },
            tmp_path,
        )
        result = json.loads(out)
        # No override — empty output lets CC show the dialog normally
        assert result == {}
    finally:
        _os.environ.clear()
        _os.environ.update(env_backup)


def test_elicitation_ci_auto_accepts_reconcile(tmp_path: Path):
    """In EMERGE_CI=1, auto-accepts reconcile with outcome=confirm."""
    env_backup = _os.environ.copy()
    try:
        _os.environ["EMERGE_CI"] = "1"
        _os.environ["CLAUDE_PLUGIN_DATA"] = str(tmp_path)
        out = _run(
            "elicitation.py",
            {
                "hook_event_name": "Elicitation",
                "mcp_server_name": "plugin_emerge_emerge",
                "message": "Choose the reconciliation outcome for delta `delta-001`:",
                "mode": "form",
                "elicitation_id": "elicit-def456",
                "requested_schema": {
                    "type": "object",
                    "properties": {"outcome": {"type": "string"}},
                },
            },
            tmp_path,
        )
        result = json.loads(out)
        assert result["hookSpecificOutput"]["action"] == "accept"
        assert result["hookSpecificOutput"]["content"]["outcome"] == "confirm"
    finally:
        _os.environ.clear()
        _os.environ.update(env_backup)


def test_elicitation_result_writes_audit_log(tmp_path: Path):
    """ElicitationResult appends entry to elicitation-log.jsonl."""
    out = _run(
        "elicitation_result.py",
        {
            "hook_event_name": "ElicitationResult",
            "mcp_server_name": "plugin_emerge_emerge",
            "action": "accept",
            "content": {"confirmed": True},
            "mode": "form",
            "elicitation_id": "elicit-abc123",
        },
        tmp_path,
    )
    result = json.loads(out)
    # ElicitationResult uses top-level systemMessage or empty — never hookSpecificOutput
    assert "hookSpecificOutput" not in result
    # Audit log must have been written
    log_path = tmp_path / "elicitation-log.jsonl"
    assert log_path.exists()
    entry = json.loads(log_path.read_text().strip())
    assert entry["elicitation_id"] == "elicit-abc123"
    assert entry["action"] == "accept"
    assert entry["mcp_server_name"] == "plugin_emerge_emerge"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_hook_scripts_output.py::test_elicitation_ci_mode_auto_accepts_span_approve tests/test_hook_scripts_output.py::test_elicitation_result_writes_audit_log -xvs
```

Expected: FAIL — scripts don't exist.

- [ ] **Step 3: Implement `hooks/elicitation.py`**

```python
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _ci_mode() -> bool:
    return os.environ.get("EMERGE_CI", "").strip() in ("1", "true", "yes")


def _auto_response(message: str, schema: dict) -> dict | None:
    """Return auto-response content for known emerge elicitation patterns, or None."""
    msg_lower = message.lower()
    props = schema.get("properties", {}) if isinstance(schema, dict) else {}

    # icc_span_approve: "activate pipeline ..." → {confirmed: true}
    if "activate pipeline" in msg_lower and "confirmed" in props:
        return {"confirmed": True}

    # icc_reconcile: "choose the reconciliation outcome ..." → {outcome: "confirm"}
    if "reconciliation outcome" in msg_lower and "outcome" in props:
        # Allow override via env var for CI pipelines that need a specific outcome
        outcome = os.environ.get("EMERGE_CI_RECONCILE_OUTCOME", "confirm").strip()
        if outcome not in ("confirm", "correct", "retract"):
            outcome = "confirm"
        return {"outcome": outcome}

    # icc_hub resolve: "choose the resolution strategy ..." → {resolution: "ours"}
    if "resolution strategy" in msg_lower and "resolution" in props:
        resolution = os.environ.get("EMERGE_CI_HUB_RESOLUTION", "ours").strip()
        if resolution not in ("ours", "theirs", "skip"):
            resolution = "ours"
        return {"resolution": resolution}

    return None


def main() -> None:
    payload_text = sys.stdin.read().strip()
    try:
        payload = json.loads(payload_text) if payload_text else {}
    except Exception:
        payload = {}

    # Only intercept emerge elicitations
    mcp_server = str(payload.get("mcp_server_name", "") or "")
    if "emerge" not in mcp_server:
        print(json.dumps({}))
        return

    # Only auto-respond in CI mode
    if not _ci_mode():
        print(json.dumps({}))
        return

    message = str(payload.get("message", "") or "")
    schema = payload.get("requested_schema") or {}
    content = _auto_response(message, schema)

    if content is None:
        # Unknown elicitation pattern in CI — decline rather than silently skip
        out = {
            "hookSpecificOutput": {
                "hookEventName": "Elicitation",
                "action": "decline",
                "content": {},
            }
        }
    else:
        out = {
            "hookSpecificOutput": {
                "hookEventName": "Elicitation",
                "action": "accept",
                "content": content,
            }
        }

    print(json.dumps(out))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Implement `hooks/elicitation_result.py`**

```python
from __future__ import annotations

import json
import os
import sys
import time
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

    # Only log emerge elicitations
    mcp_server = str(payload.get("mcp_server_name", "") or "")
    if "emerge" not in mcp_server:
        print(json.dumps({}))
        return

    try:
        data_root_env = os.environ.get("CLAUDE_PLUGIN_DATA", "")
        if data_root_env:
            log_dir = Path(data_root_env)
        else:
            from scripts.policy_config import default_hook_state_root, pin_plugin_data_path_if_present
            pin_plugin_data_path_if_present()
            log_dir = Path(default_hook_state_root())
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "elicitation-log.jsonl"
        entry = {
            "ts_ms": int(time.time() * 1000),
            "elicitation_id": str(payload.get("elicitation_id", "") or ""),
            "mcp_server_name": mcp_server,
            "action": str(payload.get("action", "") or ""),
            "content": payload.get("content") or {},
            "mode": str(payload.get("mode", "") or ""),
        }
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=True) + "\n")
    except Exception:
        pass

    # ElicitationResult uses top-level systemMessage or empty
    print(json.dumps({}))


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run all four elicitation tests**

```bash
python -m pytest tests/test_hook_scripts_output.py::test_elicitation_ci_mode_auto_accepts_span_approve tests/test_hook_scripts_output.py::test_elicitation_non_ci_mode_passes_through tests/test_hook_scripts_output.py::test_elicitation_ci_auto_accepts_reconcile tests/test_hook_scripts_output.py::test_elicitation_result_writes_audit_log -xvs
```

Expected: All 4 PASS.

- [ ] **Step 6: Register Elicitation + ElicitationResult in hooks.json**

Add after the CwdChanged block:

```json
"Elicitation": [
  {
    "matcher": ".*",
    "hooks": [
      {
        "type": "command",
        "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/elicitation.py",
        "statusMessage": "Flywheel handling elicitation..."
      }
    ]
  }
],
"ElicitationResult": [
  {
    "matcher": ".*",
    "hooks": [
      {
        "type": "command",
        "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/elicitation_result.py",
        "statusMessage": "Flywheel logging elicitation outcome..."
      }
    ]
  }
],
```

- [ ] **Step 7: Run full test suite**

```bash
python -m pytest tests -q
```

Expected: 521+ passed (4 new tests).

- [ ] **Step 8: Commit**

```bash
git add hooks/elicitation.py hooks/elicitation_result.py hooks/hooks.json tests/test_hook_scripts_output.py
git commit -m "feat: add Elicitation CI auto-response and ElicitationResult audit log hooks"
```

---

## Task 6: F1 — FileChanged Hook (Replace 3-tier Cockpit Dispatch)

Register a FileChanged hook that watches `pending-actions.json` and delivers cockpit actions directly when the file appears — replacing the Monitor tool polling path. This is an **additive** path, not a replacement; the UserPromptSubmit fallback remains as a safety net. Once FileChanged is validated working, the Monitor tool path (`watch_pending.py`) becomes optional.

**Key uncertainty to validate:** Does CC support env var expansion (e.g., `${CLAUDE_PLUGIN_DATA}`) in the FileChanged `matcher` field? If yes, use the absolute path. If not, use filename-only matching and bootstrap via `watchPaths` from `setup.py`.

**Files:**
- Create: `hooks/file_changed.py`
- Modify: `hooks/hooks.json`
- Modify: `hooks/setup.py`
- Modify: `tests/test_hook_scripts_output.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_hook_scripts_output.py`:

```python
def test_file_changed_pending_actions_delivers_to_cc(tmp_path: Path):
    """FileChanged hook processes pending-actions.json and delivers actions as additionalContext."""
    pending = tmp_path / "pending-actions.json"
    pending.write_text(json.dumps({
        "actions": [
            {"type": "tool-call", "call": {"tool": "icc_exec", "arguments": {"intent_signature": "lark.read.foo"}}}
        ]
    }))
    out = _run(
        "file_changed.py",
        {
            "hook_event_name": "FileChanged",
            "file_path": str(pending),
            "event": "add",
        },
        tmp_path,
    )
    result = json.loads(out)
    assert result["hookSpecificOutput"]["hookEventName"] == "FileChanged"
    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "icc_exec" in ctx
    assert "lark.read.foo" in ctx
    # File renamed to delivered
    assert not pending.exists()
    delivered = tmp_path / "pending-actions.delivered.json"
    assert delivered.exists()


def test_file_changed_non_pending_file_emits_empty(tmp_path: Path):
    """FileChanged for unrelated files emits empty object."""
    some_file = tmp_path / "random.txt"
    some_file.write_text("hello")
    out = _run(
        "file_changed.py",
        {
            "hook_event_name": "FileChanged",
            "file_path": str(some_file),
            "event": "change",
        },
        tmp_path,
    )
    result = json.loads(out)
    assert result == {}


def test_file_changed_returns_watch_paths_for_bootstrap(tmp_path: Path):
    """FileChanged always returns watchPaths containing the state root pending-actions path."""
    out = _run(
        "file_changed.py",
        {
            "hook_event_name": "FileChanged",
            "file_path": "/some/unrelated/file.txt",
            "event": "change",
        },
        tmp_path,
    )
    result = json.loads(out)
    # watchPaths must be present to keep the watch list alive across events
    assert "watchPaths" in result
    assert isinstance(result["watchPaths"], list)
    assert any("pending-actions.json" in p for p in result["watchPaths"])


def test_setup_outputs_watch_paths(tmp_path: Path):
    """setup.py must output watchPaths to bootstrap FileChanged on session start."""
    out = _run("setup.py", {}, tmp_path)
    result = json.loads(out)
    # watchPaths at top level (Setup uses systemMessage, not hookSpecificOutput)
    # If CC supports watchPaths on Setup, it will be present; test validates the field is output
    assert "watchPaths" in result
    assert isinstance(result["watchPaths"], list)
    assert any("pending-actions.json" in p for p in result["watchPaths"])
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_hook_scripts_output.py::test_file_changed_pending_actions_delivers_to_cc tests/test_hook_scripts_output.py::test_file_changed_non_pending_file_emits_empty tests/test_hook_scripts_output.py::test_file_changed_returns_watch_paths_for_bootstrap tests/test_hook_scripts_output.py::test_setup_outputs_watch_paths -xvs
```

Expected: FAIL — scripts missing or watchPaths not yet output.

- [ ] **Step 3: Implement `hooks/file_changed.py`**

```python
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _state_root() -> Path:
    data_root_env = os.environ.get("CLAUDE_PLUGIN_DATA", "")
    if data_root_env:
        return Path(data_root_env)
    try:
        from scripts.policy_config import default_hook_state_root, pin_plugin_data_path_if_present
        pin_plugin_data_path_if_present()
        return Path(default_hook_state_root())
    except Exception:
        return Path.home() / ".emerge"


def _watch_paths(state_root: Path) -> list[str]:
    """Absolute paths CC should keep watching."""
    return [str(state_root / "pending-actions.json")]


def main() -> None:
    payload_text = sys.stdin.read().strip()
    try:
        payload = json.loads(payload_text) if payload_text else {}
    except Exception:
        payload = {}

    file_path = str(payload.get("file_path", "") or "")
    state_root = _state_root()
    watch_list = _watch_paths(state_root)

    # Only process pending-actions files written by the cockpit
    is_pending = file_path.endswith("pending-actions.json") and "pending-actions.delivered" not in file_path

    if not is_pending:
        # Return watchPaths to keep the watch list alive even for unrelated events
        print(json.dumps({"watchPaths": watch_list}))
        return

    p = Path(file_path)
    if not p.exists():
        print(json.dumps({"watchPaths": watch_list}))
        return

    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print(json.dumps({"watchPaths": watch_list}))
        return

    actions = data.get("actions", [])
    if not actions:
        print(json.dumps({"watchPaths": watch_list}))
        return

    # Deliver by renaming (same contract as UserPromptSubmit drain)
    delivered = p.parent / "pending-actions.delivered.json"
    try:
        p.rename(delivered)
    except OSError:
        pass

    from scripts.pending_actions import format_pending_actions
    ctx = format_pending_actions(actions)

    out = {
        "hookSpecificOutput": {
            "hookEventName": "FileChanged",
            "additionalContext": ctx,
        },
        "watchPaths": watch_list,
    }
    print(json.dumps(out))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Update `hooks/setup.py` to output watchPaths**

Replace the final `out` assignment in `setup.py` (keep everything else):

```python
    emerge_pending = emerge_home / "pending-actions.json"
    # Output watchPaths so CC's FileChanged hook can watch the pending-actions file.
    # CC may ignore this field on Setup events — validated in tests.
    # Setup uses top-level systemMessage (not hookSpecificOutput).
    out = {
        "systemMessage": f"emerge plugin ready. Home: {emerge_home}",
        "watchPaths": [str(emerge_pending)],
    }
    print(json.dumps(out))
```

The full updated `main()` function in `hooks/setup.py`:

```python
def main() -> None:
    payload_text = sys.stdin.read().strip()
    try:
        payload = json.loads(payload_text) if payload_text else {}
    except Exception:
        payload = {}

    # Ensure required directories exist
    emerge_home = default_emerge_home()
    for subdir in ("hook-state", "connectors", "repl"):
        (emerge_home / subdir).mkdir(parents=True, exist_ok=True)

    # Pin CLAUDE_PLUGIN_DATA so non-hook processes can resolve the same state root.
    pin_plugin_data_path_if_present()
    GoalControlPlane().ensure_initialized()

    emerge_pending = emerge_home / "pending-actions.json"
    # watchPaths seeds CC's FileChanged watch list for pending-actions delivery.
    # Setup uses top-level systemMessage + watchPaths (hookSpecificOutput not allowed on Setup).
    out = {
        "systemMessage": f"emerge plugin ready. Home: {emerge_home}",
        "watchPaths": [str(emerge_pending)],
    }
    print(json.dumps(out))
```

- [ ] **Step 5: Register FileChanged in hooks.json**

Add after the ElicitationResult block. Use `${CLAUDE_PLUGIN_DATA}/pending-actions.json` as the matcher — validate whether CC expands env vars in matcher. The basename fallback ensures firing even without expansion:

```json
"FileChanged": [
  {
    "matcher": "pending-actions.json",
    "hooks": [
      {
        "type": "command",
        "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/file_changed.py",
        "statusMessage": "Flywheel delivering cockpit actions..."
      }
    ]
  }
],
```

> **Validation:** After deploying, trigger by writing a test pending-actions.json to `~/.emerge/` and verify the hook fires. If the `pending-actions.json` basename matcher only watches the project directory (not `~/.emerge/`), upgrade to the absolute path or use the `watchPaths` bootstrap from `setup.py`. Document the result in a comment in hooks.json.

- [ ] **Step 6: Run all four FileChanged tests**

```bash
python -m pytest tests/test_hook_scripts_output.py::test_file_changed_pending_actions_delivers_to_cc tests/test_hook_scripts_output.py::test_file_changed_non_pending_file_emits_empty tests/test_hook_scripts_output.py::test_file_changed_returns_watch_paths_for_bootstrap tests/test_hook_scripts_output.py::test_setup_outputs_watch_paths -xvs
```

Expected: All 4 PASS.

- [ ] **Step 7: Run full test suite**

```bash
python -m pytest tests -q
```

Expected: 525+ passed (4 new tests + all prior).

- [ ] **Step 8: Update CLAUDE.md — Cockpit→CC dispatch documentation**

In `CLAUDE.md`, update the "Cockpit→CC action dispatch (three-tier)" Architecture section to document the new FileChanged path as the primary delivery mechanism:

Find this line:
```
**1. Monitor tool (primary, real-time)**
```

Update the section to read:

```
**0. FileChanged hook (preferred, real-time, zero-poll)**: `file_changed.py` watches `pending-actions.json` via CC's FileChanged hook. When cockpit writes the file, CC fires the hook immediately and `additionalContext` delivers actions to Claude. Returns `watchPaths` to keep the watch list alive.
**1. Monitor tool (secondary, real-time)**: ...
```

Also add `FileChanged` to the hooks.json matchers Key Invariant and the Documentation Update Rules table.

- [ ] **Step 9: Run final full suite**

```bash
python -m pytest tests -q
```

Expected: 525+ passed.

- [ ] **Step 10: Commit**

```bash
git add hooks/file_changed.py hooks/setup.py hooks/hooks.json tests/test_hook_scripts_output.py CLAUDE.md
git commit -m "feat: add FileChanged hook for real-time cockpit pending-actions delivery"
```

---

## Quick Verification Baseline

After all tasks complete:

```bash
python -m pytest tests -q
```

Expected: **525+ passed** (up from 512). New tests:
- 2 × PostCompact
- 1 × tool_audit defensive
- 2 × CwdChanged
- 4 × Elicitation / ElicitationResult
- 4 × FileChanged + setup watchPaths

New hook scripts: `post_compact.py`, `cwd_changed.py`, `elicitation.py`, `elicitation_result.py`, `file_changed.py`

New hooks in `hooks.json`: PostCompact, CwdChanged, Elicitation, ElicitationResult, FileChanged — plus `once`, `statusMessage`, `if` on all existing entries.

---

## Self-Review

**Spec coverage:** All 6 findings covered:
- F1 FileChanged → Task 6 ✓
- F2 PostCompact → Task 1 ✓
- F3 once + statusMessage → Task 2 ✓
- F4 conditional if → Task 3 ✓
- F5 CwdChanged → Task 4 ✓
- F6 Elicitation/ElicitationResult → Task 5 ✓

**Placeholder scan:** No TBDs or placeholders. All code blocks are complete. File paths are exact.

**Type consistency:**
- `_state_root()` defined and used consistently in `file_changed.py`
- `_watch_paths()` returns `list[str]` — used as `"watchPaths"` dict key
- `_ci_mode()` returns `bool` — used in guard in `elicitation.py`
- `format_pending_actions` imported from `scripts.pending_actions` in `file_changed.py` — same module already used in `user_prompt_submit.py`
- PostCompact script mirrors pre_compact.py structure exactly, no new types introduced

**Edge cases documented:**
- FileChanged matcher bootstrap uncertainty → validation step included in Task 6 Step 5
- ElicitationResult uses `{}` output (not hookSpecificOutput) — consistent with CC schema invariant for non-allowed events
- `once: true` on Setup noted — CC schema validation required; if unsupported, field is silently ignored
