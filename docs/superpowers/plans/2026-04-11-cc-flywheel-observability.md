# CC Flywheel Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make CC a first-class observer of the flywheel lifecycle: notify on bridge failure, skeleton-ready events, and session cleanup — giving CC visibility over every state transition that currently happens silently.

**Architecture:** Four changes to `emerge_daemon.py` + a new `hooks/session_end.py` + `plugin.json` registration. Unified notification meta schema (`source`, `severity`, `category`, `intent_signature`, `requires_action`) replaces per-source ad-hoc meta dicts. Bridge failures push `severity=high` notifications; skeleton-ready events push `requires_action=True` notifications; SessionEnd hook clears orphaned span + elicit state.

**Tech Stack:** Python 3.11+, existing `_write_mcp_push()` in daemon, `plugin.json` hook registration, `_span_tracker` / `_elicit_events` instance vars.

---

## File Map

| File | Change |
|------|--------|
| `scripts/emerge_daemon.py` | Add `_notify()` helper; use in bridge fail, skeleton-ready, `_push_pattern`; add `cleanup_session()` tool-like method |
| `hooks/session_end.py` | New — calls daemon cleanup via subprocess (mirrors `session_start.py` pattern) |
| `.claude-plugin/plugin.json` | Register `SessionEnd` hook |
| `tests/test_mcp_tools_integration.py` | New tests for bridge-fail notification, skeleton-ready notification, unified meta |
| `tests/test_session_end.py` | New — unit test for session_end hook output |

---

### Task 1: `_notify()` helper + unified notification meta schema

**Files:**
- Modify: `scripts/emerge_daemon.py` (add `_notify` method to `EmergeDaemon`)
- Test: `tests/test_mcp_tools_integration.py`

- [ ] **Step 1: Write the failing test**

```python
def test_notify_helper_builds_correct_meta():
    """_notify() must produce a channel notification with unified meta schema."""
    import json
    from scripts.emerge_daemon import EmergeDaemon
    daemon = EmergeDaemon()
    pushed = []
    daemon._write_mcp_push = lambda p: pushed.append(p)

    daemon._notify(
        content="bridge failed for gmail.read.fetch",
        source="bridge",
        severity="high",
        category="warning",
        intent_signature="gmail.read.fetch",
        requires_action=False,
    )

    assert len(pushed) == 1
    p = pushed[0]
    assert p["method"] == "notifications/claude/channel"
    meta = p["params"]["meta"]
    assert meta["source"] == "bridge"
    assert meta["severity"] == "high"
    assert meta["category"] == "warning"
    assert meta["intent_signature"] == "gmail.read.fetch"
    assert meta["requires_action"] is False
    assert p["params"]["content"] == "bridge failed for gmail.read.fetch"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_notify_helper_builds_correct_meta -xvs
```

Expected: `FAILED` — `EmergeDaemon has no attribute '_notify'`

- [ ] **Step 3: Add `_notify()` to `EmergeDaemon`**

In `scripts/emerge_daemon.py`, add this method to `EmergeDaemon` class, right after `_write_mcp_push` (around line 2849):

```python
def _notify(
    self,
    content: str,
    source: str,
    severity: str = "info",        # info | warning | high
    category: str = "informational",  # informational | action_needed | warning
    intent_signature: str = "",
    requires_action: bool = False,
    extra_meta: dict | None = None,
) -> None:
    """Push a channel notification to CC with unified meta schema."""
    meta: dict = {
        "source": source,
        "severity": severity,
        "category": category,
        "requires_action": requires_action,
    }
    if intent_signature:
        meta["intent_signature"] = intent_signature
    if extra_meta:
        meta.update(extra_meta)
    self._write_mcp_push({
        "jsonrpc": "2.0",
        "method": "notifications/claude/channel",
        "params": {
            "serverName": "emerge",
            "content": content,
            "meta": meta,
        },
    })
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_notify_helper_builds_correct_meta -xvs
```

Expected: `PASSED`

- [ ] **Step 5: Commit**

```bash
git add scripts/emerge_daemon.py tests/test_mcp_tools_integration.py
git commit -m "feat: add _notify() helper with unified notification meta schema"
```

---

### Task 2: Bridge failure notification

**Files:**
- Modify: `scripts/emerge_daemon.py` (`_try_flywheel_bridge`, lines 226–246)
- Test: `tests/test_mcp_tools_integration.py`

- [ ] **Step 1: Write the failing test**

```python
def test_bridge_failure_pushes_high_severity_notification(tmp_path):
    """When flywheel bridge raises, daemon must push severity=high notification."""
    import json, os
    from scripts.emerge_daemon import EmergeDaemon
    from unittest.mock import patch

    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path)
    daemon = EmergeDaemon()

    # Seed pipelines-registry with a stable pipeline so bridge fires
    reg = {"pipelines": {"gmail.read.fetch": {"status": "stable", "consecutive_failures": 0}}}
    (tmp_path / "pipelines-registry.json").write_text(json.dumps(reg))

    pushed = []
    daemon._notify = lambda **kw: pushed.append(kw)

    # Patch pipeline.run_read to raise
    with patch.object(daemon.pipeline, "run_read", side_effect=RuntimeError("timeout")):
        result = daemon._try_flywheel_bridge({"intent_signature": "gmail.read.fetch"})

    os.environ.pop("EMERGE_STATE_ROOT", None)

    assert result is None  # bridge fell through
    assert len(pushed) == 1
    n = pushed[0]
    assert n["source"] == "bridge"
    assert n["severity"] == "high"
    assert n["intent_signature"] == "gmail.read.fetch"
    assert "timeout" in n["content"]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_bridge_failure_pushes_high_severity_notification -xvs
```

Expected: `FAILED` — no `pushed` entries (bridge silently returns None)

- [ ] **Step 3: Add notification to bridge failure path**

In `scripts/emerge_daemon.py`, replace the `except Exception as _bridge_exc:` block in `_try_flywheel_bridge` (lines 226–246):

```python
        except Exception as _bridge_exc:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "flywheel bridge failed for %s (%s), falling back to LLM: %s",
                base_pipeline_id, mode, _bridge_exc,
            )
            # Notify CC so it can intervene, retrigger, or log the degradation
            try:
                self._notify(
                    content=(
                        f"⚠️ Flywheel bridge failed for `{base_pipeline_id}` ({mode}): "
                        f"{_bridge_exc}. Falling back to LLM inference."
                    ),
                    source="bridge",
                    severity="high",
                    category="warning",
                    intent_signature=base_pipeline_id,
                    extra_meta={"failure_reason": str(_bridge_exc)},
                )
            except Exception:
                pass
            # Increment consecutive_failures directly in the registry so the policy engine
            # can downgrade stable→explore if the bridge keeps failing, without polluting
            # the recent_outcomes window (which would cause spurious window-failure downgrades).
            try:
                _reg_path = self._state_root / "pipelines-registry.json"
                with self._registry_lock:
                    _reg = self._load_json_object(_reg_path, root_key="pipelines")
                    _pe = _reg["pipelines"].get(base_pipeline_id)
                    if isinstance(_pe, dict):
                        _pe["consecutive_failures"] = int(_pe.get("consecutive_failures", 0)) + 1
                        _reg["pipelines"][base_pipeline_id] = _pe
                        self._atomic_write_json(_reg_path, _reg)
            except Exception:
                pass
            return None
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_bridge_failure_pushes_high_severity_notification -xvs
```

Expected: `PASSED`

- [ ] **Step 5: Run full suite to confirm no regressions**

```bash
python -m pytest tests/ -q --tb=short
```

Expected: same pass count as baseline (422+)

- [ ] **Step 6: Commit**

```bash
git add scripts/emerge_daemon.py tests/test_mcp_tools_integration.py
git commit -m "feat: push severity=high notification on flywheel bridge failure"
```

---

### Task 3: Skeleton-ready notification

**Files:**
- Modify: `scripts/emerge_daemon.py` (inside `icc_span_close`, around line 820–828 where `skeleton_path` is set)
- Test: `tests/test_mcp_tools_integration.py`

- [ ] **Step 1: Write the failing test**

```python
def test_span_close_stable_pushes_skeleton_ready_notification(tmp_path):
    """icc_span_close at stable must push a skeleton-ready notification to CC."""
    import os, json, time
    from scripts.emerge_daemon import EmergeDaemon
    from unittest.mock import patch

    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path)
    daemon = EmergeDaemon()

    # Open a span first
    daemon.call_tool("icc_span_open", {"intent_signature": "gmail.read.fetch"})

    notified = []
    daemon._notify = lambda **kw: notified.append(kw)

    # Patch _generate_span_skeleton to return a fake path (so skeleton generation succeeds)
    fake_path = tmp_path / "gmail" / "pipelines" / "read" / "_pending" / "fetch.py"
    fake_path.parent.mkdir(parents=True, exist_ok=True)
    fake_path.write_text("# skeleton")

    with patch.object(daemon, "_generate_span_skeleton", return_value=fake_path), \
         patch.object(daemon._span_tracker, "is_synthesis_ready", return_value=True), \
         patch.object(daemon._span_tracker, "skeleton_already_generated", return_value=False), \
         patch.object(daemon._span_tracker, "latest_successful_span", return_value=object()), \
         patch.object(daemon._span_tracker, "mark_skeleton_generated", return_value=None):
        daemon.call_tool("icc_span_close", {
            "intent_signature": "gmail.read.fetch",
            "outcome": "success",
        })

    os.environ.pop("EMERGE_STATE_ROOT", None)

    assert len(notified) == 1, f"Expected 1 notification, got {notified}"
    n = notified[0]
    assert n["source"] == "span_synthesizer"
    assert n["severity"] == "info"
    assert n["requires_action"] is True
    assert n["intent_signature"] == "gmail.read.fetch"
    assert str(fake_path) in n["content"]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_span_close_stable_pushes_skeleton_ready_notification -xvs
```

Expected: `FAILED` — `notified` is empty

- [ ] **Step 3: Add notification after skeleton generation**

In `scripts/emerge_daemon.py`, after the `if generated:` block (around lines 820–829), add the `_notify` call:

```python
                    if generated:
                        skeleton_path = str(generated)
                        self._span_tracker.mark_skeleton_generated(closed.intent_signature)
                        try:
                            self._sink.emit("span.skeleton_generated", {
                                "intent_signature": closed.intent_signature,
                                "path": skeleton_path,
                            })
                        except Exception:
                            pass
                        # Notify CC so it can review and call icc_span_approve
                        try:
                            self._notify(
                                content=(
                                    f"✅ Pipeline skeleton ready for `{closed.intent_signature}`. "
                                    f"Review `{skeleton_path}`, complete any TODOs, "
                                    "then call `icc_span_approve` to activate the bridge."
                                ),
                                source="span_synthesizer",
                                severity="info",
                                category="action_needed",
                                intent_signature=closed.intent_signature,
                                requires_action=True,
                                extra_meta={"skeleton_path": skeleton_path},
                            )
                        except Exception:
                            pass
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_span_close_stable_pushes_skeleton_ready_notification -xvs
```

Expected: `PASSED`

- [ ] **Step 5: Commit**

```bash
git add scripts/emerge_daemon.py tests/test_mcp_tools_integration.py
git commit -m "feat: push skeleton-ready notification when stable span generates pipeline skeleton"
```

---

### Task 4: Migrate `_push_pattern` and `_on_pending_actions` to `_notify()`

**Files:**
- Modify: `scripts/emerge_daemon.py` (`_push_pattern` ~line 2808, `_on_pending_actions` ~line 2748)
- Test: `tests/test_mcp_tools_integration.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_push_pattern_uses_unified_meta_schema():
    """_push_pattern must use unified meta schema with source/severity/category."""
    from scripts.emerge_daemon import EmergeDaemon
    from scripts.pattern_detector import PatternSummary
    daemon = EmergeDaemon()
    pushed = []
    daemon._write_mcp_push = lambda p: pushed.append(p)

    summary = PatternSummary(
        intent_signature="zwcad.read.state",
        occurrences=5,
        window_minutes=12.0,
        context_hint={"app": "ZWCAD"},
        machine_ids=["m1"],
        policy_stage="explore",
    )
    daemon._push_pattern("explore", {"app": "ZWCAD"}, summary)

    assert len(pushed) == 1
    meta = pushed[0]["params"]["meta"]
    # Unified schema fields must all be present
    assert meta["source"] == "operator_monitor"
    assert meta["severity"] == "info"
    assert meta["category"] == "action_needed"
    assert meta["intent_signature"] == "zwcad.read.state"
    assert meta["requires_action"] is True
    # Legacy fields still present for backwards compat
    assert "policy_stage" in meta
    assert "occurrences" in meta


def test_on_pending_actions_uses_unified_meta_schema(tmp_path):
    """_on_pending_actions notification must use unified meta schema."""
    import json, time, os
    from scripts.emerge_daemon import EmergeDaemon
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path)
    daemon = EmergeDaemon()
    pushed = []
    daemon._write_mcp_push = lambda p: pushed.append(p)

    pending = tmp_path / "pending-actions.json"
    pending.write_text(json.dumps({
        "submitted_at": int(time.time() * 1000),
        "actions": [{"type": "prompt", "prompt": "hello"}],
    }))
    daemon._on_pending_actions()
    os.environ.pop("EMERGE_STATE_ROOT", None)

    assert len(pushed) == 1
    meta = pushed[0]["params"]["meta"]
    assert meta["source"] == "cockpit"
    assert meta["severity"] == "info"
    assert meta["category"] == "action_needed"
    assert meta["requires_action"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_push_pattern_uses_unified_meta_schema tests/test_mcp_tools_integration.py::test_on_pending_actions_uses_unified_meta_schema -xvs
```

Expected: `FAILED` — meta dicts missing `severity`, `category`, `requires_action`

- [ ] **Step 3: Migrate `_push_pattern` to use `_notify()`**

Replace the `_push_pattern` method body in `scripts/emerge_daemon.py`:

```python
    def _push_pattern(self, stage: str, context: dict, summary: Any) -> None:
        """Push pattern detection result to CC via MCP channel notification."""
        message = self._build_explore_message(context, summary)
        self._notify(
            content=message,
            source="operator_monitor",
            severity="info",
            category="action_needed",
            intent_signature=summary.intent_signature,
            requires_action=True,
            extra_meta={
                "policy_stage": stage,
                "occurrences": summary.occurrences,
                "window_minutes": summary.window_minutes,
                "machine_ids": summary.machine_ids,
            },
        )
```

- [ ] **Step 4: Migrate `_on_pending_actions` to use `_notify()`**

In `_on_pending_actions`, replace the `self._write_mcp_push({...})` call (the one with `"notifications/claude/channel"` and `meta: {source: "cockpit", ...}`) with:

```python
        try:
            self._notify(
                content=_format_pending_actions_message(actions),
                source="cockpit",
                severity="info",
                category="action_needed",
                requires_action=True,
                extra_meta={
                    "action_count": len(actions),
                    "action_types": list({a.get("type") for a in actions}),
                },
            )
        except Exception:
            return  # don't advance _last_seen_pending_ts — allow retry
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_push_pattern_uses_unified_meta_schema tests/test_mcp_tools_integration.py::test_on_pending_actions_uses_unified_meta_schema -xvs
```

Expected: both `PASSED`

- [ ] **Step 6: Run full suite**

```bash
python -m pytest tests/ -q --tb=short
```

Expected: same pass count

- [ ] **Step 7: Commit**

```bash
git add scripts/emerge_daemon.py tests/test_mcp_tools_integration.py
git commit -m "refactor: migrate _push_pattern and _on_pending_actions to unified _notify() schema"
```

---

### Task 5: SessionEnd hook

**Files:**
- Create: `hooks/session_end.py`
- Modify: `.claude-plugin/plugin.json` (register SessionEnd hook)
- Create: `tests/test_session_end.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_session_end.py
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run_hook(stdin_payload: dict | None = None) -> dict:
    result = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "session_end.py")],
        input=json.dumps(stdin_payload or {}),
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert result.returncode == 0, f"hook exited {result.returncode}: {result.stderr}"
    return json.loads(result.stdout)


def test_session_end_hook_exits_cleanly():
    """session_end hook must exit 0 and emit valid SessionEnd hookSpecificOutput."""
    out = _run_hook()
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionEnd"


def test_session_end_hook_returns_cleanup_summary():
    """session_end hook output must include a cleanup_performed key."""
    out = _run_hook()
    assert "cleanup_performed" in out["hookSpecificOutput"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_session_end.py -xvs
```

Expected: `FAILED` — `hooks/session_end.py` does not exist

- [ ] **Step 3: Create `hooks/session_end.py`**

```python
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.policy_config import default_hook_state_root, pin_plugin_data_path_if_present  # noqa: E402
from scripts.state_tracker import load_tracker, save_tracker  # noqa: E402


def main() -> None:
    payload_text = sys.stdin.read().strip()
    try:
        payload = json.loads(payload_text) if payload_text else {}
    except Exception:
        payload = {}

    pin_plugin_data_path_if_present()
    state_root = Path(default_hook_state_root())
    state_path = state_root / "state.json"

    cleanup_performed: list[str] = []

    # Clear stale active_span_id — if a span was open when session ended,
    # it is unresovable; SessionStart will also clear it, but belt+suspenders.
    try:
        tracker = load_tracker(state_path)
        if tracker.state.get("active_span_id"):
            tracker.state.pop("active_span_id", None)
            tracker.state.pop("active_span_intent", None)
            save_tracker(state_path, tracker)
            cleanup_performed.append("cleared_active_span")
    except Exception:
        pass

    out = {
        "hookSpecificOutput": {
            "hookEventName": "SessionEnd",
            "cleanup_performed": cleanup_performed,
        }
    }
    print(json.dumps(out))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_session_end.py -xvs
```

Expected: both `PASSED`

- [ ] **Step 5: Register SessionEnd hook in `plugin.json`**

Open `.claude-plugin/plugin.json`. Add `"SessionEnd"` to the `hooks` dict:

```json
{
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
    ]
  }
}
```

- [ ] **Step 6: Run full suite**

```bash
python -m pytest tests/ -q --tb=short
```

Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add hooks/session_end.py tests/test_session_end.py .claude-plugin/plugin.json
git commit -m "feat: add SessionEnd hook — clears stale active_span_id on session close"
```

---

### Task 6: Final integration test + docs update

**Files:**
- Test: `tests/test_mcp_tools_integration.py`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Write an end-to-end notification schema test**

```python
def test_all_notifications_use_unified_meta_fields():
    """All _notify() calls must produce notifications with required meta fields."""
    import json
    from scripts.emerge_daemon import EmergeDaemon

    daemon = EmergeDaemon()
    all_pushed = []
    daemon._write_mcp_push = lambda p: all_pushed.append(p)

    # Trigger bridge notification path directly
    daemon._notify(
        content="test",
        source="bridge",
        severity="high",
        category="warning",
        intent_signature="x.read.y",
    )

    for p in all_pushed:
        meta = p["params"]["meta"]
        for required_field in ("source", "severity", "category", "requires_action"):
            assert required_field in meta, f"Missing {required_field!r} in meta: {meta}"
```

- [ ] **Step 2: Run test to verify it passes**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_all_notifications_use_unified_meta_fields -xvs
```

Expected: `PASSED`

- [ ] **Step 3: Update CLAUDE.md Key Invariants**

Add the following entry to the **Key Invariants** table in `CLAUDE.md`:

```markdown
- **Unified notification meta schema**: All `_write_mcp_push` channel notifications go through `_notify()`. Required meta fields: `source` (bridge|cockpit|operator_monitor|span_synthesizer), `severity` (info|warning|high), `category` (informational|action_needed|warning), `requires_action` (bool). CC uses `severity` and `requires_action` to route and prioritize. Bridge failures use `severity=high`; skeleton-ready and pending-actions use `requires_action=True`.
- **SessionEnd hook** (`hooks/session_end.py`): clears stale `active_span_id` from state.json. Registered in `plugin.json`. Complements `SessionStart` which also clears stale span state.
```

- [ ] **Step 4: Run full suite**

```bash
python -m pytest tests/ -q --tb=short
```

Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add tests/test_mcp_tools_integration.py CLAUDE.md
git commit -m "docs: update CLAUDE.md with unified notification schema and SessionEnd hook invariants"
```
