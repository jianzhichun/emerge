---
name: distill-from-pattern
description: Use when an Emerge event stream contains pattern_observed, local_pattern_observed, pattern_aggregated, or pattern_pending_synthesis and Claude must turn raw runner/operator facts into one conservative pipeline candidate through normal mechanism tools.
---

# Distill From Pattern

Use this when `pattern_observed`, `local_pattern_observed`, `pattern_aggregated`, or `pattern_pending_synthesis` appears in the event stream. The Python layer only emits facts; Claude performs the judgment.

## Handoff Contract

`scripts/synthesis_events.py` is the deterministic packaging boundary. It can append `pattern_pending_synthesis` and `synthesis_job_ready` facts for a selected event window, but it does not choose whether the pattern matters, call an LLM provider, smoke-test generated code, or materialize a pipeline.

When the stream only contains `pattern_observed`, `local_pattern_observed`, or `pattern_aggregated`, inspect the facts first. If they justify distillation, package or reference the selected window as synthesis facts, then continue from `pattern_pending_synthesis` / `synthesis_job_ready`. Do not reintroduce a Python coordinator.

## Workflow

1. Read the referenced event window from `events/events-<profile>.jsonl`, `events/events-local.jsonl`, or `events/events.jsonl`.
2. Read connector-local `NOTES.md` and optional `synthesis_hints.yaml`.
3. Infer the smallest repeated operation and choose a generic `connector.mode.name` intent.
4. Write conservative exploratory code that assigns `__result` for reads or `__action` for writes.
5. Run one `icc_exec` with explicit `intent_signature` and representative `args`.
6. If execution fails or verification is ambiguous, report the blocker instead of retrying indefinitely.

## Rules

- Keep product-specific knowledge in connector-local files, not this skill.
- Parameterize only values that vary across event samples.
- Prefer a narrow pipeline candidate over a broad one with weak verification.
- Never write lifecycle state directly; use the mechanism tools that record WAL and evidence.

## Output

Return a concise report with intent signature, source events inspected, `icc_exec` verification result, and any remaining blocker.
