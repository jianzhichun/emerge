# Emerge v3 Audit

> Historical Step 0 audit. The v3 implementation has since deleted the Python synthesis provider/coordinator path, added fact-only synthesis events, and moved pattern/suggestion output to observations. Use `summary.md` and current tests as the post-implementation baseline; this document remains as the rationale for the original approval decisions.

## Status

This is the Step 0 audit for the v3 architecture direction. It is intentionally audit-only: no delete, rewrite, or framework reshaping is approved by this document.

Baseline: current `main` at `4b69cb3` after the previous skills migration. This differs from the v3 brief baseline (`059fb50`), so all counts below use the current checkout.

## Architecture Decision Under Audit

Emerge code should narrow to two mechanism frameworks:

- Pipeline framework: deterministic loading, execution, WAL/checkpoint, bridge, and registry persistence.
- Runner framework: observation transport, local execution, outbox, lifecycle, and daemon communication.

All intelligent behavior should move to generic Claude Code skills, agents, and commands. Product assets must not hard-code verticals such as HM, ZWCAD, SolidWorks, CATIA, or FOCUS; vertical knowledge belongs in connector-local config such as `NOTES.md` and `watcher_profile.yaml`.

## Counting Method

Counts are physical line counts from `/Users/apple/Documents/workspace/emerge` on current `main`.

| Area | Files | Lines |
| --- | ---: | ---: |
| `scripts/**/*.py` | 67 | 19,105 |
| `hooks/*.py` | 23 | 1,973 |
| `skills/**/*.md` | 11 | 1,261 |
| `agents/*.md` | 4 | 101 |
| `commands/*.md` | 9 | 344 |

The v3 prompt quotes `scripts/` as 11,642 lines. Current recursive `scripts/**/*.py` count is 19,105 because the baseline now includes the prior LLM-distillation and skills migration work. Future summary must state both the chosen baseline and the counting command.

## Deletion / Simplification Candidates

| File | Lines | Current callers / tests | Audit conclusion | Replacement path |
| --- | ---: | --- | --- | --- |
| `scripts/synthesis_agent.py` | 378 | `scripts/daemon_http.py`, `scripts/operator_monitor.py`, tests for synthesis agent, reverse e2e, daemon HTTP, operator monitor | Delete after replacing reverse synthesis orchestration. It still packages jobs, exposes provider compatibility, and contains environment variables `EMERGE_SYNTHESIS_*`; these are intelligent-path glue under v3. | Emit raw `pattern_pending_synthesis` / `synthesis_job_ready` facts only. Generic `skills/distill-from-pattern` guides Claude to inspect events and use MCP primitives. |
| `scripts/synthesis_coordinator.py` | 378 | `scripts/emerge_daemon.py`, `scripts/crystallizer.py`, MCP schema/tests for `icc_synthesis_submit`, forward synthesis and smoke tests | Delete or shrink to a minimal pipeline-write primitive only after deciding the replacement MCP surface. Current smoke/materialization logic is a Python distillation coordinator. | Replace with explicit mechanism tools such as “write pending pipeline artifact” and “record synthesis blocked”; decision/smoke strategy belongs in generic skills. |
| `scripts/crystallizer.py` | 569 | `scripts/emerge_daemon.py`, `scripts/mcp/span_handler.py`, `hooks/post_tool_use.py`, `scripts/sync/asset_ops.py`, crystallize tests, sync tests, hook tests | High-risk delete. It owns YAML dumping helpers, span skeleton generation, `_code_assigns_name`, and legacy crystallization. Some helpers are mechanism or reused by sync/span paths. | Split first: move YAML/schema/code-assignment primitives to small mechanism modules; move WAL-to-pipeline reasoning to `skills/crystallize-from-wal`. Remove `icc_crystallize` only after replacement tests pass. |
| `scripts/pattern_detector.py` | 140 | `scripts/daemon_http.py`, `scripts/operator_monitor.py`, `tests/test_pattern_detector.py`, synthesis tests | Simplify, not blind delete. Current thresholds (`FREQ_THRESHOLD`, `ERROR_RATE_THRESHOLD`, cross-machine criteria) are policy decisions in code. Grouping/window metrics are useful mechanism facts. | Keep pure event grouping/stat functions and emit `pattern_metrics` facts. Generic `skills/aggregate-suggestions` or `skills/distill-from-pattern` decides whether a pattern matters. |
| `scripts/operator_monitor.py` | 116 | `scripts/emerge_daemon.py`, `tests/test_operator_monitor.py`, MCP integration tests | Simplify or merge into event routing. It reads event files, buffers windows, invokes detector, writes `local_pattern_alert`, and optionally calls synthesis agent. | Keep file watching / forwarding mechanism; remove direct synthesis-agent invocation and alert judgment. Emit local event batches/metrics for Claude. |
| `scripts/orchestrator/suggestion_aggregator.py` | 188 | `scripts/daemon_http.py`, `tests/test_suggestion_aggregator.py` | Simplify. It mixes dedupe/persistence with trigger decisions (`min_runners`, `min_occurrences_single_runner`, retrigger threshold). | Keep dedupe, persistence, and parameter-range facts; replace `_maybe_trigger` with `pattern_aggregated` events judged by `skills/aggregate-suggestions`. |
| `scripts/admin/cockpit.py` | 934 | Cockpit/server/SSE/static/CORS/API tests, daemon HTTP tests | Refactor-shrink, not wholesale delete. HTTP/static/SSE/CORS/body-limit code is mechanism. Large route dispatch and some view shaping can shrink. | Keep HTTP transport and stable API shape. Move admin workflows to commands; keep summarization only as deterministic projections. |

## Pipeline Framework Audit

Mechanism files:

| File | Lines | Role |
| --- | ---: | --- |
| `scripts/pipeline_engine.py` | 548 | Load and run Python/YAML pipelines, lifecycle hooks, bridge execution. |
| `scripts/pipeline_yaml_engine.py` | 441 | Execute YAML scenarios and rollback steps. |
| `scripts/exec_session.py` | 537 | Persistent Python session, WAL, replay, checkpoint, stdout/stderr caps. |
| `scripts/intent_registry.py` | 113 | Atomic registry persistence API. |
| `scripts/policy_config.py` | 345 | Shared constants, paths, settings, atomic JSON helpers. |
| `scripts/mcp/bridge.py` | 316 | Stable intent bridge and bridge failure classification. |

Subtotal: about 2,300 lines.

Conservative improvement opportunities for later phases:

- Extract duplicate metadata/schema parsing into `scripts/pipeline_metadata.py`.
- Extract execution/verification helpers only if tests prove behavior-preserving.
- Standardize exception hierarchy around `PipelineMissingError` / YAML errors.
- Add direct unit tests for bridge classification if gaps remain beyond `tests/test_bridge_classifier.py`.
- Write `docs/pipeline-framework.md` covering load order, pipeline file layout, WAL, verify, and bridge failure semantics.

Do not change WAL replay order, bridge failure classification, or tested YAML engine semantics during deletion phases.

## Runner Framework Audit

Mechanism files:

| File | Lines | Role |
| --- | ---: | --- |
| `scripts/remote_runner.py` | 756 | Runner HTTP server, executor, SSE client, popup dispatch. |
| `scripts/runner_client.py` | 328 | Daemon-side runner client/router. |
| `scripts/runner_emit.py` | 174 | Runner event emission and outbox. |
| `scripts/runner_watchdog.py` | 168 | Runner process supervision. |
| `scripts/runner_state_service.py` | 113 | Connected runner state snapshots. |
| `scripts/runner_sync.py` | 139 | Runner script/version sync. |
| `scripts/node_role.py` | 22 | Orchestrator vs runner role detection. |
| `scripts/runner_policy.py` | 139 | Runner-side evidence forwarding boundary. |

Subtotal: about 1,839 lines.

Conservative improvement opportunities for later phases:

- Split `remote_runner.py` by transport, executor, and SSE client while preserving imports.
- Unify no-proxy HTTP JSON helpers across runner client, runner emit, and remote runner forwarding.
- Document runner lifecycle and deployment in `docs/runner-deployment.md`.
- Add targeted tests for `runner_state_service.py`, `runner_sync.py`, and `runner_policy.py` if coverage is only indirect.
- Preserve runner rule: runners forward facts and execute local mechanisms; they do not decide pattern significance, promotion, or crystallization.

## Hooks Audit

Current hook subset sampled:

| File | Lines | Classification |
| --- | ---: | --- |
| `hooks/session_start.py` | 150 | Mechanism + markdown-loaded prompt copy. |
| `hooks/user_prompt_submit.py` | 88 | Mechanism + markdown-loaded reminder copy. |
| `hooks/pre_tool_use.py` | 313 | Mechanism guard. |
| `hooks/post_tool_use.py` | 287 | Mechanism recorder / active-span buffer. |
| `hooks/stop.py` | 42 | Mechanism guard. |
| `hooks/stop_failure.py` | 69 | Mechanism cleanup. |

The prior migration already moved some span copy to `docs/hooks/`. Future hook shrink should merge repeated span-open cleanup/guard logic carefully, but guard hooks are not “intelligence” and should remain deterministic Python.

## Generic Markdown / Agent / Command Audit

Current product assets:

| Area | Files | Notes |
| --- | ---: | --- |
| `skills/` | 11 | Includes synthesis/admin/runner/cockpit skills plus older operator-flow skills. |
| `agents/` | 4 | Generic watcher template, operator watcher, forward distiller, README. |
| `commands/` | 9 | Admin, cockpit, monitor, import/export, hub commands. |

Vertical-token scan found residual examples:

- `skills/distilling-operator-flows/SKILL.md`: mentions `zwcad` and `SolidWorks`.
- `skills/emerge-reverse-synthesis/SKILL.md`: mentions `zwcad`.
- `skills/initializing-vertical-flywheel/SKILL.md`: mentions `zwcad`.
- `skills/remote-runner-dev/SKILL.md`: mentions `hm` and `zwcad`.
- `agents/README.md`: uses `hypermesh` in example `watcher_profile.yaml`.
- `skills/cockpit-rendering/SKILL.md` and `skills/policy-optimization/SKILL.md` contain the word `focus`; this may be ordinary English, not necessarily FOCUS6, but should be reviewed.

Audit conclusion: v3’s generic-only rule requires replacing vertical examples with `mock`, `cad_generic`, or abstract connector placeholders in product assets. Real connector examples should move under `connectors/<name>/NOTES.md`, `connectors/<name>/watcher_profile.yaml`, or test fixtures.

Required generic assets for v3 approval:

| Proposed asset | Purpose |
| --- | --- |
| `skills/distill-from-pattern/SKILL.md` | Replace reverse synthesis code path; guide Claude from raw event facts to verified intent execution. |
| `skills/crystallize-from-wal/SKILL.md` | Replace WAL-to-pipeline crystallizer reasoning. |
| `skills/judge-promote-flywheel/SKILL.md` | Move promotion judgment out of policy thresholds if approved. |
| `skills/aggregate-suggestions/SKILL.md` | Move suggestion-trigger judgment out of Python. |
| `skills/pipeline-framework/SKILL.md` or `docs/pipeline-framework.md` | Teach contributors and Claude the deterministic pipeline framework. |
| `docs/runner-deployment.md` | Runner install/lifecycle/outbox deployment guide. |
| `agents/connector-watcher-template.md` | Generic watcher template only; no product-shipped vertical watcher files. |

## Approval Decisions Required

1. **Line-count baseline**: approve current `main` (`4b69cb3`, 19,105 recursive script lines) as the v3 baseline, or require a historical comparison to `059fb50`.
2. **Synthesis removal scope**: approve deleting both `synthesis_agent.py` and `synthesis_coordinator.py`, or keep a minimal artifact-writing primitive extracted from `synthesis_coordinator.py`.
3. **Crystallizer split**: approve splitting `crystallizer.py` into mechanism primitives before deletion. Direct deletion would break daemon/span/sync callers.
4. **Policy judgment migration**: approve whether `PolicyEngine._derive_transition` is actually in scope. The v3 prompt both says “move judgment to skill” and later says “do not touch `_derive_transition`”; this conflict needs author decision before code changes.
5. **Pattern detector behavior**: approve replacing threshold alerts with fact emission only.
6. **Generic-only markdown**: approve removing all vertical examples from product `skills/`, `agents/`, and `commands`, using only placeholders or mock examples.
7. **Cockpit shrink level**: approve a conservative route/helper shrink first, not a full backend rewrite.

## Implementation Guardrails After Approval

- One delete/split per commit.
- Test-first for every deleted intelligent component: update or replace tests before removing imports.
- No new Python LLM providers, null providers, or compatibility provider abstractions.
- No product-level vertical skills/agents/commands.
- Preserve `stage` writes and registry mutation contracts unless a specific approved phase changes them.
- Keep runner role boundaries: runner forwards evidence; orchestrator/main Claude judges.

## Verification Commands For Later Phases

```bash
python -m pytest tests/ -q --ignore=tests/test_runner_sse_benchmark.py --ignore=tests/test_metrics.py --ignore=tests/test_operator_popup.py --ignore=tests/test_remote_runner_install.py -k "not concurrent_tool_calls"
python -m pytest tests/test_pipeline_engine.py tests/test_pipeline_yaml_engine.py tests/test_exec_kernel.py tests/test_remote_runner.py tests/test_runner_outbox.py tests/test_runner_retry.py tests/test_bridge_classifier.py -q
```

Contract checks:

```bash
rg '\["stage"\]\s*=' scripts/
rg 'IntentRegistry\.save' scripts/
```

Markdown generic-only checks:

```bash
python - <<'PY'
from pathlib import Path
root = Path.cwd()
tokens = {"hm", "zwcad", "solidworks", "catia", "focus6", "hypermesh"}
for base in ("skills", "agents", "commands"):
    for path in (root / base).rglob("*.md"):
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        found = sorted(t for t in tokens if t in text)
        if found:
            print(path, found)
PY
```

## Recommended Next Step

Stop here for author approval. If approved, begin with the safest sequence:

1. Generic-only markdown cleanup and tests.
2. Extract crystallizer mechanism primitives.
3. Delete reverse synthesis provider path.
4. Delete or shrink forward synthesis coordinator.
5. Simplify pattern/suggestion decision logic to fact emission.
6. Decorate Pipeline and Runner frameworks with docs, smaller modules, and targeted tests.
