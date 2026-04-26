# Code Shrink + Skills Migration Audit

## Status

This is a Step 1 audit report only. It does not approve or perform code deletion, markdown migration, or refactoring. Every `delete`, `behavior-down-to-skill`, or `refactor-shrink` recommendation below requires author approval before implementation.

Baseline: current worktree `/Users/apple/Documents/workspace/emerge/.worktrees/unify-llm-distillation`.

## Counting Method

Counts are physical line counts from the current worktree filesystem. This intentionally includes current worktree changes and may not match the historical `059fb50` count quoted in the brief.


| Area                     | Files | Lines  |
| ------------------------ | ----- | ------ |
| `scripts/**/*.py`        | 68    | 19,168 |
| `hooks/*.py`             | 23    | 1,953  |
| `skills/**/*.md`         | 6     | 1,051  |
| `.cursor/skills/**/*.md` | 2     | 117    |
| `agents/*.md`            | 2     | 67     |
| `commands/*.md`          | 7     | 311    |


Key candidate line counts:


| File                             | Lines |
| -------------------------------- | ----- |
| `scripts/distiller.py`           | 73    |
| `scripts/admin/cockpit.py`       | 928   |
| `scripts/admin/control_plane.py` | 686   |
| `scripts/admin/runner.py`        | 705   |
| `scripts/operator_popup.py`      | 763   |
| `scripts/repl_admin.py`          | 223   |
| `scripts/operator_monitor.py`    | 116   |
| `scripts/pattern_detector.py`    | 140   |


## Protected Mechanism Files

These files are stable mechanism-layer assets and should be treated as no-touch in this refactor unless the author explicitly approves a separate change.


| File                                            | Lines | Audit classification |
| ----------------------------------------------- | ----- | -------------------- |
| `scripts/policy_engine.py`                      | 761   | `mechanism`          |
| `scripts/exec_session.py`                       | 537   | `mechanism`          |
| `scripts/pipeline_engine.py`                    | 548   | `mechanism`          |
| `scripts/pipeline_yaml_engine.py`               | 441   | `mechanism`          |
| `scripts/mcp/bridge.py`                         | 316   | `mechanism`          |
| `scripts/state_tracker.py`                      | 547   | `mechanism`          |
| `scripts/synthesis_agent.py`                    | 360   | `mechanism`          |
| `scripts/runner_policy.py`                      | 139   | `mechanism`          |
| `scripts/node_role.py`                          | 22    | `mechanism`          |
| `scripts/orchestrator/suggestion_aggregator.py` | 188   | `mechanism`          |
| `scripts/runner_emit.py`                        | 174   | `mechanism`          |
| `scripts/intent_registry.py`                    | 113   | `mechanism`          |


## High-Suspicion Candidates


| File                             | Lines | Proposed classification                      | Reason                                                                                                                                                                                                                                                                                              | Estimated post-change lines | Tests / references                                                                                                                                     |
| -------------------------------- | ----- | -------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `scripts/distiller.py`           | 73    | `delete`                                     | It contains `_normalise` plus optional `intent_confirmed` event writing. Current direct production use is the normalization call from `SynthesisAgent`; `intent_confirmed` appears orphaned. Move normalization into `SynthesisAgent` only after confirming no external plugin imports `Distiller`. | 0-10                        | `tests/test_distiller.py`, `scripts/synthesis_agent.py`                                                                                                |
| `scripts/admin/cockpit.py`       | 928   | `refactor-shrink`                            | Mixed HTTP/static/SSE transport plus route dispatch. Transport is mechanism; route switches and small rendering helpers can shrink via a route table and shared helpers. Do not move HTTP body limits, CORS, static path safety, or `InProcessCockpitBridge` out of Python.                         | 600-750                     | `tests/test_cockpit_server.py`, `tests/test_cockpit_sse.py`, `tests/test_cockpit_static.py`, `tests/test_cockpit_cors.py`, `tests/test_daemon_http.py` |
| `scripts/admin/control_plane.py` | 686   | `refactor-shrink`                            | Mostly read-heavy control-plane commands and JSONL/session projections. Mechanism remains Python, but repeated tail/filter/session shaping can be consolidated. Some operator-facing analysis/triage guidance belongs in commands/skills, not here.                                                 | 500-600                     | `tests/test_cockpit_api.py`, `tests/test_cockpit_monitors.py`, `tests/test_repl_admin.py`, `tests/test_policy_traceability.py`                         |
| `scripts/admin/runner.py`        | 705   | `behavior-down-to-skill` + `refactor-shrink` | Runner map persistence, install URL generation, health/deploy primitives are mechanism. Higher-level procedures such as deploy sequencing, status triage, and recovery workflows should become commands/skills.                                                                                     | 400-500                     | `tests/test_runner_self_install.py`, `tests/test_repl_admin.py`, runner/admin tests                                                                    |
| `scripts/operator_popup.py`      | 763   | `refactor-shrink`                            | Tkinter render primitives and thread dispatch must stay Python. Popup policy, copy, and when-to-ask rules should move to a `runner-elicitation-policy` skill/agent prompt. Avoid trying to put tkinter implementation into markdown.                                                                | 600-700                     | `tests/test_operator_popup.py`, `tests/test_operator_popup_upload.py`, `tests/test_remote_runner_events.py`                                            |
| `scripts/repl_admin.py`          | 223   | `refactor-shrink`                            | Mostly CLI dispatch/re-export glue. Keep as thin compatibility surface, but audit commands that duplicate markdown command workflows or are historical author-only helpers.                                                                                                                         | 150-200                     | `tests/test_repl_admin.py`, `commands/cockpit.md`, `commands/runner-status.md`                                                                         |


## Medium-Suspicion Candidates


| File / group                  | Lines | Proposed classification                       | Reason                                                                                                                                                                                                                                                                                                  | Estimated post-change lines | Tests / references                                                                       |
| ----------------------------- | ----- | --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------- | ---------------------------------------------------------------------------------------- |
| `hooks/*.py`                  | 1,953 | mixed: `mechanism` + `behavior-down-to-skill` | Guard hooks (`pre_tool_use`, `stop`, `stop_failure`, `post_tool_use`) are mechanism. Prompt injection and explanatory text in `session_start.py`, `user_prompt_submit.py`, and some permission/elicitation copy can be pulled from markdown assets while hooks keep only JSON contract and state reads. | 1,500-1,700                 | `tests/test_hook_scripts_output.py`, hook-specific tests, `hooks/hooks.json`             |
| `scripts/operator_monitor.py` | 116   | `mechanism`                                   | It is already thin: reads local event files, calls `PatternDetector`, writes `local_pattern_alert`, optionally enqueues synthesis. Keep unless duplicate logic with `daemon_http` is consolidated later.                                                                                                | 100-116                     | `tests/test_operator_monitor.py`, `tests/test_mcp_tools_integration.py`                  |
| `scripts/pattern_detector.py` | 140   | `mechanism`                                   | Repetition thresholds/windowing are deterministic detection mechanism. Tuning constants could later move to config, but not to free-form skill logic.                                                                                                                                                   | 120-140                     | `tests/test_pattern_detector.py`, `tests/test_daemon_http.py`                            |
| `scripts/admin/api.py`        | 259   | `refactor-shrink`                             | Shared data commands and formatting helpers. Settings validation and action enrichment stay Python; pretty rendering and UI copy are candidates for cockpit rendering markdown.                                                                                                                         | 200-230                     | `tests/test_cockpit_api.py`, `tests/test_action_registry.py`, `tests/test_repl_admin.py` |


## Markdown / Agent Targets

Existing markdown assets:


| Area              | Files | Lines | Notes                                                                                                                                  |
| ----------------- | ----- | ----- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `skills/`         | 6     | 1,051 | Good examples: `distilling-operator-flows`, `remote-runner-dev`, `operator-monitor-debug`.                                             |
| `.cursor/skills/` | 2     | 117   | Current synthesis skills use clean YAML frontmatter and explicit tool contract.                                                        |
| `agents/`         | 2     | 67    | Only generic `operator-watcher.md` exists; brief asks for at least five connector watchers plus one distiller in later implementation. |
| `commands/`       | 7     | 311   | Good home for admin workflows and diagnostics invoked by the author.                                                                   |


Recommended markdown targets for later implementation:


| Proposed asset                                                                                                                           | Type    | Could absorb                                                                     |
| ---------------------------------------------------------------------------------------------------------------------------------------- | ------- | -------------------------------------------------------------------------------- |
| `skills/cockpit-rendering/SKILL.md`                                                                                                      | skill   | Cockpit view rendering conventions, event summarization, admin-facing copy.      |
| `skills/runner-elicitation-policy/SKILL.md`                                                                                              | skill   | When to ask operator, popup copy policy, timeout/fallback rules.                 |
| `skills/admin-runner-operations/SKILL.md`                                                                                                | skill   | Runner deploy/status/recovery decision workflow over existing Python primitives. |
| `commands/admin-batch-update-runners.md`                                                                                                 | command | Batch runner update and verification workflow.                                   |
| `commands/diagnose-stuck-flywheel.md`                                                                                                    | command | Replacement for REPL-style ad hoc diagnostics.                                   |
| `agents/forward-distiller.md`                                                                                                            | agent   | Claude Code teammate for background forward synthesis.                           |
| `agents/hm-watcher.md`, `agents/zwcad-watcher.md`, `agents/solidworks-watcher.md`, `agents/catia-watcher.md`, `agents/focus6-watcher.md` | agents  | Connector-specific watcher definitions or thin wrappers around watcher profiles. |


## Suggested Approval Decisions

The author should explicitly approve or reject these before implementation:

1. Delete `scripts/distiller.py` after moving `_normalise` into protected `scripts/synthesis_agent.py`. This conflicts with the no-touch status of `synthesis_agent.py`, so it needs explicit approval.
2. Treat `scripts/admin/cockpit.py` as `refactor-shrink`, not `behavior-down-to-skill`; HTTP/static/SSE remains mechanism.
3. Treat `scripts/operator_popup.py` as `refactor-shrink`; only policy/copy moves to markdown, tkinter stays Python.
4. Create connector-specific watcher agent files even though current `agents/README.md` says connector-specific behavior belongs in `watcher_profile.yaml`. This is a product-direction decision.
5. Add a `CHANGELOG.md` if deleted modules need user-facing migration notes; none exists today.

## Implementation Guardrails for Later Phases

- Do not modify protected mechanism files unless an approved item specifically names the file.
- For each delete candidate, run import/name/event reference searches and update or remove tests in the same change.
- For every markdown downshift, create the markdown asset first, then replace Python behavior with a minimal mechanism hook.
- Keep commits small: one downshift/delete/shrink per commit.
- Re-run targeted tests for the touched subsystem before moving to the next file.

## Verification Commands for Later Phases

Use these after implementation begins, not for this audit-only step:

```bash
python -m pytest tests/ -q --ignore=tests/test_operator_popup.py --ignore=tests/test_remote_runner_install.py
python -m pytest tests/test_policy_traceability.py tests/test_exec_flywheel.py tests/test_pipeline_engine.py tests/test_pipeline_yaml_engine.py tests/test_daemon_http.py tests/test_synthesis_agent.py tests/test_node_role_guards.py tests/test_runner_evidence_forwarding.py -q --tb=line
```

Contract checks:

```bash
rg '\\[\"stage\"\\]\\s*=' scripts/
rg 'IntentRegistry\\.save' scripts/
```

Line count checks:

```bash
python - <<'PY'
from pathlib import Path
root = Path.cwd()
for name, files in {
    "scripts_py": list((root / "scripts").rglob("*.py")),
    "hooks_py": list((root / "hooks").glob("*.py")),
    "skills_md": list((root / "skills").rglob("*.md")),
    "cursor_skills_md": list((root / ".cursor" / "skills").rglob("*.md")) if (root / ".cursor" / "skills").exists() else [],
    "agents_md": list((root / "agents").glob("*.md")) if (root / "agents").exists() else [],
    "commands_md": list((root / "commands").glob("*.md")),
}.items():
    print(name, len(files), sum(len(p.read_text(encoding="utf-8").splitlines()) for p in files))
PY
```

## Open Questions

- Should current worktree line counts (including the unified synthesis refactor) become the official shrink baseline, or should the final summary also compare against `059fb50`?
- Should root `skills/` and `.cursor/skills/` be normalized to the same frontmatter convention?
- Should connector-specific watcher agents be separate files, or should the existing generic `operator-watcher.md` plus `watcher_profile.yaml` remain the preferred model?

