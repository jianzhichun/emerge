# LLM-Crystallized YAML Pipelines

> **Status:** Draft
> **Date:** 2026-04-24
> **Scope:** Break the flywheel out of single-connector, deterministic, single-step mode

## Problem

The flywheel can only optimize repetitive, deterministic, single-connector operations.
All 8 existing pipelines are single-function `.py` files hand-written by the developer.
The crystallizer has never produced a working pipeline from real WAL data because:

1. WAL only stores `tool_name` + `args_hash` — no actual arguments, no results, no decision context
2. The crystallizer mechanically extracts exec code, but LLM reasoning (WHY step B followed step A, WHAT condition triggered a branch) is invisible to it
3. The output format is a single Python function — no multi-step, no cross-connector, no conditionals

## Core Insight

The LLM that just did the work is the only entity that fully understands its own reasoning.
Instead of trying to reconstruct intent from a flat WAL, ask the LLM to write a declarative
YAML scenario capturing what it just did. One LLM call at crystallization time buys
permanent zero-inference execution.

## Design

### 1. Generic YAML Pipeline Engine

Extract the scenario execution engine from `cloud-server/pipelines/write/apply-test.py`
into `scripts/pipeline_yaml_engine.py`. Any connector can place a `.yaml` file in its
pipeline directory and PipelineEngine routes it through the YAML engine.

**Existing step types (carried over from apply-test):**

| Type | Description |
|------|-------------|
| `http_get` | GET request, assert status |
| `http_post` | POST with JSON body, assert status |
| `http_delete` | DELETE request |
| `http_poll` | Poll GET until `until_body` matches or `until_status` reached |
| `cli` | Run local command, assert exit code 0 |
| `cli_poll` | Run command repeatedly until `until` substring appears in stdout |
| `derive` | Compute new variables from templates |

**New step types:**

| Type | Description |
|------|-------------|
| `connector_call` | Call another connector's pipeline by intent signature |
| `branch` | Conditional execution based on a simple comparison expression |

**`branch` condition syntax** uses a restricted subset — only comparison operators
(`>`, `<`, `>=`, `<=`, `==`, `!=`) between a resolved template value and a literal.
The engine evaluates these with Python's `ast.literal_eval` for safety (no arbitrary
code execution). Example: `condition: "{{ node_count | int > 10000 }}"` resolves the
template, parses the comparison, and branches accordingly.
| `transform` | JSON-to-JSON mapping to bridge schema differences between connectors |

**Cross-connector example:**

```yaml
steps:
  - name: read-hm-state
    type: connector_call
    intent: hypermesh.read.state
    extract: { node_count: node_count, elem_count: element_count }

  - name: decide-density
    type: branch
    condition: "{{ node_count | int > 10000 }}"
    when:
      - name: coarse-automesh
        type: connector_call
        intent: hypermesh.write.automesh
        args: { density: coarse }
    otherwise:
      - name: fine-automesh
        type: connector_call
        intent: hypermesh.write.automesh
        args: { density: fine }

  - name: map-to-zwcad
    type: transform
    mapping:
      document_id: "{{ hm_model_name }}"
      tcl_cmd: "*drawline {{ node_count }}"

  - name: write-zwcad
    type: connector_call
    intent: zwcad.write.apply-change
    args: { tcl_cmd: "{{ tcl_cmd }}" }

verify:
  - name: verify-zwcad
    type: connector_call
    intent: zwcad.read.state

rollback:
  - name: undo-zwcad
    type: connector_call
    intent: zwcad.write.apply-change
    args: { tcl_cmd: "*undo" }
```

**PipelineEngine routing (in `_execute_local`):**

```python
if pipeline_path.suffix in ('.yaml', '.yml'):
    return YAMLPipelineEngine.execute(yaml_path, metadata, args)
# else: existing .py exec() path unchanged
```

**YAML result format** (matches existing .py pipeline contract):

```python
# Read mode:
{
    "pipeline_id": "hypermesh-zwcad.write.mesh-to-drawing",
    "verification_state": "verified",
    "rows": [...],
    "verify_result": {"ok": True, ...},
}

# Write mode:
{
    "pipeline_id": "...",
    "verification_state": "verified",
    "action_result": {...},
    "verify_result": {"ok": True, ...},
    "stop_triggered": False,
    "rollback_executed": False,
}
```

### 2. LLM-Assisted Crystallization

**When:** `icc_span_close` at `explore → canary` transition (same trigger as current crystallizer).

**How:** Instead of extracting WAL exec code into a `.py` function, the daemon emits a
cockpit action that asks operator-Claude to generate a YAML scenario from the span's
tool sequence. The LLM sees the full span context (what it did, why, what happened at
each step) and writes a declarative YAML that captures the pattern.

**Flow:**

```
span_close (multi-tool span, synthesis_ready=True)
  → daemon detects >1 tool call in span WAL
  → daemon emits cockpit_action "crystallize_to_yaml"
  → watch_emerge.py delivers to operator-Claude conversation
  → operator-Claude generates YAML scenario (one LLM call)
  → YAML written to connector's pipeline dir as _pending/ sketch
  → auto-activate at stable (same as current .py flow)
```

**For single-tool spans:** current `.py` crystallization path unchanged.

**The prompt template** (what operator-Claude receives via cockpit action):

```
Crystallize the following span into a YAML pipeline scenario.

Span: {intent_signature}
Actions recorded:
{formatted span actions with args and result summaries}

Write a YAML scenario using these step types:
- connector_call: call a connector pipeline (fields: intent, args, extract)
- http_get/post/delete/poll: HTTP operations
- cli/cli_poll: local commands
- derive: compute variables
- transform: map data between formats
- branch: conditional execution (condition, when, otherwise)

Include steps, verify, and rollback sections.
Use {{ template }} syntax for variable substitution.
```

### 3. Enhanced Span Action Logging

To give the LLM enough context to write a good YAML scenario, span actions need richer data.

**Current WAL action fields:**
```json
{"tool_name": "...", "args_hash": "...", "has_side_effects": true, "ts_ms": 123}
```

**New fields (additive, no breaking change):**
```json
{
  "tool_name": "mcp__plugin_emerge__icc_exec",
  "args_hash": "abc123",
  "args_snapshot": {"intent_signature": "hypermesh.read.state", ...},
  "result_summary": {"node_count": 100, "element_count": 50, "source": "live"},
  "has_side_effects": true,
  "ts_ms": 123
}
```

- `args_snapshot`: actual args for ICC tool calls (not for Read/Write/Edit — those stay as hash only)
- `result_summary`: top-level keys + first 200 chars of result (enough for the LLM to understand what happened)

These fields are only populated for ICC tools (icc_exec, icc_span_open/close, connector calls).
General CC tools (Read, Bash, Edit) keep the current hash-only behavior.

### 4. AI Perception — Zero Change

Operator-Claude's mental model stays identical:

1. `icc_span_open(intent_signature="hypermesh-zwcad.write.mesh-to-drawing")`
2. Bridge hit → zero-inference YAML execution
3. Bridge miss → do work → `icc_span_close` → LLM writes YAML (one-time) → future bridge hit

Reflection injection shows intent-level info only:
```
Stable (auto-bridge): hypermesh-zwcad.write.mesh-to-drawing
```

YAML steps are visible in cockpit, not in the prompt. Operator-Claude only sees YAML
when debugging a broken bridge (same as current .py debugging).

### 5. Component Changes Summary

| Component | Change |
|-----------|--------|
| `scripts/pipeline_yaml_engine.py` | **New** — generic YAML scenario executor |
| `scripts/pipeline_engine.py` | **Modify** — route `.yaml` files to YAML engine |
| `scripts/crystallizer.py` | **Modify** — emit cockpit action for multi-tool spans instead of inline code generation |
| `hooks/post_tool_use.py` | **Modify** — populate `args_snapshot` and `result_summary` for ICC tools |
| `cloud-server/pipelines/write/apply-test.py` | **Refactor** — delegate to generic YAML engine |
| Intent signature format | **No change** — still `connector.mode.name` |
| Policy engine | **No change** — thresholds and lifecycle identical |
| WAL format | **Additive** — new optional fields, existing fields untouched |
| Reflection format | **No change** — intent-level only |

### 6. What This Does NOT Change

- `.py` pipelines continue to work (single-function, single-connector)
- Existing stable intents and their evidence are unaffected
- Composite intents (`icc_compose`) remain available for explicit composition
- Policy thresholds and lifecycle stages unchanged
- The crystallizer's single-tool path (explore→canary, single exec code → .py) unchanged

### 7. Success Criteria

1. A multi-step span (e.g., read HM state → transform → write to ZWCAD) crystallizes
   into a `.yaml` pipeline that bridge-executes without LLM
2. The YAML pipeline carries `connector_call` steps that invoke other connectors' pipelines
3. `branch` steps correctly route based on runtime data
4. Operator-Claude's reflection shows the new stable intent with no more complexity than a single-connector one
5. The cloud-server apply-test scenario runs identically after refactoring to the generic engine

### 8. Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| LLM generates invalid YAML | Schema validation before writing; reject and set `synthesis_skipped_reason` |
| LLM hallucinates step types that don't exist | Whitelist validation against engine's registered step types |
| `connector_call` creates circular dependencies | DFS cycle detection (already exists for composites) |
| YAML engine bugs break existing cloud-server tests | Refactor apply-test.py first, run full test suite, then add new step types |
| `args_snapshot` bloats WAL | Only populated for ICC tools; capped at 2KB per action |
| One-shot LLM cost at crystallization | Occurs once per intent lifetime; amortized to zero over many bridge executions |
