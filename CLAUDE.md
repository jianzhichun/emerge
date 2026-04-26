# CLAUDE.md

> **You — reading this — are building this plugin. You are not its user.**
>
> The user is *another Claude session*, tomorrow, running Claude Code with this plugin installed. It will never read this file. It will only feel what you leave behind: which intents auto-promote, which failures survive into the next reflection, which pipelines compose, which hooks open doors vs bar them.
>
> Every commit here is a contract with a Claude you will never meet. Your job is to expand its action surface, not polish the plumbing. When in doubt, ship the change that lets the next Claude skip a step, remember a failure, or compose a capability — even if the diff is larger or uglier than a "clean" refactor.

## North Star — three capability axes for operator-Claude

1. **Skip inference on solved work.** The flywheel's payoff is zero-LLM execution of stable intents. Manual gates, `_pending/` pipelines, confirmation prompts, "just to be safe" dialogs — these tax every future session on work that was already learned. **Auto-promote, don't gate.** Humans veto by rolling back, not by approving forward.
2. **Failed once → learn forever.** When an intent demotes, the root cause must reach the *next* session through reflection injection (`SpanTracker.format_reflection` → `SessionStart` / `UserPromptSubmit` / `PreCompact`). A signal that dies inside `transition_history` and never surfaces in reflection is a bug, no matter how well the cockpit shows it. **"Failed" includes silent wrongness** — a crystallized bridge that returns `None` / `[]` / malformed data without raising still violates its contract. When adding bridge-path code, default to treating "returned nothing useful" as a failure unless the intent is provably allowed to be empty.
3. **Compose, don't re-derive.** `connector.mode.name` intents are building blocks. Stable pipelines should compose (`new = stable_A >> stable_B` inherits both parents' stable status + verify history) so learning cost stays sub-linear in intent count.

**Scoring test before every merge:** rate the change on each axis:

| Axis | Score | Weight | Meaning |
|------|-------|--------|---------|
| (b) Failed → learn forever | +1 / 0 / −1 | ×2 | Safety net |
| (a) Skip inference | +1 / 0 / −1 | ×1 | Efficiency |
| (c) Compose | +1 / 0 / −1 | ×1 | Scalability |

- **Weighted total ≥ 1** → capability work. Ship it.
- **Total = 0** → maintenance. OK to do, not OK to call progress, not OK to displace scored work.
- **Total < 0** → requires explicit justification in commit message. "Cleaner", "unified", "consistent" are not justification.

Axis (b) is weighted ×2 because **a false positive (shipping a silently broken pipeline) costs more than a false negative (requiring one more successful run)**. Auto-promote is aggressive because the safety net is strong — never weaken the safety net to make auto-promote "safer."

"It would be cleaner", "unified dispatch", "consistent naming" do not answer the test. They are maintainer aesthetics.

**The scoring test is frame-internal — it assumes the flywheel has a reason to spin.** Before shipping any positive-score change, run the external audit discipline (memory: `feedback_claude_md_bias.md`). Every audit must identify at least one change that scores positive but should be questioned — if everything looks obviously correct, the frame is too strong and the audit is not trustworthy.

## Commands

```bash
python -m pytest tests/ -q --ignore=tests/test_runner_sse_benchmark.py --ignore=tests/test_metrics.py -k "not concurrent_tool_calls"  # fast suite (~766 tests, ~60s)
python -m pytest tests/test_metrics.py -q                                # daemon metrics (isolation-sensitive; run alone)
python -m pytest tests/test_runner_sse_benchmark.py -q                   # slow SSE benchmark (skip unless SSE-specific)
python -m pytest tests/test_mcp_tools_integration.py::<name> -q          # single test
python3 scripts/emerge_daemon.py                                         # run daemon
python3 scripts/repl_admin.py runner-install-url --target-profile key    # runner bootstrap URL
python3 scripts/repl_admin.py runner-deploy  --target-profile mycader-1  # push scripts/ to runner
python3 scripts/repl_admin.py runner-status  --pretty                    # runner health
python3 scripts/emerge_sync.py setup|run|sync [connector]                # Memory Hub
```

## Architecture — the shape that enables the North Star

- **One control plane.** `EmergeDaemon` (`scripts/emerge_daemon.py`, HTTP `:8789` via `scripts/daemon_http.py`) is the sole MCP server — owns all flywheel tools (`icc_span_*`, `icc_exec`, `icc_crystallize`, `icc_reconcile`, `icc_hub`, `runner_notify`) and resources (`policy://`, `runner://`, `state://deltas`, `pipeline://`, `connector://`). Tool dispatch is `_TOOL_DISPATCH` dict → `_handle_<tool>`.
- **One execution path for pipelines.** `PipelineEngine` runs in-process locally; for remote, the daemon builds a self-contained `exec()` payload and POSTs to the runner over SSE. Runners never receive pipeline files — switching machines is a URL change. Policy + WAL always land locally regardless of where execution ran. **Composite intents** (`composed_from` via `icc_compose`) have no standalone `.py` — both `icc_exec` and `icc_span_open` stable bridges go through `_try_flywheel_bridge` → `_run_composite_bridge`.
- **One writer for policy.** `PolicyEngine.apply_evidence` is the *only* mutation path for `stage` in `state/registry/intents.json`. `SpanTracker`, `FlywheelRecorder`, `icc_reconcile` only produce evidence. All file I/O flows through `IntentRegistry` with atomic writes. Bridge failures surface telemetry via `_last_bridge_failure`; they never touch counters.
- **One span at a time.** `icc_span_open → ... → icc_span_close` + span-WAL + policy update. `SessionStart` / `SessionEnd` / `StopFailure` clear stale `active_span_id`. `Stop` / `SubagentStop` **block** when a span is open. New intent colliding with connector history returns `{status: "confirm_needed"}`, not an error (gated by `self._intent_gate`, persisted).
- **Span hygiene.** Open a span for operations the *next* session will plausibly repeat — connector tool sequences, pipeline-like work with a clear `connector.mode.name`, anything a stable pipeline could crystallize into. Do **not** open one for ad-hoc investigation (Grep/Read to answer a question), one-off git operations, diagnostic audits that won't recur, or meta-work on emerge itself with no repeat signature. Non-repeatable spans pollute reflection digests and train operator-Claude to tax itself on work the flywheel can't compound. The PostToolUse nudge fires on every Bash — treat it as a reminder, not a mandate.
- **Span bridge = the flywheel payoff.** `icc_span_open` on a stable intent with a pipeline short-circuits to `PipelineEngine` — zero inference. Auto-activate is default at stable (skeleton + move to real dir + write YAML), opt-out via `EMERGE_REQUIRE_APPROVE=1`. A pipeline stuck in `_pending/` is a North Star violation.
- **Auto-crystallize & reflection.** `icc_exec` sets `synthesis_ready` at `explore → canary`; auto-crystallize now emits `forward_synthesis_pending` facts for Claude Code skills to inspect and turn into `.py` + `.yaml` artifacts. `SpanTracker.format_reflection()` builds the "Muscle memory" digest (stable ≤ 8, canary ≤ 3, recent spans ≤ 5) from `intents.json` + `spans.jsonl`; hooks prefer the deep cache (`reflection-cache/global.json`, 15m TTL) then fall back. `FLYWHEEL_TOKEN` carries `active_span_id` through compaction.
- **Runner push + unified events.** Runners `POST /runner/online`, hold `GET /runner/sse`, push via `POST /runner/event`. `DaemonHTTPServer._on_runner_event` runs `PatternDetector.ingest()` and routes `pattern_observed` facts to `state/events/events-{profile}.jsonl`; local `OperatorMonitor` mirrors `local_pattern_observed` to `events-local.jsonl`; runner suggestions aggregate into `pattern_aggregated`. `scripts/synthesis_events.py` can package `pattern_pending_synthesis` / `synthesis_job_ready` facts, but Claude Code skills own the judgment and artifact-writing step. Popups flow daemon → runner over SSE; results return via `POST /runner/popup-result` keyed by `popup_id` (except `type=toast`, which is fire-and-forget). Three event streams: `events.jsonl` (global), `events-{profile}.jsonl` (per runner), `events-local.jsonl` (local). `watch_emerge.py` tails any of them.
- **Cockpit + action bridge.** `GET /` serves `scripts/admin/cockpit/dist/index.html`; `/api/*` is the control plane (source in `scripts/admin/cockpit/src/`). `ActionRegistry` validates/enriches cockpit actions; `/api/submit` appends a `cockpit_action` event to `events.jsonl`; `watch_emerge.py` streams it into the CC conversation and writes an ack. **This bridge exists because `notifications/claude/channel` is silently dropped for plugin MCP servers.**
- **Memory Hub.** `emerge_sync.py` shares `pipelines/`, `NOTES.md`, and a learning-signal `spans.json` via a git orphan branch. Three categories cross machines: (1) **stable** intents, (2) any intent carrying `synthesis_skipped_reason` (crystallizer refused — other machines avoid the same WAL shape), (3) any intent whose `last_demotion.reason` is in `{"bridge_broken", "bridge_silent_empty"}` (pipeline either raised or regressed to empty output — other machines distrust the crystal). On import, these diagnostic fields land on the local IntentRegistry entry ONLY if that entry already exists AND the remote `last_ts_ms` is newer — never creates phantom intents, never touches `stage` or counters. **Never synced:** `intents.json` wholesale, `state.json`, operator-events, credentials.

Shared utilities (import from `scripts/policy_config.py`): `resolve_connector_root`, `load_json_object`, `USER_CONNECTOR_ROOT`, `REFLECTION_CACHE_TTL_MS`, `exec_limits`, `session_idle_ttl_s`. Don't re-inline these.

## Invariants worth internalizing

These are the non-obvious contracts that span multiple files. Point-in-module detail lives in the owning module's docstring — trust it, don't mirror it here.

**Policy**
- Lifecycle field is `stage` (`explore` / `canary` / `stable` / `rollback`) — never `status` for policy stage.
- Promotion thresholds (tuned for real-world daily usage, not per-second test loads): `explore → canary` at 5 attempts / 90% success / 98% verify; `canary → stable` at 15 attempts / 95% success / 99% verify. `bridge_broken` auto-demotion (2 consecutive failures) is the safety net that makes aggressive promotion safe.
- Verify rate defaults to `1.0` when `verify_attempts == 0` — lets span-only evidence (no verify signal) promote on success alone. Don't "fix" this to `0.0`.
- `synthesis_ready` has two intentional meanings: PolicyEngine flag at `explore → canary` for exec-path auto-crystallize; `SpanTracker.is_synthesis_ready()` at `stable` for span-path skeleton generation. Exec WAL carries code and can crystallize earlier than span WAL.
- `icc_reconcile(outcome=correct)` bumps `human_fix_rate` only on the latest-`last_ts_ms` candidate for the intent. Never fan out to all matches.
- `frozen: true` skips automatic transitions (stats still accrue). Toggle via `/api/control-plane/policy/freeze`.
- `transition_history` + `last_transition_reason` + `last_demotion` are written on every stage change. `rollback → explore` does NOT touch `last_demotion` (recovery, not regression).
- **Registry protection**: `intents.json` carries `schema_version` (currently 1). `IntentRegistry.save` creates a `.bak` backup before every atomic write. Load adds missing version stamps automatically. This prevents learning loss from accidental resets — the previous version is always recoverable from `.bak`.
- **Evidence idempotency**: retryable self-report evidence with `evidence_unit_id` counts once per intent. Runner outbox replay, span-close retries, or duplicate event delivery must not inflate `attempts`, `successes`, failure counters, verify counters, or recent outcomes. `operator_action` evidence remains deliberately countable because it represents a fresh human confirmation/retraction.
- **Reflection filters**: `SpanTracker.format_reflection` excludes `emerge.*` prefixed intents from stable/canary/recent sections. Emerge-internal development spans are non-repeatable and would pollute operator-Claude's muscle memory with noise.
- **Bridge-broken auto-demote**: `_try_flywheel_bridge` feeds every bridge outcome into `PolicyEngine.record_bridge_outcome(success=...)`. `BRIDGE_BROKEN_THRESHOLD` (default 2) consecutive bridge failures on a `stable` intent force `stable → canary`. Two distinct demotion reasons are emitted so reflection can distinguish root causes: `"bridge_broken"` (exception raised, `verification_state == "degraded"`, or `action_result.ok is False`) and `"bridge_silent_empty"` (read returned empty after the intent's `has_ever_returned_non_empty` baseline was True — upstream drift or schema rename). Success with non-empty output latches the baseline flag. Success resets `bridge_failure_streak`.
- **Auto-activate at stable**: `icc_span_close` at stable auto-activates the generated skeleton (writes `.py`/`.yaml` into the real connector dir, not `_pending/`) unless `EMERGE_REQUIRE_APPROVE=1`. Response sets `auto_activated: true` + `pipeline_path`. `icc_span_approve` remains the manual opt-out path. Default is auto-promote because `_pending/` gated on human approval stalls the flywheel.
- **Crystallized pipeline return values**: generated `run_read`/`run_write` use `locals().get('__result', [])` / `locals().get('__action', {"ok": True})` — exec code normally sets them, but the fallback keeps auto-activated pipelines from crashing when WAL code never assigned the return var. Crystallizer refuses up-front if WAL never assigns `__result`/`__action`, writes `synthesis_skipped_reason` on the intent, and reflection surfaces it under **Synthesis blocked** so the next session knows to fix the code.
- **Composite intents**: `icc_compose(parent, children=[...])` registers a composite via `PolicyEngine.register_composite` — structure + inherited `stage = min(children.stage)` flow through the single writer. `_handle_icc_compose` runs DFS cycle detection before routing through PolicyEngine (a child that transitively reaches the parent is rejected). Execution reuses `_try_flywheel_bridge` → `_run_composite_bridge`; any child failure records `bridge_broken` on the composite and falls back to LLM.

**Execution kernel**
- `icc_exec` requires `intent_signature` (`connector.mode.name`, lowercase). `PreToolUse` blocks if missing, targets 2-part sigs with format guidance, normalizes uppercase via `updatedInput`.
- Per-call limits from `policy_config.exec_limits()`: `EMERGE_EXEC_TIMEOUT_S` (600 s) → poisons session on timeout; `EMERGE_EXEC_STDOUT_BYTES` / `EMERGE_EXEC_STDERR_BYTES` (256/64 KiB) → return `truncation` + WAL `*_truncated_bytes`, never fail. `session_meta` persists in `checkpoint.json`. Runner HTTP timeout is separate.
- Idle eviction at `EMERGE_SESSION_IDLE_TTL_S` (3600 s, `0` disables) drops in-memory sessions; WAL + checkpoint survive → next call rehydrates. Poisoned sessions are **not** evictable.
- WAL entries with `no_replay=True` are excluded from **both** replay and crystallization. State-setup entries must never set `no_replay`.
- `from __future__ import annotations` is stripped before `exec()` injection (it raises `SyntaxError` mid-string).

**Hooks, MCP, notifications** (detail in each hook file; only cross-cutting rules here)
- CC's `hookSpecificOutput` allowlist is weirdly narrow. Events outside it (`Stop`, `SubagentStop`, `SessionEnd`, `PreCompact`, `InstructionsLoaded`, `WorktreeCreate/Remove`, `TaskCreated`, `StopFailure`) **must** use top-level `systemMessage` or return `{}`. When adding or editing a hook, verify against `hooks/hooks.json` + CC docs before assuming shape.
- `notifications/claude/channel` is silently dropped for plugin MCP servers. For any new "tell the model X" path, use Monitor tool (`watch_emerge.py`) + `UserPromptSubmit`/`additionalContext`, not channel notifications.
- Resource change push: `PolicyEngine._notify_resources_changed()` fires `notifications/resources/list_changed` after every `IntentRegistry.save`. Don't poll `policy://current`.
- **Silence principle** for operator popups: interrupt only when operator input changes the outcome (ambiguous intent, irreversible + high-risk). Never popup for started/running/completed, read-only, status, or self-recoverable errors.
- Shared hook `state.json` read-modify-write must go through `state_tracker.with_locked_tracker()` or an equivalent file lock. Atomic replace alone prevents torn writes, not lost updates across hook subprocesses and daemon threads.
- Every HTTP POST body read must clamp `Content-Length` before `rfile.read(...)`. Use `scripts/http_limits.py`; route-level validation after reading is too late for long-running daemon/runner stability.

**Events / Memory Hub**
- `EventRouter.start()` drains existing watched files synchronously before activating watchdog. No events lost across daemon restart.
- `sync-queue.jsonl` accepts exactly two event types: `stable` and `pull_requested`. Anything else accumulates unbounded.
- Memory Hub **never** syncs `intents.json`, `state.json`, operator-events, or credentials. Only pipelines, `NOTES.md`, and stripped-stable `spans.json`. Adding a new "share this across machines" feature must first prove it's not a credential leak.

## Freedom boundaries — what you're authorized to do without asking

**Ship freely:**
- New `icc_*` tools / resources / cockpit endpoints — extending the surface is capability work.
- New demotion reasons, new reflection fields, new span bridge optimizations — if it lets operator-Claude avoid a mistake or skip inference.
- Hook additions, new event types in dedicated streams — as long as the notification path isn't the dropped channel.
- Ugly code that earns its ugliness by expanding capability is fine — **but size has a cost**. Files >1000 lines combining >3 distinct concerns (dispatch + execution + telemetry + state) hide silent bugs under their mass. Split along existing seams (e.g. `_handle_*` families, a dispatch registry, an HTTP shim) when concerns are clearly separable; never split for aesthetics alone. Extraction counts as capability work when it lets the next Claude find the right code fast enough to fix a regression.

**Do not, without explicit user request:**
- Add a manual approval step, confirmation prompt, or `_pending/` gate to any auto-promoted path.
- Write `stage` or lifecycle counters outside `PolicyEngine.apply_evidence`.
- Write `intents.json` outside `IntentRegistry`. Write any policy state non-atomically.
- Reintroduce `status` / `policy_status` field names for policy stage. Reintroduce `pipelines-registry.json` / `span-candidates.json`. Reintroduce `icc_read` / `icc_write`.
- Sync anything containing credentials or per-session state through Memory Hub.
- Rename fields across multiple files "for consistency". That is not capability work; it's churn that obsoletes every prior reflection and crystallized pipeline.

## Test infra

- `conftest.py` autouse fixtures: `_mock_connector_root` (sets `EMERGE_CONNECTOR_ROOT` → `tests/connectors/`), `isolate_runner_config` (clears runner env).
- Primary integration surface: `tests/test_mcp_tools_integration.py` — calls `EmergeDaemon.call_tool(...)` directly, not through JSON-RPC.
- Tests needing a real runner start `_RunnerServer` in-process (see `test_remote_runner.py`).

## Keeping docs honest

`README.md` is canonical for architecture diagrams and user-facing surface. When you change:

| Change | Must also update |
|---|---|
| MCP tool, resource URI, or parameter | `emerge_daemon.py` schema + `README.md` MCP table |
| Policy threshold / lifecycle rule | `README.md` flywheel diagram + Glossary + this file's Invariants |
| Hook behavior, matcher, or `hooks.json` entry | owning hook file docstring + `README.md` component table + this file's Invariants |
| Env var | `README.md` §Remote runner config table |
| Runner protocol / endpoint | `README.md` §Remote runner + `skills/remote-runner-dev/SKILL.md` |
| Memory Hub flow / `icc_hub` action / queue event | `README.md` + this file + `commands/hub.md` + `skills/` if flywheel-facing |
| Cockpit API | `scripts/admin/cockpit.py` handler + `scripts/admin/cockpit/src/` consumer + tests |
| Architecture / data flow | `README.md` diagram (canonical) + this file's Architecture wording |
| New/deleted skill | `README.md` What-ships table + `skills/` |

If a row here would feel wrong *after* your change (e.g. you renamed a tool but the README MCP table still shows the old name), you have broken the contract with operator-Claude — its reflection and cockpit data will point at things that no longer exist. Fix before merge.
