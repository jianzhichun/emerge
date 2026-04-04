---
name: muscle-memory-flywheel
description: How to use the emerge flywheel so AI tasks get faster over time. Covers exec conventions, crystallization trigger, reconcile usage, and pipeline lifecycle stages.
---

# Muscle Memory Flywheel

The emerge plugin turns repeated AI actions into deterministic pipelines. The first time a task runs, Claude reasons through it fully (`icc_exec`, slow). Successful patterns crystallize into pipelines (`icc_read`/`icc_write`, fast). Eventually the same task runs at near-native speed with no LLM overhead.

```
icc_exec (AI reasoning, slow)
  → flywheel log accumulates
  → synthesis_ready threshold crossed
  → icc_crystallize generates draft pipeline
  → Claude reviews + validates
  → explore → canary → stable
  → muscle memory: same task, ~native speed
```

---

## Exec Code Conventions

For code to be crystallizable, `icc_exec` calls must follow these conventions:

| Convention | Rule |
|---|---|
| Read output | Set `__result = [{"key": value, ...}]` before the end of the code block |
| Write output | Set `__action = {"ok": True, ...}` before the end of the code block |
| Side effects (COM calls, file writes, network) | Pass `no_replay=True` — excluded from crystallization and WAL replay |
| State setup (creating COM objects, imports) | No `no_replay` — replayed on restart and included in crystallization |

**Example — correct pattern for a read task:**

```python
# icc_exec call — sets up state (no no_replay)
icc_exec(
    code="import win32com.client; app = win32com.client.Dispatch('ZwCAD.Application')",
    intent_signature="zwcad.read.state",
)

# icc_exec call — side-effectful, not replayed
icc_exec(
    code="doc = app.ActiveDocument",
    intent_signature="zwcad.read.state",
    no_replay=True,
)

# icc_exec call — produces output, crystallizable
icc_exec(
    code="__result = [{'entity_count': doc.ModelSpace.Count}]",
    intent_signature="zwcad.read.state",
)
```

---

## When to Crystallize

Check `policy://current` resource. When an exec candidate shows `synthesis_ready: true`, the flywheel has accumulated enough history to crystallize:

```python
icc_crystallize(
    intent_signature="zwcad.read.state",
    connector="zwcad",
    pipeline_name="state",
    mode="read",
    target_profile="default",   # optional, defaults to "default"
)
```

This generates `~/.emerge/connectors/zwcad/pipelines/read/state.py` and `state.yaml`.

**After crystallization:**
1. Review the generated `.py` — verify `run_read`/`run_write` body looks correct
2. Edit if needed (the crystallized code is a starting point, not a final answer)
3. Validate: `icc_read(connector="zwcad", pipeline="state")`
4. Each successful `icc_read`/`icc_write` call feeds the policy flywheel toward canary then stable

---

## Registering Human Fixes

When you correct AI output, tell the flywheel so that pattern is not promoted:

```python
icc_reconcile(
    delta_id="<delta-id from state tracker>",
    outcome="correct",
    intent_signature="zwcad.write.apply-change",   # the pattern being corrected
)
```

A pattern with >5% human corrections stays in explore permanently. True muscle memory — where AI gets it right without help — promotes normally.

---

## Pipeline Lifecycle

| Stage | rollout_pct | Meaning |
|---|---|---|
| `explore` | 0% | Accumulating history, not yet trusted |
| `canary` | 20% | Threshold met, gradual rollout |
| `stable` | 100% | Fully trusted, native speed |

Promotion thresholds (configurable in `~/.emerge/settings.json`):
- explore → canary: 20 attempts, 95% success, 98% verify, ≤5% human-fix
- canary → stable: 40 attempts, 97% success, 99% verify
- Any stage → explore: 2 consecutive failures, or window failure rate <90%

---

## Recovery Signals

When `icc_exec` fails, the response includes structured fields:

```json
{
  "isError": true,
  "error_class": "NameError",
  "error_summary": "name 'app' is not defined",
  "failed_line": 3,
  "recovery_suggestion": "exec"
}
```

When `icc_read`/`icc_write` finds no pipeline yet, `isError` is **false** — it's a guidance response:

```json
{
  "isError": false,
  "pipeline_missing": true,
  "fallback": "icc_exec",
  "fallback_hint": "use icc_exec with intent_signature='zwcad.read.state'"
}
```
