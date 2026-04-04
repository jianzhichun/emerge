# Operator Intelligence Loop — Design Spec

**Date**: 2026-04-04  
**Status**: Approved for implementation

---

## 1. Problem & Goals

The forward flywheel (Solo Flywheel) crystallizes *AI actions* into deterministic pipelines. But the AI only acts when prompted. Humans at a workstation are acting continuously — and repetitively. The Operator Intelligence Loop is the reverse flywheel: it observes the human operator, detects repeated patterns, surfaces a dialog ("you've done this 8 times today — want me to take it?"), captures intent, and progressively hands the task to the AI layer.

**Goals:**
- Observe operator behavior on any machine (local or remote), across any application
- Detect high-frequency, high-value, or error-prone patterns without vertical-specific hardcoding
- Engage the operator with a native CC dialog at the right moment
- Distill confirmed intents into `intent_signature` values that feed the existing forward flywheel
- Allow vertical specialization (ZWCAD COM, Excel, browser) via the same crystallization mechanism as pipelines

**Non-goals:**
- Silent takeover without operator confirmation (no proactive wakeup capability yet)
- Cross-org pattern sharing (deferred to Memory Hub roadmap item)
- Windows service / background daemon independent of remote_runner

---

## 2. ObserverPlugin — Generic Framework First

### 2.1 Interface

All observation capability is implemented as `ObserverPlugin` subclasses, mirroring the `Pipeline` contract:

```python
class ObserverPlugin:
    """ABC for operator behavior observation. Generic first, vertical via crystallization."""

    def start(self, config: dict) -> None:
        """Begin monitoring. Called once when OperatorMonitor activates this plugin."""

    def stop(self) -> None:
        """Stop monitoring. Clean up listeners, threads, COM objects."""

    def get_context(self, hint: dict) -> dict:
        """
        Pre-elicitation context read. Called after PatternDetector fires, before
        ElicitRequest is sent. Returns enriched context for the elicitation message.
        Example: { total_rooms: 7, labeled: 4, unlabeled: 3, positions: [...] }
        """

    def execute(self, intent: str, params: dict) -> dict:
        """
        Takeover execution. Called after operator confirms via elicitation.
        Returns { ok: bool, summary: str, ... }.
        """
```

### 2.2 Built-in Generic Observers

Three observers ship with Emerge, requiring no vertical knowledge:

| Observer | Mechanism | Events Produced |
|----------|-----------|-----------------|
| `accessibility.py` | macOS AX API / Win UIAutomation | focus_change, text_input, ui_interaction |
| `filesystem.py` | watchdog file watcher | file_modified, file_created, file_renamed |
| `clipboard.py` | OS clipboard polling | clipboard_change |

These provide broad coverage across any application. `get_context()` returns window title + focused element text. `execute()` is no-op (generic observers cannot perform app-specific writes).

### 2.3 Vertical Adapters via Crystallization

Vertical adapters (ZWCAD COM, Excel win32com, browser CDP) are **not built into the framework**. They are crystallized from WAL history via `icc_crystallize mode=adapter`, written to `~/.emerge/adapters/<vertical>/adapter.py`, and loaded by `AdapterRegistry` at runtime.

Bootstrap path for a new vertical:
1. Generic observer detects broad pattern (e.g., repeated text input in ZWCAD window)
2. CC executes `icc_exec` with LLM-generated COM code → WAL records the path
3. After sufficient WAL depth: `icc_crystallize mode=adapter connector=zwcad` generates `adapter.py`
4. `AdapterRegistry` loads it; subsequent runs use COM directly — richer events + full `execute()`

This mirrors exactly how connector pipelines work. The `writing-vertical-adapter` skill documents the `ObserverPlugin` interface and crystallization workflow.

### 2.4 EventBus Format

Each machine writes operator events to `~/.emerge/operator-events/<machine_id>/events.jsonl` (one JSON object per line, append-only, rotated daily):

```json
{
  "ts_ms": 1743734400000,
  "machine_id": "cad-win-01",
  "session_id": "op_abc",
  "session_role": "operator",
  "observer_type": "zwcad_com",
  "event_type": "entity_added",
  "app": "zwcad",
  "payload": {
    "entity_type": "AcDbText",
    "content": "三室两厅",
    "layer": "标注",
    "position": [4250.0, 3180.0],
    "drawing": "project_221.dwg"
  }
}
```

`session_role` is `"operator"` for human sessions and `"monitor_sub"` for CC subagent sessions. PatternDetector filters out `monitor_sub` entries to prevent AI self-monitoring.

remote_runner exposes two endpoints:
- `POST /operator-event` — AppAdapter writes a single event
- `GET /operator-events?since_ms=N&limit=100` — OperatorMonitor polls

---

## 3. PatternDetector

Runs inside EmergeDaemon's `OperatorMonitor` thread. Receives event batches, applies pluggable detector strategies, emits `PatternSummary` when a threshold is crossed.

### 3.1 Detector Strategies

| Strategy | Signal | Threshold (default) |
|----------|--------|---------------------|
| **Frequency** | N same event_type+layer+app in time window | 3 occurrences / 20 min |
| **Time** | Operator spends >T minutes on a single repeated task | 5 min cumulative |
| **Error-rate** | Undo/redo ratio on a sequence exceeds threshold | >2 undos per 5 ops |
| **Similarity** | NLP similarity clustering of text content | cosine > 0.8, cluster size ≥ 3 |
| **Cross-machine** | Same pattern on ≥2 machines in same window | any 2 machines |

Strategies are pluggable: additional detectors can be registered as Python callables in `~/.emerge/detectors/`.

### 3.2 PatternSummary

```json
{
  "machine_ids": ["cad-win-01"],
  "intent_signature": "zwcad.annotate.room_labels",
  "occurrences": 4,
  "window_minutes": 19,
  "detector_signals": ["frequency", "similarity"],
  "context_hint": {
    "app": "zwcad",
    "drawing": "project_221.dwg",
    "layer": "标注",
    "samples": ["主卧", "次卧", "客厅", "三室两厅"]
  },
  "policy_stage": "explore"
}
```

`intent_signature` is derived by Distiller (see §5). `policy_stage` reflects the current stage of the corresponding candidate in the policy registry.

---

## 4. Trigger Mechanism — MCP Push, No CC-Side Polling

This is the architectural core. EmergeDaemon already runs as a persistent MCP server with a live connection to CC. The MCP protocol supports server-initiated requests. No CC-side polling, no subagents, no hooks required.

### 4.1 Two Push Mechanisms

| Mechanism | MCP Method | Effect in CC | Used For |
|-----------|-----------|--------------|---------|
| **Channel notification** | `notifications/claude/channel` | Injects prompt string into CC command queue (`priority: next`) | Explore stage — LLM evaluates and decides |
| **ElicitRequest** | `elicit` (server-initiated MCP request) | Shows native blocking dialog in ~16ms via `AppState.elicitation.queue` | Canary / Stable — structured confirm |

Source: `useManageMCPConnections.ts:507-532` (channel), `elicitationHandler.ts:68-172` (elicit).

### 4.2 Pre-Elicitation Context Read

Before sending either push, OperatorMonitor calls `adapter.get_context(hint)` on the relevant machine:

```python
# hint from PatternSummary
context = adapter.get_context({
    "app": "zwcad",
    "drawing": "project_221.dwg",
    "layer": "标注"
})
# context = { total_rooms: 7, labeled: 4, unlabeled: 3, unlabeled_positions: [...] }
```

This ensures the message shown to the operator contains specific, credible facts ("3 rooms unlabeled") rather than vague pattern descriptions. If the vertical adapter is not yet crystallized, generic observer `get_context()` returns window title + clipboard — less precise but still useful.

### 4.3 Three-Stage Interaction Flow

#### Explore — Channel notification

Daemon sends a channel notification. CC injects it as a prompt; the LLM turn evaluates whether the pattern is worth pursuing and optionally initiates elicitation itself.

```
notification payload:
  "检测到 project_221.dwg 中反复手动添加房间标注
   (主卧/次卧/客厅/三室两厅，共 4 次，19 分钟内)。
   还有 3 个房间未标注。评估是否值得接管。"
```

LLM can choose to: fire an ElicitRequest immediately, ask a clarifying question, or log and wait for more data.

#### Canary — ElicitRequest form

Daemon sends a structured `ElicitRequest`. CC renders it as a blocking modal dialog. Form fields:

```json
{
  "message": "project_221.dwg 共 7 个房间，你已标注 4 个，还有 3 个未标注。是否让我接管剩余标注？",
  "requestedSchema": {
    "action": {
      "type": "string",
      "oneOf": [
        { "const": "yes", "title": "是，帮我标注剩余房间" },
        { "const": "later", "title": "稍后再说" },
        { "const": "no", "title": "不需要，我自己来" }
      ]
    },
    "note": { "type": "string", "description": "补充说明（可选）", "maxLength": 200 }
  }
}
```

**Timeout**: `ElicitRequest` is a blocking MCP request — the daemon awaits the response. Daemon sets a 30-second timeout on its MCP client. On timeout, the transport returns an error; daemon treats this as implicit accept and proceeds with takeover. The CC dialog remains visible and will resolve whenever the operator interacts with it (their response is recorded for future preference tuning but does not block execution).

#### Stable — ElicitRequest, lightweight

Same form as canary. Shorter message ("同一操作，确认接管？"), `note` field omitted. Timeout reduced to 10 seconds. No auto-countdown UI — the message states the timeout period. Operator can dismiss to prevent takeover; silence = accept.

### 4.4 operator_popup.py (Non-CC Machines)

For machines running the remote_runner but without a Claude Code session (e.g., a pure ZWCAD workstation), `operator_popup.py` provides a standalone tkinter dialog. It is triggered via `icc_exec` targeting that machine's profile. Results are written back to EventBus as `intent_confirmed` events. This is a fallback path only — the primary path is always MCP ElicitRequest.

---

## 5. Distiller & Flywheel Integration

### 5.1 Distiller

When an operator confirms via elicitation, Distiller generates an `intent_signature` from the PatternSummary and stores a confirmed intent event in EventBus:

```python
# Distiller generates: "{app}.{domain}.{action}"
# e.g., "zwcad.annotate.room_labels"
```

Confirmed intents are passed to the existing policy registry as `icc_exec` candidates. The Distiller does not invent new flywheel logic — it feeds the same `candidates.json` / `pipelines-registry.json` that the forward flywheel uses.

### 5.2 Takeover Execution

After operator confirms:
1. Daemon calls `adapter.execute(intent_signature, context)` on the target machine via `icc_exec`
2. Execution path enters WAL
3. Policy registry updates candidate counters
4. On crystallization: `adapter.py` is updated with the execution path

### 5.3 Integration with `initializing-vertical-flywheel` Skill

The skill's final step (triggered when any `intent_signature` for a vertical reaches `stable`) will prompt:

> "你已有稳定的 `zwcad.*` 管道飞轮。是否也建立反向飞轮来观察操作者行为，让 AI 主动接管重复操作？"

If yes → invoke `writing-vertical-adapter` skill. This makes the two flywheels naturally complementary — you discover how to DO a task (forward), then you learn to recognize when humans are doing it repeatedly (reverse).

---

## 6. OperatorMonitor Component

New component added to EmergeDaemon. Runs as a background thread, does not block MCP request handling.

### 6.1 Lifecycle

```
EMERGE_OPERATOR_MONITOR=1  →  OperatorMonitor starts with daemon

OperatorMonitor:
  AdapterRegistry.load()  →  loads ~/.emerge/adapters/*/adapter.py
  for each configured machine:
    loop every 5s:
      events = GET /operator-events?since_ms=last_poll
      PatternDetector.ingest(events)
      if PatternSummary emitted:
        context = adapter.get_context(hint)
        push_to_cc(stage, context, summary)
```

### 6.2 Push to CC

```python
def push_to_cc(stage, context, summary):
    if stage == "explore":
        # MCP channel notification → CC command queue
        send_channel_notification(build_explore_message(context, summary))
    else:
        # MCP ElicitRequest → native CC dialog
        send_elicit_request(build_elicit_params(stage, context, summary))
```

### 6.3 Configuration

| Env var | Purpose | Default |
|---------|---------|---------|
| `EMERGE_OPERATOR_MONITOR` | Enable OperatorMonitor thread | `0` (disabled) |
| `EMERGE_MONITOR_POLL_S` | EventBus poll interval (seconds) | `5` |
| `EMERGE_MONITOR_MACHINES` | Comma-separated machine IDs to monitor | all configured runners |

---

## 7. Skills

### `skills/writing-vertical-adapter/SKILL.md`

Guides adapter authors (human or AI) through:
- `ObserverPlugin` ABC interface with annotated example
- Bootstrap sequence: icc_exec prototype → WAL → `icc_crystallize mode=adapter`
- Testing with mock EventBus + event replay
- Registering with `AdapterRegistry`

AI proactively invokes when: user asks how to add a new vertical, or when CC is writing an `adapter.py` file.

### `skills/operator-monitor-debug/SKILL.md`

Guides debugging of the monitoring pipeline:
- Reading `~/.emerge/operator-events/<id>/events.jsonl`
- Replaying event batches through PatternDetector
- Checking OperatorMonitor thread status via `runner://status`
- Testing ElicitRequest push manually via `icc_exec`

AI proactively invokes when: EventBus is empty, PatternDetector not firing, elicitation never appearing.

---

## 8. Data Flow Summary

```
[Operator machine — remote_runner]
  ObserverPlugin.start()                   ← generic or crystallized adapter
    COM / AX / watchdog events
    → POST /operator-event → EventBus

[EmergeDaemon — OperatorMonitor thread]
  every 5s: GET /operator-events
    → PatternDetector.ingest(events)
    → PatternSummary emitted
    → adapter.get_context()                ← pre-elicitation read
    → push_to_cc(stage, context)

[CC — native MCP handling]
  Explore:  channel notification
              → command queue → LLM turn
  Canary:   ElicitRequest
              → AppState.elicitation.queue
              → ElicitationDialog (~16ms)
              → operator responds
              → ElicitResult → daemon

[EmergeDaemon — on ElicitResult accept]
  Distiller: PatternSummary → intent_signature
  icc_exec @ target_machine:
    adapter.execute(intent, context)       ← takeover
    → WAL entry
    → policy registry update
    → flywheel continues
```

---

## 9. What Ships

| Component | Location | Notes |
|-----------|----------|-------|
| `ObserverPlugin` ABC | `scripts/observer_plugin.py` | New |
| Built-in observers | `scripts/observers/` (accessibility, filesystem, clipboard) | New |
| `AdapterRegistry` | `scripts/adapter_registry.py` | New, mirrors PipelineEngine |
| `OperatorMonitor` | `scripts/operator_monitor.py` | New, loaded by EmergeDaemon |
| `PatternDetector` | `scripts/pattern_detector.py` | New |
| `Distiller` | `scripts/distiller.py` | New |
| remote_runner endpoints | `scripts/remote_runner.py` | Add POST/GET /operator-event(s) |
| `operator_popup.py` | `scripts/operator_popup.py` | New, fallback for non-CC machines |
| `icc_crystallize mode=adapter` | `scripts/emerge_daemon.py` | Extend existing crystallize tool |
| Writing-vertical-adapter skill | `skills/writing-vertical-adapter/SKILL.md` | New |
| Operator-monitor-debug skill | `skills/operator-monitor-debug/SKILL.md` | New |
| Init flywheel hook | `skills/initializing-vertical-flywheel/SKILL.md` | Extend final step |

---

## 10. Open Questions (Deferred)

- **Accessibility API permissions**: macOS requires explicit user consent for AX API access. Bootstrap UX for first-time setup is unresolved.
- **Windows UIAutomation depth**: Some CAD apps render via DirectX and are invisible to UIAutomation. Generic observer falls back to clipboard + filesystem in that case.
- **ElicitRequest during active model inference**: CC source shows elicitation dialog can appear while streaming. Behavior when operator is mid-conversation with CC is untested.
- **Adapter versioning**: When a vertical app updates its COM interface, existing `adapter.py` may break. Auto-demotion strategy (similar to pipeline demotion) is not yet designed.
