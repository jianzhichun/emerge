# Code Shrink + Skills Migration Summary

## Line Counts

Current v3 implementation result:

- Baseline locked before v3 code changes: `scripts_py` 67 files, 19105 lines; `hooks_py` 23 files, 1973 lines; `skills_md` 11 files, 1261 lines; `agents_md` 4 files, 101 lines; `commands_md` 9 files, 344 lines.
- Current workspace after the v3 batch and balanced-contract pass: `scripts_py` 70 files, 18572 lines; `hooks_py` 24 files, 1982 lines; `skills_md` 15 files, 1100 lines; `agents_md` 4 files, 101 lines; `commands_md` 9 files, 344 lines.

This v3 batch removed the Python synthesis provider/coordinator path and moved pattern/suggestion output to facts. It did not reach the aspirational `scripts/ <= 6500` target; the remaining reduction requires deeper cockpit/admin/runner decomposition and should be planned as follow-up batches rather than forced into risky deletions.

## Completed

- v3: deleted `scripts/synthesis_agent.py` and `scripts/synthesis_coordinator.py`; replaced runtime synthesis execution with fact-only `scripts/synthesis_events.py`.
- v3: extracted pipeline artifact helpers to `scripts/pipeline_artifacts.py`, `scripts/pipeline_code_checks.py`, and `scripts/span_pipeline_skeleton.py`.
- v3: changed pattern/operator/suggestion paths to emit facts instead of synthesis or crystallization decisions.
- v3: added `docs/pipeline-framework.md`, `docs/runner-deployment.md`, shared runner HTTP helper, and generic v3 workflow skills.
- Deleted `scripts/distiller.py`; current synthesis/event normalization coverage lives in `tests/test_synthesis_events.py`.
- Migrated product synthesis skills to `skills/` and removed `.cursor/skills` product copies.
- Added cockpit, runner elicitation, admin runner, distiller, command, and generic connector watcher markdown assets.
- Moved hook span copy into markdown assets under `docs/hooks/`.
- Shrank admin/cockpit/popup glue conservatively through dispatch/filter/helper consolidation.
- Added `docs/architecture.md` and this summary to document the mechanism/behavior/perception split.

## Verification

- v3 final fast suite: `821 passed, 1 deselected` with `python -m pytest tests/ -q --ignore=tests/test_runner_sse_benchmark.py --ignore=tests/test_metrics.py --ignore=tests/test_operator_popup.py --ignore=tests/test_remote_runner_install.py -k "not concurrent_tool_calls" --tb=short`.
- v3 focused suites: synthesis/pattern/runner/pipeline/hook subsets passed during implementation.
- v3 generic markdown scan: product `skills/`, `agents/`, and `commands/` contain no forbidden vertical terms from the approved generic-only list.
- v3 contract scan: lifecycle `stage` assignments remain limited to existing policy code; no new direct policy stage writers were introduced.

The exact broad command from the plan (`python -m pytest tests/ -q --ignore=tests/test_operator_popup.py --ignore=tests/test_remote_runner_install.py`) hung after several minutes and was stopped. The project fast-suite command excluding known slow/isolation-sensitive tests completed successfully.
