# Phase 4: Hooks — Watcher Liveness + PermissionDenied + Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `TeammateIdle` hook to keep agents-team watchers alive, add `PermissionDenied` hook to prevent silent icc_* tool failures in auto mode, and remove a dead `"if"` field from hooks.json.

**Architecture:** Two new hook scripts follow the exit-code-2 / JSON-return patterns already established by `task_completed.py` and `stop.py`. `hooks.json` gets two new event entries and one field removal. `CLAUDE.md` gets updated invariant lines. No daemon changes needed.

**Tech Stack:** Python 3.10+ stdlib only, subprocess-based tests (same pattern as `tests/test_hooks_stop.py`).

---

## File Map

| File | Type | What changes |
|---|---|---|
| `hooks/teammate_idle.py` | Create | Exit 2 for `*-watcher` in `emerge-monitors` team; else `{}` |
| `hooks/permission_denied.py` | Create | `{"retry": true}` for `icc_*` tools; else `{}` |
| `hooks/hooks.json` | Modify | Add `TeammateIdle` + `PermissionDenied` entries; remove dead `"if"` field |
| `tests/test_hooks_teammate_idle_permission_denied.py` | Create | Subprocess tests for both new hooks |
| `CLAUDE.md` | Modify | Update hooks.json matchers invariant; add two new invariant bullets |

---

## Task 1: TeammateIdle hook

**Files:**
- Create: `hooks/teammate_idle.py`
- Create: `tests/test_hooks_teammate_idle_permission_denied.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_hooks_teammate_idle_permission_denied.py`:

```python
# tests/test_hooks_teammate_idle_permission_denied.py
"""Tests for teammate_idle.py and permission_denied.py hooks."""
from __future__ import annotations
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEAMMATE_IDLE_HOOK = ROOT / "hooks" / "teammate_idle.py"
PERMISSION_DENIED_HOOK = ROOT / "hooks" / "permission_denied.py"


def _run(script: Path, payload: dict, data_dir: Path):
    """Run a hook script with JSON payload on stdin. Returns (returncode, stdout, stderr)."""
    env = {**os.environ, "EMERGE_DATA_ROOT": str(data_dir)}
    result = subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


# ---------------------------------------------------------------------------
# TeammateIdle tests
# ---------------------------------------------------------------------------

def test_teammate_idle_exits_2_for_watcher_in_monitors_team(tmp_path):
    """A *-watcher in emerge-monitors must get exit 2 to keep it alive."""
    rc, out, err = _run(
        TEAMMATE_IDLE_HOOK,
        {"hook_event_name": "TeammateIdle",
         "team_name": "emerge-monitors",
         "teammate_name": "mycader-1-watcher"},
        tmp_path,
    )
    assert rc == 2
    assert "monitor" in err.lower()


def test_teammate_idle_feedback_mentions_watch_emerge(tmp_path):
    """Feedback message must tell the agent to restart watch_emerge Monitor."""
    rc, out, err = _run(
        TEAMMATE_IDLE_HOOK,
        {"hook_event_name": "TeammateIdle",
         "team_name": "emerge-monitors",
         "teammate_name": "profile-abc-watcher"},
        tmp_path,
    )
    assert rc == 2
    assert "watch_emerge" in err


def test_teammate_idle_allows_other_teams(tmp_path):
    """Non-monitors team must be allowed to go idle (exit 0, empty JSON)."""
    rc, out, err = _run(
        TEAMMATE_IDLE_HOOK,
        {"hook_event_name": "TeammateIdle",
         "team_name": "superpowers",
         "teammate_name": "researcher"},
        tmp_path,
    )
    assert rc == 0
    assert json.loads(out) == {}


def test_teammate_idle_allows_non_watcher_in_monitors_team(tmp_path):
    """A non-watcher agent in emerge-monitors (e.g. team-lead) may go idle."""
    rc, out, err = _run(
        TEAMMATE_IDLE_HOOK,
        {"hook_event_name": "TeammateIdle",
         "team_name": "emerge-monitors",
         "teammate_name": "team-lead"},
        tmp_path,
    )
    assert rc == 0
    assert json.loads(out) == {}


def test_teammate_idle_empty_payload_is_safe(tmp_path):
    """Empty payload (no team_name / teammate_name) must not crash; exit 0."""
    rc, out, err = _run(TEAMMATE_IDLE_HOOK, {}, tmp_path)
    assert rc == 0
    assert json.loads(out) == {}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_hooks_teammate_idle_permission_denied.py::test_teammate_idle_exits_2_for_watcher_in_monitors_team -v
```

Expected: `FAILED` — `FileNotFoundError: hooks/teammate_idle.py`

- [ ] **Step 3: Create `hooks/teammate_idle.py`**

```python
"""TeammateIdle hook — keep emerge-monitors watcher agents alive.

When a teammate in the `emerge-monitors` team whose name ends with `-watcher`
is about to go idle, exit with code 2. CC sends the stderr text back to the
agent as feedback, causing it to continue working instead of going idle.

All other agents (other teams, or non-watcher teammates in emerge-monitors)
are allowed to go idle normally — return `{}`, exit 0.

Output contract: TeammateIdle is NOT in CC's hookSpecificOutput-allowed list.
Use raw stderr text + exit code 2 to keep the agent running.
"""
from __future__ import annotations

import json
import sys


def main() -> None:
    payload_text = sys.stdin.read().strip()
    try:
        payload = json.loads(payload_text) if payload_text else {}
    except Exception:
        payload = {}

    team_name = str(payload.get("team_name") or "")
    teammate_name = str(payload.get("teammate_name") or "")

    if team_name == "emerge-monitors" and teammate_name.endswith("-watcher"):
        print(
            f"[emerge/TeammateIdle] {teammate_name} is an emerge monitor agent — "
            "do NOT go idle. Resume your persistent watch_emerge Monitor: "
            f"command: python3 ${{CLAUDE_PLUGIN_ROOT}}/scripts/watch_emerge.py "
            f"--runner-profile {teammate_name.removesuffix('-watcher')} "
            "description: 'pattern alert watcher' persistent: true. "
            "Wait for the next pattern alert and apply the stage→action protocol.",
            file=sys.stderr,
        )
        sys.exit(2)

    # All other agents: allow idle normally
    print(json.dumps({}))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run TeammateIdle tests to verify they pass**

```bash
python -m pytest tests/test_hooks_teammate_idle_permission_denied.py -k "teammate" -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit TeammateIdle hook**

```bash
git add hooks/teammate_idle.py tests/test_hooks_teammate_idle_permission_denied.py
git commit -m "feat: TeammateIdle hook — keep emerge-monitors watcher agents alive via exit 2"
```

---

## Task 2: PermissionDenied hook

**Files:**
- Create: `hooks/permission_denied.py`
- Modify: `tests/test_hooks_teammate_idle_permission_denied.py` (append tests)

- [ ] **Step 1: Append PermissionDenied tests**

Append to `tests/test_hooks_teammate_idle_permission_denied.py`:

```python
# ---------------------------------------------------------------------------
# PermissionDenied tests
# ---------------------------------------------------------------------------

def test_permission_denied_retry_for_icc_exec(tmp_path):
    """icc_exec denied → retry: true."""
    rc, out, err = _run(
        PERMISSION_DENIED_HOOK,
        {"hook_event_name": "PermissionDenied",
         "tool_name": "mcp__plugin_emerge_emerge__icc_exec"},
        tmp_path,
    )
    assert rc == 0
    assert json.loads(out).get("retry") is True


def test_permission_denied_retry_for_icc_span_open(tmp_path):
    """icc_span_open denied → retry: true."""
    rc, out, err = _run(
        PERMISSION_DENIED_HOOK,
        {"hook_event_name": "PermissionDenied",
         "tool_name": "mcp__plugin_emerge_emerge__icc_span_open"},
        tmp_path,
    )
    assert rc == 0
    assert json.loads(out).get("retry") is True


def test_permission_denied_retry_for_any_icc_variant(tmp_path):
    """Any icc_* tool from any emerge plugin variant → retry: true."""
    rc, out, err = _run(
        PERMISSION_DENIED_HOOK,
        {"hook_event_name": "PermissionDenied",
         "tool_name": "mcp__plugin_test_emerge_test__icc_crystallize"},
        tmp_path,
    )
    assert rc == 0
    assert json.loads(out).get("retry") is True


def test_permission_denied_no_retry_for_bash(tmp_path):
    """Bash denied → no retry opinion (empty dict)."""
    rc, out, err = _run(
        PERMISSION_DENIED_HOOK,
        {"hook_event_name": "PermissionDenied",
         "tool_name": "Bash"},
        tmp_path,
    )
    assert rc == 0
    assert json.loads(out).get("retry") is not True


def test_permission_denied_no_retry_for_non_icc_mcp(tmp_path):
    """Non-icc_ MCP tool denied → no retry opinion."""
    rc, out, err = _run(
        PERMISSION_DENIED_HOOK,
        {"hook_event_name": "PermissionDenied",
         "tool_name": "mcp__plugin_emerge_emerge__icc_goal_read"},
        tmp_path,
    )
    # icc_goal_read IS an icc_ tool — expect retry: true
    assert json.loads(out).get("retry") is True


def test_permission_denied_empty_payload_is_safe(tmp_path):
    """Empty payload must not crash; no retry."""
    rc, out, err = _run(PERMISSION_DENIED_HOOK, {}, tmp_path)
    assert rc == 0
    assert json.loads(out).get("retry") is not True
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_hooks_teammate_idle_permission_denied.py -k "permission" -v
```

Expected: `FAILED` — `FileNotFoundError: hooks/permission_denied.py`

- [ ] **Step 3: Create `hooks/permission_denied.py`**

```python
"""PermissionDenied hook — retry icc_* tools denied by auto-mode classifier.

When CC's auto-mode classifier denies an emerge icc_* tool call, this hook
returns {"retry": true} to tell CC that the model should be allowed to retry
with explicit permission. Without this, icc_* denials are silent — the model
never sees an error and the flywheel misses the event.

All other tools (Bash, Write, etc.) are not retried — those are the user's
permission settings to respect.

Output contract: top-level {"retry": true}. Not hookSpecificOutput.
"""
from __future__ import annotations

import json
import re
import sys

_ICC_PATTERN = re.compile(r"mcp__plugin_.*emerge.*__icc_.*")


def main() -> None:
    payload_text = sys.stdin.read().strip()
    try:
        payload = json.loads(payload_text) if payload_text else {}
    except Exception:
        payload = {}

    tool_name = str(payload.get("tool_name") or "")
    if _ICC_PATTERN.fullmatch(tool_name):
        print(json.dumps({"retry": True}))
    else:
        print(json.dumps({}))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run PermissionDenied tests to verify they pass**

```bash
python -m pytest tests/test_hooks_teammate_idle_permission_denied.py -k "permission" -v
```

Expected: 6 passed.

- [ ] **Step 5: Run full test file**

```bash
python -m pytest tests/test_hooks_teammate_idle_permission_denied.py -v
```

Expected: 11 passed.

- [ ] **Step 6: Commit PermissionDenied hook**

```bash
git add hooks/permission_denied.py tests/test_hooks_teammate_idle_permission_denied.py
git commit -m "feat: PermissionDenied hook — retry icc_* tools denied by auto-mode classifier"
```

---

## Task 3: hooks.json — register new hooks + remove dead field

**Files:**
- Modify: `hooks/hooks.json`

- [ ] **Step 1: Add `TeammateIdle` entry to hooks.json**

In `hooks/hooks.json`, add this entry inside the top-level `"hooks"` object, after `"SubagentStart"`:

```json
    "TeammateIdle": [
      {
        "matcher": ".*",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/teammate_idle.py",
            "timeout": 10,
            "statusMessage": "Flywheel keeping watcher alive..."
          }
        ]
      }
    ],
```

- [ ] **Step 2: Add `PermissionDenied` entry to hooks.json**

Add after the `TeammateIdle` entry:

```json
    "PermissionDenied": [
      {
        "matcher": "mcp__plugin_.*emerge.*__icc_.*",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/permission_denied.py",
            "timeout": 10,
            "statusMessage": "Flywheel requesting retry..."
          }
        ]
      }
    ],
```

- [ ] **Step 3: Remove dead `"if"` field from PostToolUse tool_audit entry**

In `hooks/hooks.json`, find the PostToolUse `tool_audit` hook entry. It currently looks like:

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

Remove the `"if": "!mcp__plugin_emerge_emerge__icc_*"` line. Result:

```json
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
```

- [ ] **Step 4: Verify hooks.json is valid JSON**

```bash
python3 -c "import json; json.load(open('hooks/hooks.json')); print('OK')"
```

Expected: `OK`

- [ ] **Step 5: Run full test suite to check no regressions**

```bash
python -m pytest tests -q --tb=short
```

Expected: 567+ passed.

- [ ] **Step 6: Commit hooks.json changes**

```bash
git add hooks/hooks.json
git commit -m "feat: hooks.json — register TeammateIdle + PermissionDenied; remove dead 'if' field"
```

---

## Task 4: CLAUDE.md updates

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update hooks.json matchers invariant line**

In `CLAUDE.md`, find the bullet:

```
- **hooks.json hook matchers**: `PreToolUse`, `PostToolUse`, `PostToolUseFailure` all use `mcp__plugin_.*emerge.*__icc_.*` to cover all current and future icc_ tools. `tool_audit.py` uses the inverse negative-lookahead. `SessionEnd`, `Stop`, `SubagentStop` are registered in `hooks/hooks.json` (matcher format). `plugin.json` only keeps `SessionStart → runner_sync.py` (runner sync runs separately from session_start.py). `StopFailure`, `TaskCompleted`, and `SubagentStart` are registered with `python3 ${CLAUDE_PLUGIN_ROOT}/hooks/stop_failure.py`, `task_completed.py`, and `subagent_start.py` respectively.
```

Replace with:

```
- **hooks.json hook matchers**: `PreToolUse`, `PostToolUse`, `PostToolUseFailure` all use `mcp__plugin_.*emerge.*__icc_.*` to cover all current and future icc_ tools. `tool_audit.py` uses the inverse negative-lookahead. `SessionEnd`, `Stop`, `SubagentStop` are registered in `hooks/hooks.json` (matcher format). `plugin.json` only keeps `SessionStart → runner_sync.py` (runner sync runs separately from session_start.py). `StopFailure`, `TaskCompleted`, and `SubagentStart` are registered with `python3 ${CLAUDE_PLUGIN_ROOT}/hooks/stop_failure.py`, `task_completed.py`, and `subagent_start.py` respectively. `TeammateIdle` (matcher `.*`) registered with `teammate_idle.py`. `PermissionDenied` (matcher `mcp__plugin_.*emerge.*__icc_.*`) registered with `permission_denied.py`.
```

- [ ] **Step 2: Add TeammateIdle invariant bullet**

After the `**Agents-team mode**:` bullet, add:

```
- **TeammateIdle hook** (`hooks/teammate_idle.py`): fires just before an agent teammate goes idle. For `team_name == "emerge-monitors"` + `teammate_name` ending in `-watcher`: exits code 2 with a feedback message telling the agent to restart its `watch_emerge` Monitor. Exit code 2 causes CC to feed the stderr back to the agent as feedback so it continues working. All other agents exit 0 and go idle normally. Output contract: raw stderr + exit 2 (NOT `hookSpecificOutput` — TeammateIdle is not in CC's allowed list).
```

- [ ] **Step 3: Add PermissionDenied invariant bullet**

After the `**TeammateIdle hook**:` bullet, add:

```
- **PermissionDenied hook** (`hooks/permission_denied.py`): fires when CC's auto-mode classifier denies a tool call. For `tool_name` matching `mcp__plugin_.*emerge.*__icc_.*`: returns `{"retry": true}` so CC lets the model retry with explicit permission. Prevents silent flywheel failures when icc_* tools are denied in auto mode. All other tools return `{}` (no opinion).
```

- [ ] **Step 4: Run full suite one final time**

```bash
python -m pytest tests -q --tb=short
```

Expected: all tests pass.

- [ ] **Step 5: Commit docs update**

```bash
git add CLAUDE.md
git commit -m "docs: CLAUDE.md — TeammateIdle + PermissionDenied hook invariants"
```

---

## Self-Review

**Spec coverage:**
1. ✅ TeammateIdle hook — Task 1 creates `hooks/teammate_idle.py`
2. ✅ PermissionDenied hook — Task 2 creates `hooks/permission_denied.py`
3. ✅ hooks.json registration — Task 3 adds both entries
4. ✅ Dead `"if"` field removal — Task 3 Step 3
5. ✅ CLAUDE.md updates — Task 4

**Placeholder scan:** No TBDs, no incomplete steps.

**Type consistency:**
- `teammate_idle.py` uses `teammate_name.removesuffix("-watcher")` — Python 3.9+, available in 3.10+ ✅
- `_ICC_PATTERN = re.compile(r"mcp__plugin_.*emerge.*__icc_.*")` — same pattern as `hooks.json` matcher ✅
- `{"retry": True}` → JSON `{"retry": true}` via `json.dumps` ✅
- Test file defines `TEAMMATE_IDLE_HOOK` and `PERMISSION_DENIED_HOOK` in Task 1; Task 2 appends to same file using those names ✅
