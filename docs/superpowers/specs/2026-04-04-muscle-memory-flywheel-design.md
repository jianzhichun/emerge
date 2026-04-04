# Muscle Memory Flywheel — Design Spec

**Date:** 2026-04-04
**Status:** Approved

## Vision

Every time a human or AI uses the system, it gets faster. The first time a task runs, Claude reasons through it fully via `icc_exec` (slow). Successful patterns crystallize automatically into pipelines (fast). Eventually the same task runs at near-native speed with no LLM overhead. No human needs to manage the process once the flywheel is spinning.

```
icc_exec (AI reasoning, ~slow)
  → candidates.json accumulates history
  → synthesis_ready threshold crossed
  → icc_synthesize generates pipeline draft
  → Claude reviews + validates via icc_read/icc_write
  → policy flywheel: explore → canary → stable
  → same task now runs as pipeline (~native speed)
```

---

## Existing Infrastructure (do not rebuild)

| Component | Location | Role |
|---|---|---|
| Policy flywheel (explore→canary→stable) | `repl_daemon._update_pipeline_registry()` | Already promotes pipelines based on metrics |
| L1.5 exec→pipeline routing | `repl_daemon._try_l15_promote()` | Already redirects icc_exec to pipeline when both stable |
| Candidates tracking | `session/candidates.json` | Already accumulates exec + pipeline attempts |
| WAL persistence | `repl_state.wal.jsonl` | Already records every successful exec code |
| Metrics sink | `scripts/metrics.py` | Already emits policy.transition events |
| `no_replay` flag | `repl_state.exec_code()` | Already marks side-effectful code to skip on replay |

---

## Part 1 — Recovery: Structured Errors + Pipeline Fallback

### Problem
All three tool paths return bare error strings on failure. Claude gets no structured context to self-correct.

### `icc_exec` failure — structured error fields

Add to the existing `isError: true` response:

```json
{
  "isError": true,
  "error_class": "NameError",
  "error_summary": "name 'app' is not defined",
  "failed_line": 3,
  "retry_hint": "variable 'app' not in this profile — initialize COM object first",
  "recovery_suggestion": "exec",
  "content": [{"type": "text", "text": "..."}]
}
```

Implementation: parse `traceback.format_exc()` output already captured in `exec_code()`. Add `_parse_exec_error(error_message, code)` helper in `repl_state.py` that extracts `error_class`, `error_summary`, `failed_line`. Return these as top-level fields alongside `isError`.

### `icc_read`/`icc_write` pipeline missing — fallback directive

When `PipelineEngine._load_pipeline()` raises `FileNotFoundError`, instead of propagating the exception as a bare error, return a structured "pipeline missing" response:

```json
{
  "isError": false,
  "pipeline_missing": true,
  "connector": "zwcad",
  "pipeline": "read-annotations",
  "mode": "read",
  "fallback": "icc_exec",
  "fallback_hint": "no pipeline registered yet — use icc_exec with intent_signature='zwcad.read.read-annotations' to explore",
  "content": [{"type": "text", "text": "Pipeline not found. Use icc_exec to explore first."}]
}
```

`isError: false` is intentional — this is a "no pipeline yet" state, not a failure. Claude can act on the `fallback_hint` immediately without treating it as an error condition.

### `icc_read`/`icc_write` execution failure — degraded response

When the pipeline exists but execution fails, add `recovery_suggestion: "exec"` alongside the existing error so Claude knows to fall back.

---

## Part 2 — Auto-Synthesis: Crystallize Exec History into Pipeline Files

### Synthesis readiness signal

In `_update_pipeline_registry()`, when an exec candidate (non-`pipeline::` key) transitions from explore → canary, check if WAL contains at least one successful no-side-effect code block for this `intent_signature`. If yes:

- Set `synthesis_ready: true` in the registry entry
- Emit `policy.synthesis_ready` metric event

This is a signal, not an action. Claude reads it via `policy://current` resource or metric stream and decides when to synthesize.

### New tool: `icc_synthesize`

```json
{
  "name": "icc_synthesize",
  "arguments": {
    "intent_signature": "zwcad.read.state",
    "connector": "zwcad",
    "pipeline_name": "state",
    "mode": "read",
    "target_profile": "default"
  }
}
```

`target_profile` determines which ReplState's WAL to read (each profile has its own `session_{id}/wal.jsonl`). Defaults to `"default"`.

**Daemon behaviour:**

1. Resolve `session_id` from `target_profile` (same logic as `_get_repl()`). Scan that session's `wal.jsonl` for entries where `status=success`, `no_replay=false`, and `metadata.intent_signature` matches. Take the most recent.
2. Wrap the crystallized code in the standard pipeline harness:

```python
# auto-generated — review before promoting
# intent_signature: zwcad.read.state
# synthesized_at: <ts>

def run_read(metadata, args):
    __args = args  # compat with exec __args scope
    # --- CRYSTALLIZED ---
    <exec code here>
    # --- END ---
    return __result  # exec code must set __result

def verify_read(metadata, args, rows):
    return {"ok": bool(rows)}
```

3. Template the `.yaml` metadata:

```yaml
intent_signature: zwcad.read.state
rollback_or_stop_policy: stop
read_steps:
  - run_read
verify_steps:
  - verify_read
synthesized: true
synthesized_at: <ts>
```

4. Write both files to `~/.emerge/connectors/{connector}/pipelines/{mode}/{pipeline_name}.{py,yaml}`.
5. Return `{"ok": true, "py_path": "...", "yaml_path": "...", "code_preview": "..."}`.

Claude then reviews the files, edits if needed, and runs `icc_read`/`icc_write` to validate. Validated attempts feed the existing policy flywheel.

### Exec code convention (enforced via skill documentation)

For code to be synthesizable, it must follow these conventions when called with `icc_exec`:

| Convention | Rule |
|---|---|
| Read output | Set `__result = <list of dicts>` before end |
| Write output | Set `__action = {"ok": True, ...}` before end |
| Side effects | Mark with `no_replay=true` — these are excluded from synthesis |
| State setup | No `no_replay` — replayed on restart AND synthesized into pipeline |

These conventions are taught in `skills/muscle-memory-flywheel/SKILL.md`.

---

## Part 3 — Human Fix Tracking: Close the Quality Loop

### Problem
`trusted_human_fix` is hardcoded `False` in two places. `promote_max_human_fix_rate` (5%) threshold is meaningless. A pipeline that only works because humans keep correcting it must not reach stable.

### `icc_reconcile` extended semantics

The `outcome` field already has `correct`. Wire it up:

| outcome | Effect |
|---|---|
| `confirm` | No change to human_fix_rate |
| `correct` | `human_fixes += 1` for the most recent candidate entry matching this delta's intent_signature |
| `retract` | Remove the attempt from counts entirely |

**When to call `icc_reconcile(outcome=correct)`:** When Claude (or the human) provides a correction after an exec or pipeline result was wrong. Claude is expected to call this proactively when it detects it is fixing its own prior output.

`icc_reconcile` gains an optional `intent_signature` parameter when `outcome=correct`:

```json
{
  "delta_id": "abc123",
  "outcome": "correct",
  "intent_signature": "zwcad.write.apply-change"
}
```

The `intent_signature` is the bridge between the StateTracker delta (which records *what* changed) and the candidates entry (which records *how*). When provided, the daemon increments `human_fixes` on the matching candidate. If omitted, the reconcile still updates StateTracker as before but does not affect human_fix_rate.

### Effect on promotion

With real `human_fix_rate` data:
- A pipeline that needs human correction >5% of the time stays in explore forever
- True muscle memory (AI gets it right without human help) promotes normally
- "Human crutch" patterns are quarantined from the stable path

---

## What Becomes Obsolete

| Existing item | What changes |
|---|---|
| `icc_promote` MCP prompt | Replaced by `icc_synthesize` tool. The prompt guided manual file creation; synthesis automates it. Prompt can be removed or repurposed as a "force synthesis" shortcut. |
| Bare `FileNotFoundError` propagation in `call_tool` | Replaced by structured `pipeline_missing` response. |
| Bare exception string in `icc_exec` failure | Replaced by structured error with `error_class`, `retry_hint`, etc. |

---

## Files Changed

| File | Change |
|---|---|
| `scripts/repl_state.py` | Add `_parse_exec_error()`, return structured fields from `exec_code()` on failure |
| `scripts/repl_daemon.py` | Add `icc_synthesize` to `call_tool()` + `tools/list`; structured pipeline_missing response; wire `correct` in `icc_reconcile`; emit `synthesis_ready` in `_update_pipeline_registry()` |
| `scripts/pipeline_engine.py` | `_load_pipeline()` raises a typed `PipelineMissingError` instead of generic `FileNotFoundError` so `call_tool` can distinguish it cleanly |
| `skills/muscle-memory-flywheel/SKILL.md` | New skill documenting exec conventions, synthesis trigger, reconcile usage, flywheel stages |

---

## Out of Scope

- LLM API calls from within the daemon (daemon stays stateless/sync)
- Automatic pipeline file deletion on rollback (files persist; registry status drives routing)
- Multi-session candidate aggregation (session-scoped candidates are intentional)
