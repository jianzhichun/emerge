---
name: emerge-reverse-synthesis
description: Distill Emerge reverse flywheel raw operator events into a structured synthesis result. Use when a lead agent receives synthesis_job_ready with skill_name=emerge-reverse-synthesis.
---

# Emerge Reverse Synthesis

## Purpose

Turn repeated raw operator events into a deterministic pipeline candidate. The daemon only packages the job and validates your result.

## Inputs

Use the `synthesis_job_ready` job payload:
- `normalized_intent`: detected operator behavior
- `events`: raw operator events
- `connector_notes` and `synthesis_hints`
- `context_hint`, `machine_ids`, `detector_signals`

## Rules

1. Infer the smallest reusable operation from raw operator events.
2. Parameterize operator-specific values through `__args[...]` only when they are likely inputs.
3. Keep stable connector constants literal.
4. Assign `__result` for read mode or `__action` for write mode.
5. Remove debug code and narration from final code.
6. Include a clear `rationale` describing event evidence and parameter choices.
7. Submit via `icc_synthesis_submit`; never write pipeline files directly.

## Output JSON

Pass this as `result` to `icc_synthesis_submit`:

```json
{
  "connector": "zwcad",
  "mode": "write",
  "pipeline_name": "create_room_labels",
  "code": "__action = {'ok': True, 'created': []}",
  "confidence": 0.82,
  "rationale": "raw operator events repeatedly added room labels on the same layer; label text is parameterized via __args.",
  "verify_strategy": {
    "required_fields": []
  }
}
```

## Quality Bar

The generated code is compiled once, then runs without LLM. Treat this as compile-time distillation, not runtime reasoning.
