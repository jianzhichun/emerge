# Emerge Plugin Completion Design

**Date:** 2026-04-03  
**Status:** Approved  
**Scope:** Fill all gaps identified in plugin audit. No backwards-compatibility constraints.

---

## Overview

Four sequential workstreams, each a prerequisite for the next:

| # | Workstream | Key outcome |
|---|-----------|-------------|
| A | Core engine hardening | Reliable foundation |
| B | MCP surface expansion | Full CC capability usage |
| C | L1.5 composition routing | Exec → pipeline promotion |
| D | Observability | Settings + metrics |

---

## Workstream A — Core Engine Hardening

### A1 · PreCompact Hook

**Problem:** Current `hooks/pre_compact.py` is a no-op. On context compaction, all StateTracker deltas are lost.

**Solution:** On PreCompact signal, serialize StateTracker state into a budget-capped recovery token and return it as `additionalContext`. The new session receives the token via SessionStart and can restore from it.

**Behaviour:**
1. Read `hook-state/state.json`
2. Call `tracker.format_recovery_token(budget_chars=800)`
3. Token includes: goal, LEVEL_CORE_CRITICAL deltas only, open_risks
4. Return `{"hookSpecificOutput": {"additionalContext": "<L1.5_RECOVERY_TOKEN ...>"}}`

**Constraints:** budget_chars=800 hard cap. PERIPHERAL deltas are dropped. Provisional unreconciled deltas are retained with their provisional flag so the next session can re-confirm.

---

### A2 · Runner Retry / Backoff

**Problem:** `RunnerClient.call_tool()` fails immediately on transient errors.

**Solution:** Add `RetryConfig` dataclass. `RunnerClient` reads retry config from settings (D1). `call_tool()` retries on connection errors and HTTP 5xx; never retries 4xx. `health()` is not retried (probe semantics).

```python
@dataclass
class RetryConfig:
    max_attempts: int = 3
    base_delay_s: float = 0.5
    max_delay_s: float = 10.0
    # delay = min(base * 2^attempt, max) * random()  — full jitter
```

Each retry emits a `runner.retry` metrics event (D2).

---

### A3 · Pipeline Metadata Schema Validation

**Problem:** Malformed pipeline metadata silently uses empty dict, producing cryptic errors at runtime.

**Solution:** `PipelineEngine._load_metadata()` validates immediately after parsing. No external dependencies — inline logic only.

**Rules:**
- `intent_signature` required (non-empty string)
- `rollback_or_stop_policy` required, must be `"stop"` or `"rollback"`
- Exactly one of `read_steps` or `write_steps` required (non-empty list)
- `verify_steps` required (non-empty list)

**Failure:** `ValueError("pipeline metadata invalid at <path>: missing fields: {...}")` — path + specific missing fields in message.

---

## Workstream B — MCP Surface Expansion

### B1 · MCP Resources

Add `resources/list` and `resources/read` handlers to `ReplDaemon.handle_jsonrpc()`.

**Resource registry:**

| URI | Content | Dynamic? |
|-----|---------|---------|
| `policy://current` | Full pipelines-registry.json for active session | No |
| `pipeline://{connector}/{mode}/{name}` | Pipeline metadata (yaml/json) | Yes — scan connector_roots |
| `runner://status` | runner-map + health summary | No |
| `state://deltas` | StateTracker goal + deltas + risks | No |

**Wire format** (MCP spec compliant):
```json
{
  "uri": "policy://current",
  "mimeType": "application/json",
  "text": "{...}"
}
```

`resources/list` returns all static URIs plus dynamically discovered `pipeline://` URIs by scanning `_connector_roots`. Dynamic resources scanned at read time, not cached.

---

### B2 · MCP Prompts

Add `prompts/list` and `prompts/get` handlers. Content inlined in daemon — no external files.

| name | Description | Arguments |
|------|-------------|-----------|
| `icc_explore` | Explore a new vertical using icc_exec with policy tracking | `vertical` (required), `goal` (optional) |
| `icc_promote` | Promote an exec history into a formalized pipeline | `intent_signature` (required), `script_ref` (required), `connector` (required) |

Returns MCP prompt format: `{name, description, arguments[], messages[{role, content}]}`.

---

### B3 · plugin.json Capabilities

Rewrite `.claude-plugin/plugin.json`:

```json
{
  "name": "emerge",
  "version": "0.2.0",
  "description": "Generic RWB flywheel plugin for Claude Code",
  "capabilities": {
    "tools": ["icc_exec", "icc_read", "icc_write", "icc_reconcile"],
    "resources": ["policy://current", "pipeline://*", "runner://status", "state://deltas"],
    "prompts": ["icc_explore", "icc_promote"]
  },
  "permissions": {
    "filesystem": ["~/.emerge/"],
    "network": ["localhost", "192.168.122.0/24"]
  }
}
```

---

### B4 · `icc_reconcile` Tool

New MCP tool (4th tool). Hidden from default `tools/list` recommendations but callable directly.

**Signature:**
```
icc_reconcile(delta_id: str, outcome: "confirm" | "correct" | "retract") → dict
```

**Behaviour:**
1. Call `StateTracker.reconcile_delta(delta_id, outcome)`
2. Persist updated state to `hook-state/state.json`
3. Return `{delta_id, outcome, verification_state, goal}`

PostToolUse hook matcher extended to include `icc_reconcile`.

---

## Workstream C — L1.5 Composition Routing

### C1 · Promotion Condition

When `icc_exec` is called with `intent_signature + script_ref + base_pipeline_id`, daemon evaluates the L1.5 candidate key:

```
l15::<base_pipeline_id>::<intent_signature>::<script_ref>
```

Routing decision table:

| L1.5 candidate status | Pipeline exists & canary/stable? | Action |
|----------------------|----------------------------------|--------|
| explore or canary | any | Normal exec — accumulate data |
| stable | no | Normal exec — pipeline not ready |
| **stable** | **yes** | **Route to icc_read / icc_write** |

"Pipeline exists" = `pipeline::<base_pipeline_id>` in pipelines-registry with status canary or stable.

---

### C2 · Routing Logic

New method `_try_l15_promote(arguments) -> dict | None` called at the top of `icc_exec` handling in `call_tool()`.

```python
def _try_l15_promote(self, arguments):
    key = self._resolve_l15_key(arguments)
    if key is None: return None
    candidate = candidates_registry.get(key)
    if not candidate or candidate["status"] != "stable": return None
    pipeline_id = arguments["base_pipeline_id"]
    pipeline_entry = pipeline_registry.get(f"pipeline::{pipeline_id}")
    if not pipeline_entry or pipeline_entry["status"] not in ("canary", "stable"): return None
    connector, mode, name = pipeline_id.split(".", 2)
    if mode == "write":
        result = self.pipeline.run_write({**arguments, "connector": connector, "pipeline": name})
    else:
        result = self.pipeline.run_read({**arguments, "connector": connector, "pipeline": name})
    result["l15_promoted"] = True
    return result
```

Returns `None` → normal exec path. Returns dict → skip exec, return promoted result.

---

### C3 · Feedback Loop

After L1.5 promotion:
- Record as **pipeline event** (not exec event) → feeds into pipeline canary/stable tracking
- Emit `l15.promoted` metrics event (D2)
- PostToolUse hook receives `l15_promoted: true` → raises delta level from PERIPHERAL to CORE_SECONDARY

This closes the loop: promoted calls continue contributing to pipeline stability metrics.

---

## Workstream D — Observability

### D1 · Settings Externalization

New file: `~/.emerge/settings.json`

```json
{
  "policy": {
    "promote_min_attempts": 20,
    "promote_min_success_rate": 0.95,
    "promote_min_verify_rate": 0.98,
    "promote_max_human_fix_rate": 0.05,
    "rollback_consecutive_failures": 2,
    "stable_min_attempts": 40,
    "stable_min_success_rate": 0.97,
    "stable_min_verify_rate": 0.99,
    "window_size": 20
  },
  "connector_root": "~/.emerge/connectors",
  "runner": {
    "timeout_s": 30,
    "retry_max_attempts": 3,
    "retry_base_delay_s": 0.5
  },
  "metrics_sink": "local_jsonl"
}
```

**Load order:** `EMERGE_SETTINGS_PATH` env → `~/.emerge/settings.json` → hardcoded defaults.  
**Validation:** inline schema check (no external deps), raises `ValueError` on invalid fields.  
**Caching:** `load_settings()` singleton in `policy_config.py`, loaded once at daemon startup.

`PipelineEngine`, `RunnerClient`, `ReplDaemon` all read from `load_settings()` instead of hardcoded constants.

---

### D2 · Metrics Sink

New module: `scripts/metrics.py`

```python
class MetricsSink(Protocol):
    def emit(self, event_type: str, payload: dict) -> None: ...

class LocalJSONLSink:
    # appends {"ts_ms": ..., "event_type": ..., **payload} to ~/.emerge/metrics.jsonl
    # atomic write via tempfile + rename

class NullSink:
    def emit(self, *_, **__): pass

def get_sink(settings: dict) -> MetricsSink:
    kind = settings.get("metrics_sink", "local_jsonl")
    return LocalJSONLSink() if kind == "local_jsonl" else NullSink()
```

**Emit points:**

| Event type | Where |
|-----------|-------|
| `pipeline.read` | `_record_pipeline_event` — icc_read |
| `pipeline.write` | `_record_pipeline_event` — icc_write |
| `exec.call` | `_record_exec_event` |
| `l15.promoted` | `_try_l15_promote` on hit |
| `runner.retry` | `RunnerClient.call_tool` on retry |
| `policy.transition` | pipeline status change in `_update_pipeline_registry` |

Existing `pipeline-events.jsonl` / `exec-events.jsonl` are kept (session-level fine-grained logs). `metrics.jsonl` is the cross-session aggregation stream — both run in parallel.

---

## Files Touched

| File | Change |
|------|--------|
| `hooks/pre_compact.py` | Real implementation (A1) |
| `scripts/runner_client.py` | RetryConfig + retry loop (A2) |
| `scripts/pipeline_engine.py` | Metadata validation (A3) |
| `scripts/repl_daemon.py` | Resources, prompts, icc_reconcile, L1.5 routing (B1-B4, C2-C3) |
| `scripts/policy_config.py` | load_settings() singleton, settings schema (D1) |
| `scripts/metrics.py` | New file — MetricsSink implementations (D2) |
| `.claude-plugin/plugin.json` | Capabilities declaration (B3) |
| `tests/test_mcp_tools_integration.py` | New tests for all capabilities |
| `tests/test_pipeline_engine.py` | Schema validation tests |
| `tests/test_repl_admin.py` | Settings-aware tests |

---

## Testing Strategy

Each workstream follows RED → GREEN pattern:
- A: Unit tests for schema validation errors, integration tests for retry behaviour, PreCompact hook output shape
- B: MCP protocol tests for resources/list, resources/read, prompts/list, icc_reconcile tool
- C: L1.5 promotion trigger test (stable candidate + stable pipeline → redirected), non-promotion test (canary candidate → normal exec)
- D: Settings load/override tests, metrics emit verification (LocalJSONLSink file contents)

Full suite must pass at end of each workstream before proceeding to next.
