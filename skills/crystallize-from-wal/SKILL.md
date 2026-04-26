---
name: crystallize-from-wal
description: Use when an Emerge intent is marked synthesis_ready and Claude must inspect successful WAL samples to create a conservative pending pipeline artifact.
---

# Crystallize From WAL

Use this when an intent has `synthesis_ready` evidence. The runtime provides WAL facts; Claude performs parameter selection and artifact authoring.

## Workflow

1. Gather all successful WAL samples for the intent.
2. Compare samples to separate constants from inputs.
3. Name parameters by domain meaning, using connector-local `NOTES.md` when available.
4. Create a strict YAML scenario or Python pipeline under the connector's pending pipeline area.
5. Include verification that checks structure, required fields, and success conditions.
6. Run the narrowest test or `icc_exec` smoke check that proves the artifact loads.

## Rules

- Do not preserve debug prints, temporary tracing, or one-off local paths.
- Do not promote the artifact directly unless a mechanism tool explicitly performs that transition.
- If WAL samples conflict, mark synthesis blocked with the reason and the missing evidence.

## Output

Return the pending artifact path, the parameters inferred from WAL, verification evidence, and blockers if any.
