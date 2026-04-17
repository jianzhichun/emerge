---
name: distilling-operator-flows
description: Use when wiring a new vertical to capture operator actions on a remote runner and distill them into pipelines — the full `observe → detect pattern → crystallize → promote` loop. Complements `initializing-vertical-flywheel` (static assets) and `operator-monitor-debug` (diagnosing a broken loop).
---

# Distilling Operator Flows

## Purpose

Turn live human operator actions on a runner machine into zero-LLM pipelines. This skill covers the write path: **how to instrument, observe, and crystallize**. For pure asset bootstrap see `initializing-vertical-flywheel`; for debugging an already-broken monitoring pipeline see `operator-monitor-debug`.

## End-to-End Loop

```
operator on runner
   ↓  (tray "发送消息" OR pipeline hook OR icc_exec takeover)
event source → POST /runner/event  OR  event_bus.emit_event(...)
   ↓
events/events-{profile}.jsonl   (per-runner, via daemon _on_runner_event)
 OR operator-events/<machine_id>/events.jsonl  (via event_bus helper)
   ↓
PatternDetector.ingest(events)  → PatternSummary when thresholds met
   ↓
Distiller.distill(summary, confirmed=True)  → normalized intent_signature
   ↓
icc_exec with that intent_signature (WAL records code)
   ↓
icc_crystallize → `.py` + `.yaml` under ~/.emerge/connectors/<v>/pipelines/
   ↓
PolicyEngine: explore → canary (5 wins) → stable (window rate ≥ 0.9)
   ↓
span-bridge / exec-bridge: zero-LLM takeover on next match
```

## When to Use

- Adding a new vertical where the operator physically drives an application (ZWCAD, Excel, browser, AutoCAD, etc.) and you want CC to learn from their keystrokes.
- Tuning an existing vertical whose pipelines have stalled at `explore` despite recurring operator activity.
- Debugging why `distiller.distill()` is producing an `intent_signature` you did not expect.

Do **not** use when:
- The runner has not yet been installed — run `initializing-vertical-flywheel` first.
- Pattern detection works but elicitation / channel notify is silent — use `operator-monitor-debug`.

## Key APIs

### Event shape (operator-produced)
```python
{
  "ts_ms": 1776401020761,
  "machine_id": "<stable-host-id>",      # required; path traversal rejected
  "session_role": "operator",             # "monitor_sub" events are filtered out
  "event_type": "entity_added",           # app-specific verb
  "app": "zwcad",                         # used by frequency grouper
  "payload": {                            # free-form; layer/target often grouped
    "layer": "标注",
    "target": "room_7"
  }
}
```

### Three production paths
| Path | Trigger | Destination | Notes |
|---|---|---|---|
| Tray input bubble | Operator clicks "发送消息" in pystray menu on runner | `POST /runner/event` → `events/events-{profile}.jsonl` with `type="operator_message"` | Fastest human → CC channel; `PatternDetector` skips `operator_message` |
| Pipeline hook | Inside pipeline `start()` or `verify()` Python code | `event_bus.emit_event(machine_id, event_type, payload)` → `operator-events/<machine_id>/events.jsonl` | Use when a pipeline itself wants to report operator-observable side effects |
| icc_exec takeover | CC runs exploratory code via `icc_exec` with `intent_signature` | `_write_operator_event` in daemon (`session_role=monitor_sub`) | Filtered by `PatternDetector._frequency_check` — never self-reinforces |

### PatternDetector thresholds (from `scripts/pattern_detector.py`)
- `FREQ_THRESHOLD = 3` events of same `(app, event_type, layer)` tuple
- `FREQ_WINDOW_MS = 20 * 60_000` rolling window
- `ERROR_RATE_THRESHOLD = 0.4` undos / total → fires "error-rate" summary
- `CROSS_MACHINE_MIN_MACHINES = 2`, `MIN_PER_MACHINE = 2` → cross-machine summary

Tune these per-vertical by overriding the class; do **not** edit the base constants — tests lock them.

### Distiller.distill contract (from `scripts/distiller.py`)
- `distill(summary, confirmed=False)` → returns **normalized** `intent_signature` (lowercase, `_` separators, dots preserved, ≤200 chars)
- `confirmed=True` additionally writes `intent_confirmed` event with `session_role="monitor_sub"` into `operator-events/<machine_id>/events.jsonl`
- Input `machine_id` is path-traversal validated; reject `..`, `/`, leading/trailing whitespace

## Step-by-Step: Add a New Vertical Distillation Loop

### 1. Confirm runner readiness
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" runner-status --pretty
# proceed only when Runner reachable: True
```

### 2. Wire the event producer

Choose ONE based on operator UX:

**(a) Tray path** — operator drives UX; already built into `RunnerExecutor._start_tray()`. No code needed; verify icon is present on runner:
```bash
# via icc_exec targeting the runner profile
import os; print(os.environ.get("DISPLAY") or os.environ.get("USERNAME"))
# must be an interactive Session 1 (Windows) or logged-in GUI session
```

**(b) Pipeline hook path** — instrument the pipeline that represents operator work:
```python
# inside a pipeline .py, after a successful operator-observable action
from scripts.event_bus import emit_event
emit_event({
    "session_role": "operator",     # required; PatternDetector skips "monitor_sub"
    "event_type": "entity_added",
    "app": "zwcad",
    "payload": {"layer": layer_name, "target": target_id},
})
# ts_ms and machine_id (socket.gethostname()) are auto-injected when missing.
```

### 3. Observe events accumulating
```bash
# on runner (or via icc_exec to the target profile)
tail -f ~/.emerge/state/events/events-<profile>.jsonl
# OR
tail -f ~/.emerge/operator-events/<machine_id>/events.jsonl
```
Keep this running while the operator performs the flow 3+ times within 20 minutes.

### 4. Verify PatternDetector fires
```python
import json
from pathlib import Path
from scripts.pattern_detector import PatternDetector

events_path = Path.home() / ".emerge/operator-events/<machine_id>/events.jsonl"
events = [json.loads(l) for l in events_path.read_text().splitlines() if l.strip()]
summaries = PatternDetector().ingest(events[-200:])
for s in summaries:
    print(s.intent_signature, s.occurrences, s.detector_signals)
```
If empty:
- Confirm `session_role == "operator"` (not `monitor_sub`) — `_on_runner_event` sets this correctly for tray events; pipeline hooks must set it explicitly.
- Confirm `(app, event_type, layer)` tuple is **stable** across occurrences — varying `layer` splits the group and keeps each bucket below threshold.

### 5. Distill a canonical intent_signature
```python
from scripts.distiller import Distiller
d = Distiller()
sig = d.distill(summaries[0], confirmed=True)
print(sig)  # e.g. "zwcad.entity_added.label_room"
```
The normalizer enforces the `<connector>.(read|write).<name>` convention via post-hoc edits — review and rename segments if needed (PolicyEngine rejects 2-part sigs via `pre_tool_use.py`).

### 6. Record exec WAL with that intent_signature
CC runs exploratory code with the signature:
```
icc_exec(
  intent_signature="zwcad.write.label_room",
  code="<COM driver code>",
  target_profile="<runner>"
)
```
`FlywheelRecorder.record_exec_event` updates session `candidates.json` **and** hands evidence to `PolicyEngine.apply_evidence` in one atomic step.

### 7. Crystallize after 3+ successes
```
icc_crystallize(
  intent_signature="zwcad.write.label_room",
  connector="zwcad",
  pipeline_name="label_room",
  mode="write"
)
```
Writes `~/.emerge/connectors/zwcad/pipelines/write/label_room.{py,yaml}`. The `.py` is WAL-extracted; the `.yaml` metadata is strict YAML (no JSON-style payloads).

### 8. Watch policy promotion
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" control-plane intents --pretty | grep zwcad
```
Thresholds (from `scripts/policy_engine.py`):
- `explore → canary`: 5 successful attempts, `verify_rate ≥ threshold`
- `canary → stable`: window success rate `≥ 0.9`
- `stable → bridge`: immediate next call bypasses LLM via `_try_flywheel_bridge`

## Vertical-Specific Distillation Profiles

Different verticals sit on fundamentally different observation surfaces. Treat the section above as the **skeleton**; pick the matching profile below for parameters + event shape + where `session_role="operator"` actually comes from.

### A. Desktop COM / AX verticals (ZWCAD, AutoCAD, Excel, SolidWorks)

- **Observation surface**: COM object property reads/writes + UIAutomation window events. Thread-local (STA) — reconnect every `icc_exec`.
- **Event source**: pipeline hook path. `start()`/`verify()` in pipeline calls `event_bus.emit_event()` after each COM mutation.
- **Grouping tuple**: `(app, event_type, payload.layer)` — layer/sheet is the natural dimension; don't include entity IDs.
- **Thresholds**: defaults work (`FREQ=3`, `WINDOW=20min`) — operator pace is slow/deliberate.
- **Takeover risk**: high — COM writes are usually irreversible; require `canary` notify + timeout-choice, never silent explore auto-takeover.
- **Intent signature shape**: `<app>.(read|write).<entity>_<action>` — e.g. `zwcad.write.label_room`, `excel.read.pivot_totals`.

### B. Cloud API / SaaS verticals (Lark/Feishu, Notion, Jira, Linear)

- **Observation surface**: MCP tool call results — **not** keystrokes. The operator works inside CC conversation; the "action" is a successful tool response.
- **Event source**: `icc_exec` takeover path. `_write_operator_event` with `session_role="monitor_sub"` so this path is **self-filtered by PatternDetector** — promotion must come from `FlywheelRecorder.record_exec_event` → `PolicyEngine.apply_evidence`, not from pattern detection.
- **Grouping tuple**: N/A — skip `PatternDetector` entirely. Promotion is driven by repeated successful `icc_exec` calls with the same `intent_signature`.
- **Thresholds**: PolicyEngine defaults (5 successes → canary; ≥0.9 window rate → stable). Don't lower; SaaS APIs have clean success signals.
- **Takeover risk**: low for reads, medium for writes. Writes should require `canary` confirmation; reads can go `explore → stable` fast.
- **Intent signature shape**: `<saas>.(read|write).<resource>_<verb>` — e.g. `lark.read.doc_content`, `lark.write.calendar_event`.

### C. Browser / Web-app verticals (Chrome extension, CDP, Playwright)

- **Observation surface**: DOM mutation + navigation events via `mcp__claude-in-chrome__*` or CDP. Much noisier than COM — most events are irrelevant.
- **Event source**: pipeline hook + aggressive pre-filter. Only emit events for semantically meaningful actions (form submit, button click on named target) — not every DOM mutation.
- **Grouping tuple**: `(app=hostname, event_type, payload.selector_hash)` — hash a normalized CSS selector to avoid splitting on dynamic IDs.
- **Thresholds**: **raise** `FREQ_THRESHOLD` to 5 and **shrink** `FREQ_WINDOW_MS` to 10min — web flows repeat faster and noise is higher.
- **Takeover risk**: medium — visible to other users; always `canary` confirm before writes.
- **Intent signature shape**: `<site>.(read|write).<page>_<action>` — e.g. `github.write.pr_comment`, `gmail.read.thread_summary`.

### D. Chat / Meeting / Realtime verticals (Lark VC, Zoom, Slack huddle)

- **Observation surface**: transcript chunks, reaction events, meeting-summary artifacts. Events are **bursty** and cross-session.
- **Event source**: post-meeting artifact ingestion — one batch per meeting, emitted via `event_bus.emit_event` after the artifact is downloaded.
- **Grouping tuple**: `(app, event_type, payload.meeting_type)` — don't group by meeting_id (every meeting is unique; never crosses threshold).
- **Thresholds**: enable **cross-machine** detector (`CROSS_MACHINE_MIN_MACHINES=2`) — one vertical, many operators joining the same meeting type.
- **Takeover risk**: low (summaries are read-only artifacts).
- **Intent signature shape**: `<tool>.read.<artifact>_summary` — e.g. `lark_vc.read.standup_summary`, `zoom.read.transcript_actions`.

### E. Engineering / CLI / shell verticals

- **Observation surface**: shell command history + exit codes. The operator runs a recurring chain of commands (deploy, release, audit).
- **Event source**: shell hook (zsh `precmd`/`preexec` writing to `~/.emerge/operator-events/<machine_id>/events.jsonl`) **or** pipeline hook wrapping known CLI tools.
- **Grouping tuple**: `(app="shell", event_type=command_name, payload.subcommand)` — dedupe by command + first positional arg.
- **Thresholds**: **lower** `FREQ_THRESHOLD` to 2 — engineering flows are rarer but more deliberate.
- **Takeover risk**: high for destructive commands (`kubectl delete`, `git push -f`) — always `canary` confirm; never auto-stable writes that touch shared state.
- **Intent signature shape**: `<tool>.(read|write).<subcmd>` — e.g. `kubectl.read.pod_logs`, `git.write.release_tag`.

### Picking a profile

| Operator UX | Profile | Event path |
|---|---|---|
| Driving a native desktop app with mouse/keyboard | A. Desktop COM/AX | pipeline hook, `session_role=operator` |
| Typing into CC, expecting API results | B. Cloud API/SaaS | `icc_exec` takeover, `session_role=monitor_sub`, no PatternDetector |
| Clicking around a web app in Chrome | C. Browser/Web | pipeline hook, pre-filtered DOM events |
| Downloading meeting transcripts / batch artifacts | D. Chat/Meeting | cross-machine detector, batch ingestion |
| Running shell commands in a loop | E. CLI/shell | shell hook or pipeline wrapper, low freq threshold |

When in doubt: profile B is the safest default — works for any vertical with a clean API surface, skips pattern detection, relies purely on PolicyEngine's exec-event counting.

## Common Pitfalls

| Symptom | Root cause | Fix |
|---|---|---|
| `PatternDetector` never fires | `session_role=monitor_sub` on producer side | Pipeline hook must set `session_role="operator"` explicitly |
| Signatures like `unknown.pattern` | `PatternSummary.intent_signature` was empty before distill | Fix the detector's naming rule, not the distiller |
| intent stuck in explore despite 10+ runs | `verify_observed=True` but `verify_rate < 0.9` | Add real `verify()` checks to the pipeline, not stubs |
| `canary → explore` ping-pong | `two_consecutive_failures` demotion | Use cockpit `/api/control-plane/intent-history` to inspect `last_demotion.reason` |
| Bridge failure warning but counters unchanged | Correct by design — `_try_flywheel_bridge` surfaces telemetry only; subsequent `icc_exec` fallback produces the authoritative evidence | No action; bridge is honest about failure |

## Files Touched

- `scripts/pattern_detector.py` — detector thresholds + `PatternSummary` dataclass
- `scripts/distiller.py` — `Distiller.distill()` + normalization rules
- `scripts/event_bus.py` — `emit_event()` pipeline hook helper
- `scripts/remote_runner.py` — `RunnerExecutor._start_tray()` + `_post_operator_message`
- `scripts/mcp/flywheel_recorder.py` — `record_exec_event` / `record_pipeline_event` → `PolicyEngine.apply_evidence`
- `scripts/policy_engine.py` — the single writer of `entry["stage"]`

## Related Skills

- `initializing-vertical-flywheel` — bootstrap the runner + static pipeline assets (run first)
- `operator-monitor-debug` — diagnose when this loop breaks
- `remote-runner-dev` — deploy/redeploy runner code after edits
- `policy-optimization` — tune promotion thresholds once distillation works
