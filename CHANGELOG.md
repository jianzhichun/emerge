# Changelog

## 2026-04-26

- Began v3 implementation: removed Python synthesis provider/coordinator code, replaced it with fact-only `scripts/synthesis_events.py`, and moved synthesis judgment to generic skills.
- Split pipeline artifact mechanisms into `pipeline_artifacts.py`, `pipeline_code_checks.py`, and `span_pipeline_skeleton.py`.
- Changed pattern/operator/suggestion paths to emit facts (`pattern_observed`, `local_pattern_observed`, `pattern_aggregated`) instead of triggering synthesis decisions.
- Added `docs/pipeline-framework.md` and `docs/runner-deployment.md`; extracted shared runner no-proxy HTTP helper.
- Added generic v3 workflow skills: `distill-from-pattern`, `crystallize-from-wal`, `aggregate-suggestions`, and `judge-promote-flywheel`.
- Removed `scripts/distiller.py`; intent normalization now lives in mechanism helpers, and the orphan `intent_confirmed` event writer was deleted.
- Moved product synthesis skills from `.cursor/skills/` to repository `skills/`.
- Added markdown behavior assets for cockpit rendering, runner elicitation policy, admin runner operations, stuck flywheel diagnosis, batch runner updates, forward distillation, and connector watcher agents.
- Moved hook span copy into `docs/hooks/` while keeping hook JSON output and state guards in Python.
- Reduced low-risk admin/cockpit glue with table-driven CLI dispatch, shared JSONL filtering, shared query parsing, and compact popup type dispatch.
