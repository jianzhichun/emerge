---
name: aggregate-suggestions
description: Use when Emerge emits pattern_aggregated facts and Claude must decide whether runner suggestions justify a follow-up distillation task.
---

# Aggregate Suggestions

Use this when `pattern_aggregated` appears. The code layer deduplicates and persists facts; Claude decides what the facts mean.

## Workflow

1. Read the aggregated suggestion event and its source event ids.
2. Check whether the suggestions describe the same user-facing operation.
3. Read connector-local notes for vocabulary and risk guidance.
4. Decide whether to open a distillation task, wait for more evidence, or mark the pattern ignored.

## Rules

- Do not infer from count alone; inspect the source facts.
- Treat destructive writes as higher risk and require clearer evidence.
- Keep connector-specific thresholds in connector-local notes or the current conversation, not in Python.

## Output

Return `distill`, `wait`, or `ignore`, plus the evidence ids and a short rationale.
