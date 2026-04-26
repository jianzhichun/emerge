# Pipeline Framework

Emerge pipelines are the deterministic runtime path for learned muscle memory. They receive `connector`, `mode`, `pipeline`, and args, then return structured results without LLM inference.

## Layout

Connector roots are searched in order:

1. `EMERGE_CONNECTOR_ROOT`, when set.
2. `~/.emerge/connectors`.
3. The plugin repository `connectors/` directory.

Each pipeline lives under:

```text
<connector>/pipelines/<read|write|workflow>/<name>.yaml
<connector>/pipelines/<read|write|workflow>/<name>.py
```

YAML-only scenario pipelines omit the `.py` file and are executed by `YAMLScenarioEngine`.

## Contracts

Read pipelines implement `run_read(metadata, args)` and may implement `verify_read(metadata, args, rows)`. Results are normalized to:

```json
{"rows": [], "verify_result": {"ok": true}, "verification_state": "verified"}
```

Write pipelines implement `run_write(metadata, args)` and must implement `verify_write(metadata, args, action_result)`. When verification fails, `rollback_or_stop_policy` controls whether the engine calls `rollback_write` or marks `stop_triggered`.

Workflow pipelines are YAML scenarios that return action and verification payloads from their steps.

## YAML Metadata

Metadata must be strict YAML, not JSON-style inline objects. Required fields:

- `intent_signature`
- exactly one of `read_steps`, `write_steps`, or `workflow_steps`
- `verify_steps`
- `rollback_or_stop_policy: stop|rollback`

Invalid metadata fails before code execution so broken artifacts do not enter the bridge path silently.

## Bridge Semantics

Stable intents use `FlywheelBridge` to call `PipelineEngine` directly. Bridge execution records success or bridge failure evidence through `PolicyEngine.record_bridge_outcome`, but the pipeline engine itself only executes deterministic artifacts.

## Failure Rules

- Missing pipeline files raise `PipelineMissingError` with the searched roots.
- Path segments reject traversal and unsafe names.
- Write pipelines without `verify_write` fail fast.
- YAML step failures in `verify` degrade verification rather than crashing the whole read/write call.
