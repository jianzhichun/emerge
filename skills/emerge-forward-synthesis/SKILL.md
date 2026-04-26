---
name: emerge-forward-synthesis
description: Distill Emerge forward flywheel WAL samples into a parameterized pipeline result. Use when a lead agent receives a forward_synthesis_pending event or a job with skill_name=emerge-forward-synthesis.
---

# Emerge Forward Synthesis

## Purpose

Turn successful `icc_exec` WAL samples into one deterministic pipeline candidate. The Python daemon is only a coordinator; you are the lead agent doing distillation.

## Inputs

Use the job payload from `forward_synthesis_pending`:
- `normalized_intent`: target `connector.mode.pipeline_name`
- `samples`: successful exec samples with `code`, `args`, optional `result`
- connector `NOTES.md` and `synthesis_hints.yaml` when included

## Rules

1. Compare all samples. Literals that vary with sample `args` should become `__args["name"]`.
2. Be conservative. If unsure whether a literal is a parameter, keep it constant.
3. Remove dead code: no `print`, debug logging, timing noise, or exploratory branches.
4. Do not introduce new dependencies unless connector notes already require them.
5. For read mode, assign `__result`. For write mode, assign `__action`.
6. Generate a stronger verify strategy. Prefer `required_fields`, type checks, and row-shape checks over `bool(rows)`.
7. Validate the candidate through `icc_exec` with `no_replay=true`, then write the pending artifact yourself using the normal file-editing tools.

## Output JSON

Use this shape in your working notes and pending artifact rationale:

```json
{
  "connector": "mock",
  "mode": "read",
  "pipeline_name": "sheet",
  "code": "__result = [{'file': __args['filename']}]",
  "confidence": 0.91,
  "rationale": "filename varied across samples, so it was parameterized; sheet name was constant.",
  "verify_strategy": {
    "required_fields": ["file"]
  }
}
```

## Few-Shot Patterns

### Varying file path

Samples use `/tmp/a.xlsx`, `/tmp/b.xlsx`, `/tmp/c.xlsx` and each sample args has `filename`.

Output code should use `__args["filename"]`.

### Constant file path

All samples use `/tmp/q3.xlsx` and there is no input arg explaining it.

Keep `/tmp/q3.xlsx` literal. 保守优先: do not invent `__args["filename"]`.

### Required fields

If every successful `__result` row has `name` and `area`, include:

```json
{"verify_strategy": {"required_fields": ["name", "area"]}}
```
