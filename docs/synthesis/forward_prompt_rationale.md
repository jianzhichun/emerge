# Forward Synthesis Prompt Rationale

## Why few-shot examples

The forward flywheel has to generalize from successful `icc_exec` samples without making runtime LLM calls. The few-shot set covers the product-critical cases:

- varying file path -> parameterize through `__args["filename"]`
- constant file path -> keep the literal
- shared output row shape -> emit `verify_strategy.required_fields`

These examples are intentionally small so a lead agent can apply them to CAE/CAD connectors without copying domain-specific code.

## 保守优先

The default is not to parameterize. A literal becomes an argument only when samples show variation and the job payload provides a meaningful arg name. If evidence is ambiguous, the pipeline should keep the literal and let bridge failure/demotion create a better future synthesis job.

This avoids confusing operators with invented parameters like `x1` and avoids over-general pipelines that silently operate on the wrong data.

## verify patterns

Forward synthesis should strengthen verification beyond `bool(rows)`:

- required fields for row dictionaries
- list vs dict type checks
- write result `ok` checks
- row-shape consistency when all successful samples share a schema

The coordinator uses `verify_strategy` when materializing `_pending/` pipeline files, and smoke testing catches syntax/runtime errors before files are written.
