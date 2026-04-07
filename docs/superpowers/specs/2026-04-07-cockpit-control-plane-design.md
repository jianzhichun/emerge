# Cockpit Control Plane — Design Spec

**Date:** 2026-04-07
**Status:** Draft
**Scope:** Upgrade cockpit from pipeline display page to full control plane with 8-layer object model, unified audit timeline, and control actions.

---

## 1. Problem

Current cockpit surfaces ~30% of available runtime state. Specifically:

- **Visible:** pipeline registry (policy lifecycle), goal (set/rollback), connector notes/components, thresholds.
- **Invisible:** state deltas, open risks, verification state, exec events, span WAL/candidates, session health (checkpoint/recovery), operator events, metrics trends.
- **No control actions on:** deltas (confirm/correct/retract), risks (handle/snooze), spans (approve/freeze), exec candidates (replay/crystallize), operator patterns (confirm-intent/dismiss).

The cockpit must become the single pane of glass for all Emerge runtime objects — observable, controllable, auditable.

---

## 2. Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Navigation model | **C: Mixed** — Overview by `intent_signature`, Detail by object type | Intent is the natural partition for operational troubleshooting; object type is the natural partition for targeted control actions. |
| Control scope | All 8 layers writable | Greenfield — no backward compatibility constraint. |
| Event sourcing | Reuse existing `exec-events.jsonl` + `pipeline-events.jsonl` + `goal-ledger.jsonl` + `span-wal` as audit sources | These already exist and are append-only. No need to invent a new event bus. |
| Dual candidate systems | Surface both `candidates.json` (exec) and `span-candidates.json` (span) side by side | They are independent tracking systems with different schemas; hiding either loses signal. |
| Risk model | Enhance `StateTracker` — add `risk_id`, `status`, `snoozed_until_ms`, `handled_reason` | Current risks are bare strings; control actions need identity and lifecycle. |
| Delta enrichment | Add `intent_signature`, `tool_name`, `ts_ms` to delta records | Without these, deltas cannot be associated to intents in the Overview. |

---

## 3. Object Model (8 Layers)

### 3.1 Delta

**Source:** `state.json → deltas[]`

**Current schema:**
```
id, message, level (core_critical|core_secondary|peripheral),
verification_state (verified|degraded), provisional (bool),
reconcile_outcome? (confirm|correct|retract)
```

**Enrichment (new fields):**
```
intent_signature? (string — from tool_input if available, else "unknown")
tool_name? (string — triggering tool)
ts_ms (int — when delta was recorded)
```

**Control actions:**

| Action | Endpoint | Confirm? |
|--------|----------|----------|
| `confirm` | `POST /api/control-plane/delta/reconcile` | No |
| `correct` | same | No (but records human_fix on policy) |
| `retract` | same | Yes — degrades verification_state |

**Writer:** `hooks/post_tool_use.py` via `StateTracker.add_delta()`.
**API proxy:** cockpit calls `POST /api/control-plane/delta/reconcile` → `repl_admin` loads `StateTracker`, calls `reconcile_delta(delta_id, outcome)`, saves, and if `outcome=correct` + `intent_signature` present, also increments `human_fix_rate` on the most-recently-used candidate in `candidates.json` (same logic as daemon `_increment_human_fix`).

### 3.2 Risk

**Source:** `state.json → open_risks[]`

**Current schema:** bare `string[]`.

**Enrichment (upgrade to objects):**
```
risk_id (string — hash of text or sequential)
text (string — risk description)
status (open|handled|snoozed)
created_at_ms (int)
snoozed_until_ms? (int — auto-reopen after expiry)
handled_reason? (string)
source_delta_id? (string — which delta triggered this risk)
intent_signature? (string)
```

**Control actions:**

| Action | Confirm? |
|--------|----------|
| `add risk` | No |
| `mark handled` (requires reason) | No |
| `snooze` (requires duration) | No |
| `reopen` | No |

**Migration:** existing bare strings become `{ risk_id: hash(text), text, status: "open", created_at_ms: 0 }`.

### 3.3 Goal

**Source:** `goal-snapshot.json` + `goal-ledger.jsonl`

**Schema (already rich):**
- Snapshot: `version, text, source, decided_by, rationale, updated_at_ms, ttl_ms, expires_at_ms, locked_until_ms, last_event_id`
- Ledger entry: `event_id, ts_ms, event_type, source, actor, text, rationale, confidence, decision { accepted, reason, score, breakdown, snapshot_version }`

**Control actions:**

| Action | Confirm? |
|--------|----------|
| `set goal` | No |
| `rollback to event` | Yes — shows text diff + version |
| `lock goal` (set `locked_until_ms`) | No |

**No schema changes needed** — goal is already the most mature object.

### 3.4 Span

**Source:** `span-wal/spans.jsonl` (closed spans) + `span-candidates.json` (aggregate policy) + `state.json → active_span_id/intent` (live span)

**Span record:**
```
span_id, intent_signature, description, source, skill_name,
opened_at_ms, closed_at_ms, outcome, is_read_only,
args, result_summary, actions[] (seq, tool_name, args_hash, has_side_effects, ts_ms)
```

**Span candidate:**
```
intent_signature, is_read_only, description, attempts, successes,
human_fixes, consecutive_failures, recent_outcomes[], last_ts_ms,
skeleton_generated
```

**Control actions:**

| Action | Confirm? |
|--------|----------|
| `approve skeleton` (→ `icc_span_approve`) | Yes |
| `freeze intent` (prevent auto-bridge) | Yes |
| `unfreeze intent` | No |
| `reset candidate counters` | Yes |

### 3.5 Exec

**Source:** `<session>/exec-events.jsonl` + `<session>/candidates.json` + `<session>/wal.jsonl`

**Exec event (per line):**
```
ts_ms, source="exec", mode, target_profile, intent_signature,
script_ref, base_pipeline_id, verify_passed, human_fix, is_error,
sampled_in_policy
```

**Exec candidate:**
```
source, target_profile, last_execution_path, intent_signature,
script_ref, attempts, successes, verify_passes, human_fixes,
degraded_count, consecutive_failures, recent_outcomes[], total_calls,
last_ts_ms, description?
```

**Control actions:**

| Action | Confirm? |
|--------|----------|
| `crystallize` (→ `icc_crystallize`) | Yes — generates .py + .yaml |
| `replay WAL` (re-execute session WAL) | Yes |
| `clear candidate` | Yes |

### 3.6 Policy

**Source:** `pipelines-registry.json`

**Per-pipeline:**
```
status (explore|canary|stable), rollout_pct, last_transition_reason,
attempts_at_transition, description?, source?, synthesis_ready,
success_rate, verify_rate, human_fix_rate, window_success_rate,
consecutive_failures, last_policy_action, last_execution_path,
updated_at_ms
```

**Control actions:**

| Action | Confirm? | Evidence required? |
|--------|----------|--------------------|
| `promote → canary` | No | Show threshold gaps |
| `promote → stable` | Yes | Show all thresholds met |
| `demote → explore` | Yes (requires reason) | — |
| `demote → canary` | No | — |
| `freeze` (new: prevent auto-transitions) | Yes | — |
| `unfreeze` | No | — |
| `reset failures` | No | — |
| `delete pipeline` | Yes | — |
| `override thresholds` (per-intent) | Yes | — |

### 3.7 Session

**Source:** `<session>/checkpoint.json` + `<session>/recovery.json` + `<session>/wal.jsonl`

**Checkpoint:** `wal_seq_applied, globals (serializable snapshot), state_hash, updated_at_ms`
**Recovery:** `recovery_degraded, issues[], updated_at_ms`

**Control actions:**

| Action | Confirm? |
|--------|----------|
| `export snapshot` (JSON download) | No |
| `reset tracker` | Yes — typed confirm "RESET"; auto-exports before reset |
| `view WAL` (paginated) | Read-only |
| `view checkpoint globals` | Read-only |

### 3.8 Operator

**Source:** `~/.emerge/operator-events/<machine>/events.jsonl` + `PatternSummary` output

**Operator event:** `ts_ms, machine_id, session_role, event_type, app, payload`
**PatternSummary:** `machine_ids[], intent_signature, occurrences, window_minutes, detector_signals[], context_hint, policy_stage`

**Control actions:**

| Action | Confirm? |
|--------|----------|
| `confirm intent` (→ Distiller writes `intent_confirmed`) | No |
| `dismiss pattern` | No |
| `escalate to CC` (push as prompt) | No |

---

## 4. Information Architecture

```
┌─────────────────────────────────────────────────────────┐
│ Header: Goal bar (set/rollback/lock) + CC indicator     │
├─────────────────────────────────────────────────────────┤
│ Thresholds bar (editable via settings modal)            │
├──────────┬──────────────────────────────────────────────┤
│ Tabs     │                                              │
│          │                                              │
│ Overview │  Main Panel                                  │
│ <conn>   │                                              │
│ <conn>   │                                              │
│ Audit    │                                              │
│ Session  │                                              │
│ Operator │                                              │
│          ├──────────────────────────────────────────────┤
│          │  Queue Panel (pending actions + submit)       │
├──────────┴──────────────────────────────────────────────┤
│ Status bar: msg | CC status | refresh timer + last time │
└─────────────────────────────────────────────────────────┘
```

### 4.1 Overview Tab (intent-first)

**Top strip:** 4 stat cards:
- Total intents (explore / canary / stable counts)
- Degraded intents (verification_state == degraded OR consecutive_failures >= 1)
- Open risks count
- Unreconciled deltas count (provisional == true)

**Intent table** (sortable, filterable):

| Column | Source |
|--------|--------|
| Intent signature | pipeline key / span candidate key |
| Source | exec / span / both |
| Policy status | pipelines-registry / span-candidates |
| Success rate | computed from candidates |
| Human fix rate | candidates |
| Open deltas | state.json deltas filtered by intent |
| Open risks | state.json risks filtered by intent |
| Last event | max(exec-events.ts_ms, span-wal.closed_at_ms) |

Click row → navigate to connector tab, scroll to that intent's detail.

### 4.2 Connector Tab (object-detail)

Sub-panels (existing + new):

| Panel | Status |
|-------|--------|
| Pipelines | Exists — enhance with dual-candidate view |
| Notes | Exists |
| Controls | Exists (injected components) |
| **Deltas** | **New** — filtered by connector |
| **Risks** | **New** — filtered by connector |
| **Spans** | **New** — recent span WAL entries for this connector |
| **Exec Events** | **New** — recent exec events for this connector |

### 4.3 Audit Tab (new global tab)

**Unified timeline** merging:
- `exec-events.jsonl` (per session)
- `pipeline-events.jsonl` (per session)
- `goal-ledger.jsonl`
- `span-wal/spans.jsonl`
- delta reconcile events (from state.json reconcile_outcome changes)

**Filters:**
- Time range (last 1h / 6h / 24h / custom)
- Intent signature (autocomplete)
- Object type (delta / risk / goal / span / exec / policy)
- Severity (degraded only / all)
- Outcome (success / failure / all)

**Each row shows:** `ts_ms | object_type icon | intent | action | actor | before→after summary`

### 4.4 Session Tab (new global tab)

- Session ID, state root, WAL entry count
- Checkpoint: `wal_seq_applied`, `state_hash`, `updated_at_ms`
- Recovery: `degraded?`, `issues[]` (collapsible)
- StateTracker snapshot: `verification_state`, `consistency_window_ms`, delta/risk counts
- Actions: Export / Reset

### 4.5 Operator Tab (new global tab)

- Per-machine event stream (last N events)
- Detected patterns (`PatternSummary` cards)
- Per-pattern actions: confirm / dismiss / escalate
- Machine health (last event ts, event rate)

---

## 5. API Surface (new endpoints)

All new endpoints under `/api/control-plane/`.

### 5.1 Read endpoints

| Endpoint | Returns |
|----------|---------|
| `GET /api/control-plane/state` | Full StateTracker snapshot (deltas + risks + verification_state) |
| `GET /api/control-plane/intents` | Merged intent list from pipelines-registry + span-candidates + candidates.json |
| `GET /api/control-plane/exec-events?limit=N&since_ms=T&intent=X` | Paginated exec events |
| `GET /api/control-plane/pipeline-events?limit=N&since_ms=T&intent=X` | Paginated pipeline events |
| `GET /api/control-plane/spans?limit=N&intent=X` | Recent span WAL entries |
| `GET /api/control-plane/span-candidates` | All span candidate entries |
| `GET /api/control-plane/session` | Checkpoint + recovery + WAL stats |
| `GET /api/control-plane/operator-events?machine=X&limit=N` | Operator event stream |
| `GET /api/control-plane/patterns` | Latest PatternSummary objects |
| `GET /api/control-plane/metrics-summary?window_ms=T` | Aggregated exec frequency, success rate, error rate over window |
| `GET /api/control-plane/audit?since_ms=T&until_ms=T&type=X&intent=X&limit=N` | Unified timeline |

### 5.2 Write endpoints

| Endpoint | Body | Effect |
|----------|------|--------|
| `POST /api/control-plane/delta/reconcile` | `{ delta_id, outcome, intent_signature? }` | Reconcile delta; if `correct` + intent → human_fix on policy |
| `POST /api/control-plane/risk/update` | `{ risk_id, action: "handle"|"snooze"|"reopen", reason?, snooze_duration_ms? }` | Update risk lifecycle |
| `POST /api/control-plane/risk/add` | `{ text, intent_signature? }` | Add new risk |
| `POST /api/control-plane/span/freeze` | `{ intent_signature }` | Set frozen flag on span candidate |
| `POST /api/control-plane/span/unfreeze` | `{ intent_signature }` | Clear frozen flag |
| `POST /api/control-plane/span/reset` | `{ intent_signature }` | Zero out span candidate counters |
| `POST /api/control-plane/policy/set` | `{ key, fields }` | Set pipeline registry fields (existing) |
| `POST /api/control-plane/policy/freeze` | `{ key }` | Set frozen flag on pipeline |
| `POST /api/control-plane/policy/unfreeze` | `{ key }` | Clear frozen flag |
| `POST /api/control-plane/session/export` | `{}` | Returns JSON snapshot download |
| `POST /api/control-plane/session/reset` | `{ confirm: "RESET" }` | Export + reset StateTracker |
| `POST /api/control-plane/operator/confirm-intent` | `{ intent_signature, machine_ids }` | Distiller confirm |
| `POST /api/control-plane/operator/dismiss` | `{ intent_signature }` | Mark pattern dismissed (in-memory only; no persistent dismiss log yet) |

### 5.3 Existing endpoints (kept)

All current `/api/*` endpoints remain unchanged for backward compatibility with the submit/dispatch loop in `commands/cockpit.md`. New control-plane endpoints are additive.

---

## 6. Data Flow

```
hooks/post_tool_use.py ──write──► state.json (deltas, risks)
                                      │
hooks/post_tool_use_failure.py ──►    │ (mark_degraded)
                                      │
SpanTracker ──write──► span-wal/spans.jsonl
                   └──► span-candidates.json
                                      │
EmergeDaemon ──write──► exec-events.jsonl
                    └──► pipeline-events.jsonl
                    └──► candidates.json
                    └──► pipelines-registry.json
                                      │
GoalControlPlane ──write──► goal-snapshot.json
                        └──► goal-ledger.jsonl
                                      │
ExecSession ──write──► wal.jsonl
                   └──► checkpoint.json
                   └──► recovery.json
                                      │
OperatorMonitor ──read──► operator-events/*.jsonl
PatternDetector ──produce──► PatternSummary (in-memory)
Distiller ──write──► operator-events (intent_confirmed)
                                      │
         ┌────────────────────────────┘
         ▼
   /api/control-plane/* ──read all──► cockpit frontend
                        ◄──write──── cockpit control actions
```

---

## 7. Confirmation & Safety

### 7.1 Confirmation tiers

| Tier | Actions | UX |
|------|---------|-----|
| **Silent** | confirm delta, add risk, mark handled, snooze, read-only views | Inline button, status bar feedback |
| **Warn** | correct delta, promote canary, demote, reset failures, unfreeze | Yellow highlight, single click confirm |
| **Block** | retract delta, promote stable, freeze, delete pipeline, reset session, rollback goal | Modal with before/after diff, typed confirm for destructive ops |

### 7.2 Batch operations

Batch only for `confirm delta` (safe). All other actions are single-item only.

### 7.3 Consistency

- No optimistic UI updates — all writes wait for server response.
- After every submit: full refresh cycle (`policy + assets + state + goal + goalHistory`).
- Status bar shows batch sequence number + last sync time.

---

## 8. Frontend Architecture

Single-file `cockpit_shell.html` (existing pattern). No build toolchain.

### 8.1 New state variables

```javascript
let stateData = null;      // GET /api/control-plane/state
let intentsData = null;    // GET /api/control-plane/intents
let spanCandidates = null; // GET /api/control-plane/span-candidates
let sessionData = null;    // GET /api/control-plane/session
let auditEvents = [];      // GET /api/control-plane/audit
let operatorData = null;   // GET /api/control-plane/operator-events + patterns
```

### 8.2 Refresh strategy

| Data | Interval | Trigger |
|------|----------|---------|
| Policy + assets | 5s (existing) | Auto |
| State (deltas/risks) | 5s | Auto (same cycle as policy) |
| Intents | 5s | Auto (derived from policy + candidates) |
| Goal + history | 5s (existing) | Auto |
| Session health | 30s | Auto (less frequent — checkpoint is infrequent) |
| Audit timeline | On tab switch + 10s while visible | Lazy |
| Operator events | 10s while tab visible | Lazy |
| Metrics summary | 30s while visible | Lazy |

### 8.3 Tab structure

```
Overview | <connector>... | Audit | Session | Operator
```

Connector tabs get 4 new sub-panels: Deltas, Risks, Spans, Exec Events (alongside existing Pipelines, Notes, Controls).

---

## 9. Schema Changes Required

### 9.1 `StateTracker` delta enrichment

In `hooks/post_tool_use.py`, when calling `tracker.add_delta()`:

```python
delta_id = tracker.add_delta(
    message=message,
    level=level,
    verification_state=verification_state,
    provisional=provisional,
    intent_signature=intent_signature,  # NEW
    tool_name=tool_name,                # NEW
    ts_ms=int(time.time() * 1000),      # NEW
)
```

`StateTracker.add_delta()` signature adds optional `intent_signature`, `tool_name`, `ts_ms`.

### 9.2 Risk object upgrade

`StateTracker.open_risks` changes from `list[str]` to `list[dict]`:

```python
{
    "risk_id": "r-<hash>",
    "text": "...",
    "status": "open",  # open | handled | snoozed
    "created_at_ms": 1234567890,
    "snoozed_until_ms": None,
    "handled_reason": None,
    "source_delta_id": None,
    "intent_signature": None,
}
```

Migration: `_normalize_state` converts bare strings to object form.

### 9.3 Pipeline registry — `frozen` field

Add optional `frozen: bool` (default `false`) to pipeline entries. When `frozen=true`, daemon skips auto-promotion/demotion for that intent.

### 9.4 Span candidates — `frozen` field

Same pattern: add `frozen: bool` to span candidate entries.

---

## 10. Implementation Order

| Step | What | Files |
|------|------|-------|
| 1 | Schema changes: delta enrichment + risk objects + frozen flags | `state_tracker.py`, `post_tool_use.py`, `emerge_daemon.py`, `span_tracker.py` |
| 2 | New API endpoints (`/api/control-plane/*`) | `repl_admin.py` |
| 3 | Frontend: Overview tab with intent table + stat cards | `cockpit_shell.html` |
| 4 | Frontend: Connector sub-panels (Deltas, Risks, Spans, Exec Events) | `cockpit_shell.html` |
| 5 | Frontend: Audit tab | `cockpit_shell.html` |
| 6 | Frontend: Session tab | `cockpit_shell.html` |
| 7 | Frontend: Operator tab | `cockpit_shell.html` |
| 8 | Control action handlers (write endpoints) | `repl_admin.py` |
| 9 | Confirmation modals + safety gates | `cockpit_shell.html` |
| 10 | Tests: API endpoints + schema migration + control actions | `tests/` |

---

## 11. Risk Migration & Backward Compatibility

### 11.1 `state.json` risk migration

`_normalize_state` in `state_tracker.py` detects bare strings in `open_risks` and converts:

```python
# Before: ["pipeline verification failed: icc_exec"]
# After:  [{"risk_id": "r-<sha256[:12]>", "text": "pipeline verification failed: icc_exec",
#            "status": "open", "created_at_ms": 0}]
```

All consumers (`format_context`, `format_recovery_token`, hooks) are updated to read `.text` from risk objects. The `add_risk(text)` method creates the object form directly.

### 11.2 `state.json` delta backward compat

New fields (`intent_signature`, `tool_name`, `ts_ms`) are optional. `_normalize_state` fills missing `ts_ms` with `0`, missing `intent_signature`/`tool_name` with `None`. The Overview intent table groups `None`-intent deltas under an "Unattributed" section.

### 11.3 Frozen flag

`frozen` defaults to `false` if absent. No migration needed — existing registry/candidate files work as-is.

---

## 12. Operator Dismiss Persistence (Future)

`POST /api/control-plane/operator/dismiss` currently only suppresses the pattern in-memory for the current cockpit session. A persistent dismiss log (`~/.emerge/operator-dismissed.jsonl`) with `{ intent_signature, dismissed_at_ms, reason? }` should be added when operator patterns are actively used. Not blocking for v1.
