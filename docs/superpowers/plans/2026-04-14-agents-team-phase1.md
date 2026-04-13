# Agents-Team Mode — Phase 1 (MVP) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable a team-lead CC session to spawn one per-runner watcher agent, each receiving isolated pattern alerts and applying the stage→action popup protocol.

**Architecture:** Three changes wire the per-runner routing: (1) `OperatorMonitor._poll_machine` stamps `runner_profile` into the context dict; (2) `_push_pattern` writes `pattern-alerts-{runner_profile}.json` instead of a shared file; (3) `watch_patterns.py` accepts `--runner-profile` so each watcher agent monitors only its own file. A new `/emerge:monitor` command and an updated `commands/cockpit.md` document the team orchestration and stage→action protocol.

**Tech Stack:** Python 3.10+, pytest, existing emerge daemon/monitor/runner stack, CC `TeamCreate`/`Agent` tools.

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `scripts/operator_monitor.py` | Modify | Stamp `runner_profile` into context before calling `push_fn` |
| `scripts/emerge_daemon.py` | Modify | `_push_pattern` routes alert to per-runner file |
| `scripts/watch_patterns.py` | Modify | Accept `--runner-profile` arg; watch per-runner file |
| `commands/cockpit.md` | Modify | Add stage→action protocol; update Monitor 2 note |
| `commands/monitor.md` | Create | New `/emerge:monitor` command — team spawn + shutdown |
| `tests/test_operator_monitor.py` | Modify | Test `runner_profile` injected into context |
| `tests/test_mcp_tools_integration.py` | Modify | Test per-runner alert file routing |

---

## Task 1: OperatorMonitor stamps runner_profile into context

**Files:**
- Modify: `scripts/operator_monitor.py` (lines around `_poll_machine`)
- Modify: `tests/test_operator_monitor.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_operator_monitor.py`:

```python
def test_poll_machine_injects_runner_profile_into_context(tmp_path):
    """runner_profile key in context must equal the machines-dict key."""
    captured: list[dict] = []

    def fake_push(stage: str, context: dict, summary) -> None:
        captured.append(context)

    now_ms = int(time.time() * 1000)
    events = [
        {
            "ts_ms": now_ms - i * 60_000,
            "machine_id": "workstation-A",
            "session_role": "operator",
            "event_type": "entity_added",
            "app": "zwcad",
            "payload": {"layer": "标注", "content": f"room_{i}"},
        }
        for i in range(3)
    ]

    monitor = OperatorMonitor(
        machines={"mycader-1": _FakeRunnerClient(events)},
        push_fn=fake_push,
        poll_interval_s=0.05,
        event_root=tmp_path / "operator-events",
        adapter_root=tmp_path / "adapters",
    )
    monitor.start()
    time.sleep(0.3)
    monitor.stop()

    assert len(captured) >= 1
    assert captured[0].get("runner_profile") == "mycader-1"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_operator_monitor.py::test_poll_machine_injects_runner_profile_into_context -q
```

Expected: FAIL — `assert captured[0].get("runner_profile") == "mycader-1"` → `None != "mycader-1"`

- [ ] **Step 3: Add runner_profile injection in _poll_machine**

In `scripts/operator_monitor.py`, locate `_poll_machine`. Add one line before `self._push_fn(...)`:

```python
def _poll_machine(self, machine_id: str, client: Any) -> None:
    since_ms = self._last_poll_ms.get(machine_id, 0)
    events = client.get_events(machine_id=machine_id, since_ms=since_ms)

    if events:
        latest_ts = max(e.get("ts_ms", 0) for e in events)
        self._last_poll_ms[machine_id] = latest_ts

        buf = self._event_buffers.setdefault(machine_id, deque())
        buf.extend(events)

    buf = self._event_buffers.get(machine_id)
    if not buf:
        return

    now_ms = int(time.time() * 1000)
    window_ms = PatternDetector.FREQ_WINDOW_MS
    while buf and now_ms - buf[0].get("ts_ms", 0) > window_ms:
        buf.popleft()

    if not buf:
        return

    summaries = self._detector.ingest(list(buf))
    for summary in summaries:
        app = summary.context_hint.get("app", machine_id)
        plugin = self._adapter_registry.get_plugin(app)
        try:
            context = plugin.get_context(summary.context_hint)
        except Exception:
            context = summary.context_hint.copy()
        context["runner_profile"] = machine_id   # ← ADD THIS LINE
        self._push_fn(summary.policy_stage, context, summary)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_operator_monitor.py::test_poll_machine_injects_runner_profile_into_context -q
```

Expected: PASS

- [ ] **Step 5: Run full test suite to check no regressions**

```bash
python -m pytest tests/test_operator_monitor.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/operator_monitor.py tests/test_operator_monitor.py
git commit -m "feat: OperatorMonitor stamps runner_profile into pattern context"
```

---

## Task 2: _push_pattern routes to per-runner alert file

**Files:**
- Modify: `scripts/emerge_daemon.py` (`_push_pattern` method, ~line 2980)
- Modify: `tests/test_mcp_tools_integration.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_mcp_tools_integration.py`:

```python
import json as _json

def test_push_pattern_writes_per_runner_alert_file(tmp_path):
    """When context carries runner_profile, alert goes to pattern-alerts-{profile}.json."""
    daemon = EmergeDaemon(root=tmp_path)
    from scripts.pattern_detector import PatternSummary

    summary = PatternSummary(
        machine_ids=["workstation-A"],
        intent_signature="hypermesh.mesh.batch",
        occurrences=5,
        policy_stage="canary",
        context_hint={"app": "hypermesh"},
        window_minutes=10.0,
    )
    context = {"runner_profile": "mycader-1", "app": "hypermesh"}
    daemon._push_pattern("canary", context, summary)

    alert_file = daemon._state_root / "pattern-alerts-mycader-1.json"
    assert alert_file.exists(), "per-runner alert file not created"
    data = _json.loads(alert_file.read_text())
    assert data["runner_profile"] == "mycader-1"
    assert data["machine_id"] == "workstation-A"
    assert data["stage"] == "canary"
    assert data["intent_signature"] == "hypermesh.mesh.batch"


def test_push_pattern_falls_back_to_shared_file_when_no_runner_profile(tmp_path):
    """Without runner_profile in context, alert goes to pattern-alerts.json."""
    daemon = EmergeDaemon(root=tmp_path)
    from scripts.pattern_detector import PatternSummary

    summary = PatternSummary(
        machine_ids=["m1"],
        intent_signature="zwcad.draw.line",
        occurrences=3,
        policy_stage="explore",
        context_hint={"app": "zwcad"},
        window_minutes=5.0,
    )
    daemon._push_pattern("explore", {"app": "zwcad"}, summary)

    fallback_file = daemon._state_root / "pattern-alerts.json"
    assert fallback_file.exists(), "fallback alert file not created"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_push_pattern_writes_per_runner_alert_file tests/test_mcp_tools_integration.py::test_push_pattern_falls_back_to_shared_file_when_no_runner_profile -q
```

Expected: both FAIL — per-runner file not created (current code always writes `pattern-alerts.json`).

- [ ] **Step 3: Update _push_pattern in emerge_daemon.py**

Locate `def _push_pattern` (~line 2980). Replace the body:

```python
def _push_pattern(self, stage: str, context: dict, summary: Any) -> None:
    """Push pattern detection result via file-based alert (watch_patterns.py Monitor).

    Writes pattern-alerts-{runner_profile}.json when runner_profile is present in
    context (set by OperatorMonitor._poll_machine), otherwise falls back to
    pattern-alerts.json for local/legacy setups.
    """
    import time as _time
    runner_profile = str(context.get("runner_profile", "")).strip()
    filename = (
        f"pattern-alerts-{runner_profile}.json"
        if runner_profile
        else "pattern-alerts.json"
    )
    machine_id = summary.machine_ids[0] if summary.machine_ids else ""
    message = self._build_explore_message(context, summary)
    alert_data = {
        "submitted_at": int(_time.time() * 1000),
        "stage": stage,
        "intent_signature": summary.intent_signature,
        "runner_profile": runner_profile,
        "machine_id": machine_id,
        "message": message,
        "meta": {
            "occurrences": summary.occurrences,
            "window_minutes": summary.window_minutes,
            "machine_ids": summary.machine_ids,
        },
    }
    try:
        self._write_json(self._state_root / filename, alert_data)
    except Exception:
        pass
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_push_pattern_writes_per_runner_alert_file tests/test_mcp_tools_integration.py::test_push_pattern_falls_back_to_shared_file_when_no_runner_profile -q
```

Expected: both PASS.

- [ ] **Step 5: Run full integration tests**

```bash
python -m pytest tests/test_mcp_tools_integration.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/emerge_daemon.py tests/test_mcp_tools_integration.py
git commit -m "feat: _push_pattern routes alerts to per-runner file"
```

---

## Task 3: watch_patterns.py accepts --runner-profile

**Files:**
- Modify: `scripts/watch_patterns.py`
- Modify: `tests/test_mcp_tools_integration.py` (one new test)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_mcp_tools_integration.py`:

```python
def test_watch_patterns_profile_arg_selects_correct_file(tmp_path, monkeypatch):
    """watch_patterns.py --runner-profile mycader-1 watches pattern-alerts-mycader-1.json."""
    import subprocess, sys, time as _time, json as _j

    alert_file = tmp_path / "pattern-alerts-mycader-1.json"
    # Start watcher pointing at tmp_path
    env = {
        **__import__("os").environ,
        "EMERGE_STATE_ROOT": str(tmp_path),
    }
    proc = subprocess.Popen(
        [sys.executable, "scripts/watch_patterns.py", "--runner-profile", "mycader-1"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    _time.sleep(0.3)
    # Write the alert file
    alert_file.write_text(_j.dumps({
        "stage": "canary",
        "intent_signature": "hypermesh.mesh.batch",
        "runner_profile": "mycader-1",
        "machine_id": "ws-A",
        "meta": {"occurrences": 5, "window_minutes": 10, "machine_ids": ["ws-A"]},
    }))
    _time.sleep(0.5)
    proc.terminate()
    out = proc.stdout.read().decode()
    assert "mycader-1" in out or "hypermesh.mesh.batch" in out, (
        f"watcher did not output alert content; got: {out!r}"
    )
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_watch_patterns_profile_arg_selects_correct_file -q
```

Expected: FAIL — script ignores `--runner-profile`, watches wrong file.

- [ ] **Step 3: Update watch_patterns.py**

Replace the entire file content:

```python
#!/usr/bin/env python3
"""Watch for operator-monitor pattern alerts and emit formatted lines to stdout.

Designed to be launched via CC's Monitor tool::

    Monitor(command="python3 .../watch_patterns.py --runner-profile mycader-1",
            description="operator pattern alert watcher — mycader-1",
            persistent=true)

Without --runner-profile, falls back to watching pattern-alerts.json (legacy).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.pending_actions import format_pattern_alert  # noqa: E402
from scripts.watch_file import run_watcher  # noqa: E402


def _state_root() -> Path:
    env = os.environ.get("EMERGE_STATE_ROOT") or os.environ.get("CLAUDE_PLUGIN_DATA")
    if env:
        return Path(env)
    return Path.home() / ".emerge" / "repl"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Watch emerge pattern alerts for one runner.")
    p.add_argument(
        "--runner-profile",
        default="",
        help="Profile name to scope alert file (e.g. mycader-1). "
             "Omit to watch the shared pattern-alerts.json fallback.",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    profile = args.runner_profile.strip()
    filename = f"pattern-alerts-{profile}.json" if profile else "pattern-alerts.json"
    run_watcher(_state_root() / filename, format_pattern_alert)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_watch_patterns_profile_arg_selects_correct_file -q
```

Expected: PASS.

- [ ] **Step 5: Run full suite**

```bash
python -m pytest tests -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/watch_patterns.py tests/test_mcp_tools_integration.py
git commit -m "feat: watch_patterns.py accepts --runner-profile for per-runner isolation"
```

---

## Task 4: Update commands/cockpit.md — stage→action protocol + per-runner Monitor

**Files:**
- Modify: `commands/cockpit.md`

No test needed (documentation change); verify by reading the file after edit.

- [ ] **Step 1: Read current Monitor 2 section**

```bash
grep -n "Monitor 2\|watch_patterns\|pattern alert\|stage\|runner.profile" commands/cockpit.md | head -20
```

Note the line numbers of the Monitor 2 block and the end of step 4.

- [ ] **Step 2: Update Monitor 2 launch command**

Find the Monitor 2 block in step 4. The launch command currently is:
```
python3 "/Users/apple/.claude/plugins/cache/emerge/emerge/0.3.65/scripts/watch_patterns.py"
```

For per-runner watcher agents, this becomes parameterized. Add a note after the existing Monitor 2 block:

```markdown
   > **Per-runner agents-team mode**: when spawning a watcher agent for a specific
   > runner profile (e.g. `mycader-1`), launch Monitor 2 as:
   > ```
   > python3 ".../watch_patterns.py --runner-profile mycader-1"
   > ```
   > description: "operator pattern alert watcher — mycader-1"
   > Each watcher receives only alerts for its assigned runner.
```

- [ ] **Step 3: Add stage→action protocol section to step 4**

After the Monitor 2 block, insert a new subsection:

```markdown
   **Stage → Action protocol** (for watcher agents handling pattern alerts):

   | `stage` | Action |
   |---|---|
   | `explore` | Silent — record intent only, no popup |
   | `canary` | `runner_client.notify({"type":"choice","title":"emerge — 可以接管了","body":f"[{intent_signature}] 已见 {occurrences} 次，接管此次操作？","options":["接管","跳过","停止学习"],"timeout_s":15})` |
   | `stable` | `icc_exec(intent_signature=...)` silently; optional info notify after |

   **AI-initiated popups** (any stage, when agent is uncertain or wants to distill knowledge):
   ```python
   runner_client.notify({
       "type": "input",
       "title": "emerge — 需要确认",
       "body": "<question>",
   })
   # action=confirmed, value=<operator answer>
   # → append answer to NOTES.md via notes-comment action
   ```

   **Silence principle**: only interrupt for authorization (canary takeover) or genuine
   ambiguity. Never popup for: execution in progress/completed, read-only queries,
   errors CC can resolve autonomously.
```

- [ ] **Step 4: Verify the file looks correct**

```bash
grep -n "stage\|action protocol\|runner.profile\|canary\|explore\|stable" commands/cockpit.md | head -30
```

- [ ] **Step 5: Commit**

```bash
git add commands/cockpit.md
git commit -m "docs: cockpit — per-runner Monitor 2 and stage→action protocol"
```

---

## Task 5: Create commands/monitor.md — /emerge:monitor command

**Files:**
- Create: `commands/monitor.md`

- [ ] **Step 1: Read plugin.json to understand command registration format**

```bash
cat .claude-plugin/plugin.json
```

Commands in `commands/` are auto-registered as `/emerge:<name>` skills. `monitor.md` becomes `/emerge:monitor`.

- [ ] **Step 2: Create commands/monitor.md**

```markdown
# /emerge:monitor — Agents-Team Monitor Mode

Spawn a per-runner watcher agent for every configured runner. Each watcher
monitors its runner's EventBus for patterns and applies the stage→action popup
protocol autonomously.

Always invoke the admin CLI via the **Emerge plugin root**.

## Steps

1. **Read configured runner profiles**:
   ```
   python3 "/Users/apple/.claude/plugins/cache/emerge/emerge/0.3.65/scripts/repl_admin.py" runner-status --pretty
   ```
   Extract the list of profile names (e.g. `mycader-1`, `mycader-2`).
   If no runners are configured, inform the user and stop.

2. **Create the monitor team**:
   ```python
   TeamCreate(team_name="emerge-monitors", description="Per-runner pattern watchers")
   ```

3. **Spawn one watcher per runner profile** (replace `{profile}` for each):
   ```python
   Agent(
       subagent_type="general-purpose",
       team_name="emerge-monitors",
       name="{profile}-watcher",
       prompt="""
   You are an emerge vertical monitor agent for runner: {profile}.

   Setup:
   1. Start Monitor:
      command: python3 "/Users/apple/.claude/plugins/cache/emerge/emerge/0.3.65/scripts/watch_patterns.py --runner-profile {profile}"
      description: "pattern alert watcher — {profile}"
      persistent: true
   2. You are now idle, waiting for pattern alerts.

   On [OperatorMonitor] alert notification:
   - Read `stage` and `intent_signature` from the alert.
   - Apply the stage→action protocol:
     - stage=explore  → silent; record intent in notes if new
     - stage=canary   → call runner notify:
         runner_client.notify via icc_exec(intent_signature="{profile}.popup.ask",
         script="from scripts.runner_client import RunnerRouter; ...")
         OR use the /notify endpoint directly if runner_client is available.
         ui_spec: {type:choice, title:"emerge — 可以接管了",
                   body:f"[{intent_signature}] 已见 {occurrences} 次，接管？",
                   options:["接管","跳过","停止学习"], timeout_s:15}
         接管 → icc_exec(intent_signature=...)
         停止学习 → pipeline freeze via repl_admin
     - stage=stable   → icc_exec(intent_signature=...) silently
   - SendMessage(team_lead, summary of action taken)
   - Return to idle.

   On shutdown_request from team lead:
   - Stop your Monitor.
   - Exit cleanly.
   """
   )
   ```
   Repeat Agent spawn for each profile found in step 1.

4. **Confirm team is running**:
   Report to operator: team `emerge-monitors` created with N watchers.
   List each watcher name and its assigned runner profile.

5. **Shutdown** (when operator says stop/exit monitors):
   ```python
   SendMessage(to="all", message={"type": "shutdown_request"})
   # Wait ~5s for confirmations, then:
   TeamDelete()
   ```

## Dynamic addition

When a new runner is bootstrapped mid-session:
```python
Agent(team_name="emerge-monitors", name="{new_profile}-watcher", prompt=<same as above>)
```
No need to recreate the team.

## Notes

- Each watcher's Monitor fires as a notification in the watcher's own conversation.
- The team lead does not process pattern alerts directly — that is the watcher's job.
- Knowledge distillation answers (from input popups) are written to
  `~/.emerge/connectors/{connector}/NOTES.md` via `notes-comment` cockpit action.
```

- [ ] **Step 3: Verify file was created**

```bash
cat commands/monitor.md | head -5
```

- [ ] **Step 4: Commit**

```bash
git add commands/monitor.md
git commit -m "feat: add /emerge:monitor command for agents-team spawn"
```

---

## Task 6: Bump version and update CLAUDE.md + README.md

**Files:**
- Modify: `.claude-plugin/plugin.json` (version bump)
- Modify: `CLAUDE.md` (architecture + key invariants)
- Modify: `README.md` (component table)

- [ ] **Step 1: Bump plugin version to 0.3.66**

In `.claude-plugin/plugin.json`, change `"version": "0.3.65"` → `"version": "0.3.66"`.

- [ ] **Step 2: Add to CLAUDE.md Architecture section**

After the `**OperatorMonitor** auto-starts...` invariant, add:

```
**Per-runner alert routing**: `OperatorMonitor._poll_machine` stamps `context["runner_profile"] = machine_id` (the machines-dict key, which is the runner profile name). `_push_pattern` writes to `pattern-alerts-{runner_profile}.json` when present, falling back to `pattern-alerts.json`. `watch_patterns.py --runner-profile <name>` watches only the scoped file. Each watcher agent in an agents-team monitors its own file.

**Agents-team mode**: `/emerge:monitor` command creates a `TeamCreate("emerge-monitors")` team and spawns one `{profile}-watcher` subagent per runner. Each watcher runs a persistent Monitor on `pattern-alerts-{profile}.json` and applies the stage→action protocol (explore=silent, canary=notify+choice, stable=silent exec). New runners can be added dynamically without recreating the team.
```

- [ ] **Step 3: Add to CLAUDE.md Key Invariants**

Add entry:

```
- **Pattern alert routing invariant**: `runner_profile` in context is the machines-dict key (profile name), not the actual machine hostname. `pattern-alerts-{runner_profile}.json` is the per-runner scoped file. When `runner_profile` is empty (local-only or legacy), alert falls back to `pattern-alerts.json`. Both files use atomic write (temp+rename via `_write_json`).
```

- [ ] **Step 4: Add to README.md component table**

In the component table, add or update the OperatorMonitor row to mention per-runner routing, and add an Agents-Team row referencing `/emerge:monitor`.

- [ ] **Step 5: Run full test suite one final time**

```bash
python -m pytest tests -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add .claude-plugin/plugin.json CLAUDE.md README.md
git commit -m "chore: bump to 0.3.66; document agents-team per-runner routing"
```

---

## Self-Review

**Spec coverage check:**

| Spec section | Task |
|---|---|
| 2.1 machine_id → runner_profile mapping | Task 1 (stamped into context in _poll_machine) |
| 2.2 Per-runner alert files + runner_profile field | Task 2 |
| 2.3 watch_patterns.py parameterized | Task 3 |
| 3.1–3.6 Team orchestration + dynamic addition | Task 5 (monitor.md) |
| 4. Stage→action protocol | Task 4 (cockpit.md) + Task 5 (monitor.md prompt) |
| Phase 2 (tray companion) | Separate plan: `2026-04-14-agents-team-phase2.md` |

**Note on runner-machine-map.json**: the spec describes persisting a `runner-machine-map.json` file. This plan uses the simpler approach of stamping `runner_profile` directly into the context (the machines-dict key IS the profile name). No separate map file is needed because `OperatorMonitor` already has the profile→client mapping in `self._machines`. This is functionally equivalent with less code.
