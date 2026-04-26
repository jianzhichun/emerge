# Code Shrink + Skills Migration Summary

## Line Counts

Baseline before implementation:

- `scripts_py`: 68 files, 19168 lines
- `hooks_py`: 23 files, 1953 lines
- `skills_md`: 6 files, 1051 lines
- `cursor_skills_md`: 2 files, 117 lines
- `agents_md`: 2 files, 67 lines
- `commands_md`: 7 files, 311 lines

After this pass:

- `scripts_py`: 67 files, 19105 lines
- `hooks_py`: 23 files, 1973 lines
- `skills_md`: 11 files, 1261 lines
- `cursor_skills_md`: 0 files, 0 lines
- `agents_md`: 4 files, 102 lines
- `commands_md`: 9 files, 344 lines

This pass did not reach the aspirational `scripts/ < 8000` target. It completed the behavior-layer migration and low-risk shrink without forcing high-risk runtime rewrites in cockpit, runner installers, or popup rendering.

## Completed

- Deleted `scripts/distiller.py` and moved normalization coverage to `tests/test_synthesis_agent.py`.
- Migrated product synthesis skills to `skills/` and removed `.cursor/skills` product copies.
- Added cockpit, runner elicitation, admin runner, distiller, command, and generic connector watcher markdown assets.
- Moved hook span copy into markdown assets under `docs/hooks/`.
- Shrank admin/cockpit/popup glue conservatively through dispatch/filter/helper consolidation.
- Added `docs/architecture.md` and this summary to document the mechanism/behavior/perception split.

## Verification

- Baseline core subset: `116 passed`
- Distiller migration subset: `20 passed`
- Markdown asset validation: `6 passed`
- Hook tests: `62 passed`
- Runner/admin tests: `47 passed`
- Cockpit/API tests: `64 passed`
- Popup/runner event tests: `27 passed`
- Final fast suite: `840 passed, 1 deselected`
- Final core subset: `119 passed`
- Contract grep: stage writes remain in `scripts/policy_engine.py`; `IntentRegistry.save` direct calls were not introduced.

The exact broad command from the plan (`python -m pytest tests/ -q --ignore=tests/test_operator_popup.py --ignore=tests/test_remote_runner_install.py`) hung after several minutes and was stopped. The project fast-suite command excluding known slow/isolation-sensitive tests completed successfully.
