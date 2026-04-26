# Changelog

## 2026-04-26

- Removed `scripts/distiller.py`; reverse synthesis now normalizes observed intent names inside `scripts/synthesis_agent.py`, and the orphan `intent_confirmed` event writer was deleted.
- Moved product synthesis skills from `.cursor/skills/` to repository `skills/`.
- Added markdown behavior assets for cockpit rendering, runner elicitation policy, admin runner operations, stuck flywheel diagnosis, batch runner updates, forward distillation, and connector watcher agents.
- Moved hook span copy into `docs/hooks/` while keeping hook JSON output and state guards in Python.
- Reduced low-risk admin/cockpit glue with table-driven CLI dispatch, shared JSONL filtering, shared query parsing, and compact popup type dispatch.
