# CLAUDE.md

## Commands

```bash
# Run full test suite
python -m pytest tests -q

# Run a single test file
python -m pytest tests/test_mcp_tools_integration.py -q

# Run a single test by name
python -m pytest tests/test_mcp_tools_integration.py::test_increment_human_fix_targets_most_recent_candidate_only -q

# Run the daemon manually (dev)
python3 scripts/emerge_daemon.py

# Runner install URL (operator runs on remote machine; first-time setup)
python3 scripts/repl_admin.py runner-install-url --target-profile "key" --pretty

# Runner deploy (push updated scripts + hot-reload watchdog — use after any scripts/ change)
python3 scripts/repl_admin.py runner-deploy --target-profile mycader-1

# Runner status
python3 scripts/repl_admin.py runner-status --pretty

# Memory Hub — first-time setup (interactive wizard, run once per machine)
python3 scripts/emerge_sync.py setup

# Memory Hub — start sync agent (background poll loop, restart after any scripts/ change)
python3 scripts/emerge_sync.py run

# Memory Hub — manual sync for all selected connectors (or one specific connector)
python3 scripts/emerge_sync.py sync
python3 scripts/emerge_sync.py sync gmail
```

## Architecture

**Documentation source of truth**: `README.md` is the canonical source for architecture and data-flow diagrams. `CLAUDE.md` focuses on implementation constraints and invariants; when behavior changes, update both, but keep diagram semantics centralized in `README.md` to avoid drift.

**Single control plane**: `EmergeDaemon` (`scripts/emerge_daemon.py`) is the only MCP server (HTTP, port 8789, via `scripts/daemon_http.py`). It handles all flywheel tools (`icc_span_open`, `icc_span_close`, `icc_span_approve`, `icc_exec`, `icc_crystallize`, `icc_reconcile`, `icc_hub`) and all resources (`policy://`, `runner://`, `state://deltas`, `pipeline://`, `connector://`). There is no second server.

**Single execution path for pipelines**: `PipelineEngine` is called in-process by the span bridge (`icc_span_open` when stable). When `RunnerRouter` resolves a client, the daemon builds a self-contained inline `exec()` payload and POSTs to the remote runner. The runner never receives pipeline files — switching machines is a URL change only.

**Auto-crystallize**: `icc_exec` synthesis_ready triggers daemon to auto-extract WAL code and write `.py`+`.yaml` pipeline (intent_signature encodes connector/mode/name). Skipped if file exists; `icc_crystallize` manual call can force-overwrite. Logic lives in `scripts/crystallizer.py` (`PipelineCrystallizer`); daemon delegates via thin `_crystallize`/`_auto_crystallize`/`_generate_span_skeleton` wrappers.

**Tool dispatch**: `EmergeDaemon.call_tool` uses a `_TOOL_DISPATCH` dict → `_handle_<tool>` methods. Each tool handler is an independent method — no if/elif chain. Adding a new tool: add entry to `_TOOL_DISPATCH` + implement `_handle_<tool>`.

**Shared utilities in `policy_config.py`**: `resolve_connector_root()` (EMERGE_CONNECTOR_ROOT env or `~/.emerge/connectors`), `load_json_object(path, root_key)` (safe JSON load with empty-dict fallback), `USER_CONNECTOR_ROOT` constant, `REFLECTION_CACHE_TTL_MS` constant. All files should import these instead of inlining the patterns.

**Span path**: `icc_span_open` → [any MCP tool calls, PostToolUse records] → `icc_span_close` → span-wal + span-candidates update policy. At stable, auto-generates Python skeleton to `_pending/`. `icc_span_approve` moves skeleton to real dir and generates YAML, activating the bridge.

**Span bridge**: `icc_span_open` detects stable + pipeline exists → PipelineEngine executes directly and returns result, zero LLM inference. `_record_pipeline_event` called, pipeline quality enters pipelines-registry normal tracking.

**Single span constraint**: at most one active span at any time. SessionStart hook clears stale `active_span_id`. `icc_exec` calls are excluded from span action recording.

**Span Protocol injection**: SessionStart and PreCompact inject a static span protocol directive (~150 chars) instructing the model to wrap reusable multi-step tool sequences in `icc_span_open`/`icc_span_close`. UserPromptSubmit does NOT inject the directive — only the FLYWHEEL_TOKEN carries `active_span_id`/`active_span_intent` (or null) per turn.

**Daemon intent gate**: `icc_span_open` returns `{status: "confirm_needed"}` (not error) when `intent_signature` is new and the connector already has existing span intents in `span-candidates.json`. The model must re-call with the same intent to confirm. Gate tracked via `self._intent_gate: set[str]` (persisted to `state_root/intent-gate.json` via atomic write; survives daemon restarts). Fires at most once per new intent per daemon lifecycle. Existing intents capped at 5 in the response. Does not fire for the first intent of a connector or for intents already in span-candidates.

**Span reflection injection**: `SpanTracker.format_reflection()` composes a compact "Muscle memory" summary from `span-candidates.json` (stable/canary policy) and recent `span-wal/spans.jsonl` outcomes. Hooks call `format_reflection_with_cache()` first: fresh deep cache (`reflection-cache/global.json`, TTL 15m) is preferred, otherwise fallback to lightweight reflection. `PreCompact`, `PostCompact`, and `UserPromptSubmit` (turn 1) inject reflection. Reflection output is capped (stable<=8, canary<=3, recent<=5) so token cost stays bounded.

**`icc_read`/`icc_write` deleted**: fully removed — no dispatch, no schema, no internal compat path. Use `icc_span_open` bridge for pipeline execution, `icc_exec` for exploration.

**`connector://` resource**: `connector://<name>/notes` reads `~/.emerge/connectors/<name>/NOTES.md` — operational notes, COM patterns, API quirks, and known issues for a vertical. `connector://<name>/spans` — JSON index of span intent policy states for that connector. Listed automatically when data is present.

**Policy never leaves the daemon**: all lifecycle state (`candidates.json`, `pipelines-registry.json`), WAL, and metrics are written locally regardless of whether execution is local or remote.

**Flywheel bridge**: inside `icc_exec`, when a candidate matching `intent_signature`+`script_ref` is `stable`, execution short-circuits to the pipeline result without LLM inference. Bridge key: `flywheel::<pipeline_id>::<intent_signature>::<script_ref>`.

**`from __future__` stripping**: pipeline `.py` files may contain `from __future__ import annotations`. This line is stripped before injection into `exec()` payloads (it raises `SyntaxError` when not first in a string).

**Human-fix targeting**: `icc_reconcile` with `outcome=correct` increments `human_fix_rate` only on the candidate with the highest `last_ts_ms` matching the `intent_signature` — the most-recently-used one. Never all matching candidates.

**Delta enrichment**: Each delta in `StateTracker` carries `intent_signature`, `tool_name`, and `ts_ms` alongside the original `id`, `message`, `level`, `verification_state`, `provisional` fields. `_normalize_state` fills missing fields with `None`/`None`/`0` for backward compatibility.

**Risk object model**: `open_risks` are now dicts with `risk_id`, `text`, `status` (open/handled/snoozed), `created_at_ms`, `snoozed_until_ms`, `handled_reason`, `source_delta_id`, `intent_signature`. `_normalize_state` migrates bare string risks to objects. `update_risk(risk_id, action)` handles lifecycle transitions.

**Frozen flag**: Both `pipelines-registry.json` entries and `span-candidates.json` entries support a `frozen: bool` field. When frozen, `_update_pipeline_registry` skips all auto-transitions (stats still update) and `get_policy_status` returns `"explore"`. Set/unset via cockpit `/api/control-plane/policy/freeze` and `/unfreeze`.

**Memory Hub**: `emerge_sync.py` is a standalone sync agent that shares connector assets (pipelines, NOTES.md, spans.json) via a self-hosted git repo's orphan branch (`emerge-hub`). The daemon writes a `stable` event to `~/.emerge/sync-queue.jsonl` when a pipeline is promoted to stable; emerge_sync polls the queue and triggers a push flow. A background timer drives periodic pull. Conflicts are written to `~/.emerge/pending-conflicts.json` and resolved via `icc_hub(action="resolve", ...)`. Hub config lives in `~/.emerge/hub-config.json`. Never synced: credentials, operator-events, `pipelines-registry.json`.

**EventRouter**: File system watcher that monitors `pending-actions.json` (created by cockpit when a pending action exists) and local operator event files. Triggers async handlers on file changes. Enforces drain-on-start contract: all existing watched files are processed synchronously before watchdog activation.

**Cockpit→CC action dispatch (three-tier)**: When the cockpit submits actions, `pending-actions.json` is written. Three delivery paths exist:
1. **Monitor tool (primary, real-time)**: `scripts/watch_emerge.py` runs via CC's Monitor tool (`persistent: true`), tails `events.jsonl` for `cockpit_action` events and prints formatted actions to stdout → CC conversation. Launched by `/emerge:cockpit` command (step 3).
2. **EventRouter rename**: daemon `_on_pending_actions()` renames to `pending-actions.processed.json` as a handoff for the fallback path. If the Monitor already consumed the file, this is a no-op.
3. **UserPromptSubmit hook (fallback)**: on the next user message, drains `.processed.json` (or `.json`) into `additionalContext` so the model sees and executes them, then renames to `pending-actions.delivered.json`.
This design is necessary because CC's `notifications/claude/channel` is silently dropped for plugin MCP servers (requires KAIROS channel gate not available to plugins).

**Cockpit control plane**: With the HTTP daemon, the browser UI and `/api/*` control plane are served on the **same port as MCP** (default **8789**). Cockpit frontend source lives under `scripts/admin/cockpit/src/` and Vite build output under `scripts/admin/cockpit/dist/`: `GET /` serves `dist/index.html` and bundled `/assets/*`; `/api/control-plane/*`, `/api/status`, `/api/sse/status`, etc. match `repl_admin.py` + `scripts/admin/cockpit.py` route handlers. Cockpit supports explicit session routing via `/api/control-plane/sessions` and `session_id` query params on session-scoped control-plane endpoints (`policy`, `session`, `exec-events`, `tool-events`, `pipeline-events`, `session/export`, `session/reset`). `repl_admin.py serve` remains for standalone use (no daemon) via `CockpitHTTPServer` on its own port. In-process: `DaemonHTTPServer.cockpit_broadcast` pushes SSE to cockpit clients; `InProcessCockpitBridge` supplies the same handler surface as `CockpitHTTPServer`. Standalone: `get_monitor_data()` falls back to `runner-monitor-state.json`. `_StandaloneDaemonStub` is the sentinel daemon for CLI-only mode. CORS uses `resolve_cors_allow_origin` (same `Host` as `Origin` netloc, or loopback aliases with matching ports — no blanket `127.0.0.1` bypass that ignores `Host` port).

**Daemon HTTP persistence**: `emerge_daemon.py` runs as an HTTP MCP server (`scripts/daemon_http.py` `DaemonHTTPServer`, port 8789, PID file `~/.emerge/daemon.pid` with `host`/`port`/`version`/`code_fingerprint`). Bind address defaults to loopback; set **`EMERGE_DAEMON_BIND`** (e.g. `0.0.0.0`) or **`python3 scripts/emerge_daemon.py --bind 0.0.0.0`** so LAN hosts can fetch runner self-install URLs. CC sessions connect via `plugin.json` `url: "http://localhost:8789/mcp"`. `SessionStart` hook starts daemon via `--ensure-running`; this now detects stale runtime code via pid fingerprint and restarts instead of reusing an outdated process. All CC sessions (team lead + watcher subagents) share one daemon instance. **HTTP mode: `_elicit()` returns None directly** (CC HTTP transport has no persistent SSE push channel); `icc_span_approve` and `icc_hub resolve` use `PreToolUse` hook `permissionDecision: ask` for confirmation.

**Runner push architecture**: Runners connect to the daemon via `GET /runner/sse?runner_profile=<p>`, register via `POST /runner/online`, and push events via `POST /runner/event`. `DaemonHTTPServer._on_runner_event` maintains a per-runner sliding-window `deque` and runs `PatternDetector.ingest()` on each push; pattern alerts are written directly to `events-{profile}.jsonl` (type=`pattern_alert`). `DaemonHTTPServer._notify_cockpit_broadcast({"monitors_updated": True})` is called on pattern detection and runner connect/disconnect (tests may set `daemon._cockpit_server` to a mock) — no file IPC. Popup commands are sent via SSE; results return via `POST /runner/popup-result` with correlation ID.

**Unified event streams**: `~/.emerge/repl/events.jsonl` (global), `events-{profile}.jsonl` (per-runner), `events-local.jsonl` (local). `watch_emerge.py` is the unified watcher supporting all three modes.

**Monitors tab**: cockpit reads `GET /api/control-plane/monitors` which returns `runner-monitor-state.json` (written by daemon on runner connect/disconnect). SSE `monitors_updated` event triggers automatic refresh when the Monitors tab is active.

## Test Infrastructure

`conftest.py` sets two `autouse` fixtures:
- `_mock_connector_root`: sets `EMERGE_CONNECTOR_ROOT` to `tests/connectors/` so `PipelineEngine` finds the mock connector
- `isolate_runner_config`: clears runner env vars so tests never hit a real remote runner

Tests that need a real runner (`test_remote_runner.py`) start their own in-process server via `_RunnerServer`.

Integration tests go in `test_mcp_tools_integration.py` and call `EmergeDaemon.call_tool(...)` directly — not through JSON-RPC. This is the primary integration surface.

## Key Invariants

- `icc_exec` **requires** `intent_signature` — enforced by `PreToolUse` hook which blocks the call with convention guidance if missing.
- Connector pipelines live in `~/.emerge/connectors/<connector>/pipelines/{read,write}/` (user-space). `tests/connectors/` is test fixture only, not shipped.
- Pipeline metadata files (`*.yaml`) are strict YAML only. JSON-style object/array payloads in `.yaml` are invalid.
- Policy state files use atomic writes (temp file + rename). Never write directly.
- WAL entries with `no_replay=True` are excluded from both replay and crystallization. State setup entries must not use `no_replay`.
- `OperatorMonitor` auto-starts when a runner is configured (`_get_runner_router() is not None`) OR `EMERGE_OPERATOR_MONITOR=1`. Previously required the env var explicitly. `EventRouter` registers the local operator-events handler when `_operator_monitor is not None` (not env-var-based). `push_fn` parameter is removed. `process_local_file` writes `local_pattern_alert` events directly to `events-local.jsonl` in `state_root`. `state_root` is injected by `start_operator_monitor` from `self._state_root`.
- **Per-runner alert routing**: `DaemonHTTPServer._on_runner_event` writes `pattern_alert` to `events-{runner_profile}.jsonl` when `PatternDetector` fires. `OperatorMonitor.process_local_file` writes `local_pattern_alert` to `events-local.jsonl`. The old `_push_pattern` / `pattern-alerts-{profile}.json` file format is removed. `watch_emerge.py --runner-profile <name>` watches `events-{name}.jsonl`; agents-team watcher monitors its own file.
- **Agents-team mode**: `/emerge:monitor` command creates `TeamCreate("emerge-monitors")` and spawns one `{profile}-watcher` subagent per runner. Each watcher runs a persistent Monitor on `events-{profile}.jsonl` and applies the stage→action protocol (explore=silent, canary=notify+choice+timeout, stable=silent exec). New runners can be added dynamically via `Agent(team_name="emerge-monitors", ...)` without recreating the team. Shutdown: `SendMessage(to="all", {type:"shutdown_request"})` → `TeamDelete()`.
- **TeammateIdle hook** (`hooks/teammate_idle.py`): fires just before an agent teammate goes idle. For `team_name == "emerge-monitors"` + `teammate_name` ending in `-watcher`: exits code 2 with a feedback message telling the agent to restart its `watch_emerge` Monitor. Exit code 2 causes CC to feed the stderr back to the agent as feedback so it continues working. All other agents exit 0 and go idle normally. Output contract: raw stderr + exit 2 (NOT `hookSpecificOutput` — TeammateIdle is not in CC's allowed list).
- **PermissionDenied hook** (`hooks/permission_denied.py`): fires when CC's auto-mode classifier denies a tool call. For `tool_name` matching `mcp__plugin_.*emerge.*__icc_.*`: returns `{"retry": true}` so CC lets the model retry with explicit permission. Prevents silent flywheel failures when icc_* tools are denied in auto mode. All other tools return `{}` (no opinion).
- `ObserverPlugin` (`scripts/observer_plugin.py`) is the ABC for all operator observation. `AdapterRegistry` loads built-in observers (`scripts/observers/`) and vertical adapters from `~/.emerge/adapters/<vertical>/adapter.py`. Vertical adapters are user-authored Python files that subclass `ObserverPlugin` (not shipped, authored per-user). `ObserverPlugin` provides a concrete `emit_event(event: dict)` helper that writes to the local EventBus; adapters call it to record domain-specific events for PatternDetector. `ts_ms` and `machine_id` are injected automatically if absent; caller-provided values win.
- `EventBus`: `~/.emerge/operator-events/<machine_id>/events.jsonl` — append-only. Written via (a) `POST /operator-event` on the remote runner (human ops), (b) `_write_operator_event()` in the daemon after each `icc_exec` with `intent_signature` (CC takeovers, `session_role=monitor_sub`), (c) `ObserverPlugin.emit_event()` from vertical adapters. `session_role=monitor_sub` events are filtered by `PatternDetector` to prevent AI self-monitoring. `icc_exec` skips the write when `no_replay=True` or `intent_signature` is absent.
- **Silence principle (operator interruption):** Show a popup (`show_notify`) only when the operator's input genuinely changes the outcome — intent is unclear, or the action is irreversible and high-risk. Never show a popup for: execution started/in-progress/completed, read-only operations (state queries), status updates, or errors CC can resolve autonomously. Default is silence; interrupt only when necessary.
- **Memory Hub sync queue contract**: `sync-queue.jsonl` carries exactly two event types — `stable` (written by daemon on policy promotion, consumed by `_run_stable_events`) and `pull_requested` (written by `icc_hub sync`, consumed by `_run_stable_events`). Never write other event types to the queue; unconsumed events accumulate without bound.
- **Memory Hub conflict resolution states**: `pending` → user calls `icc_hub resolve` → `resolved` → emerge_sync applies it → `applied`. "ours" leaves the file at HEAD (no-op). "theirs" uses `git show origin/<branch>:<file>` to write the remote version. "skip" marks applied without any git op. Never re-attempt pull_flow for a connector that had a push conflict in the same cycle.
- **Memory Hub never syncs**: `pipelines-registry.json`, `span-candidates.json`, `state.json`, operator-events, credentials. Only pipeline `.py`/`.yaml` files, `NOTES.md`, and a stripped `spans.json` (stable entries only) are shared.
- **EventRouter drain-on-start contract**: `EventRouter.start()` synchronously calls handlers for all existing watched files before handing control to watchdog/polling. This ensures no events are lost between daemon restart and watchdog activation.
- **MCP protocol version**: daemon negotiates version — responds `min(client_version, "2025-11-25")`. Server max is `_SERVER_MAX_PROTOCOL_VERSION = "2025-11-25"`. Tools include `title`, `annotations`, and `outputSchema` per MCP 2025-11-25 spec. `_elicit()` must only be called from non-main threads (ThreadPoolExecutor workers or daemon threads), never from the main stdin loop.
- **Notification delivery**: `notifications/claude/channel` is silently dropped by CC for plugin MCP servers. All notification paths use working alternatives:
  - **Cockpit actions**: Monitor tool (`watch_emerge.py`, primary real-time — tails `events.jsonl` for `cockpit_action` events) + UserPromptSubmit hook fallback (see Cockpit→CC three-tier dispatch).
  - **Operator monitor patterns**: `DaemonHTTPServer._on_runner_event` writes `type=pattern_alert` directly to `events-{runner_profile}.jsonl`; `OperatorMonitor.process_local_file` writes `type=local_pattern_alert` to `events-local.jsonl`. `watch_emerge.py [--runner-profile <name>]` Monitor streams alerts to CC conversation.
  - **Bridge failure warnings**: `_try_flywheel_bridge` stores failure info on `self._last_bridge_failure`; `icc_exec` handler injects warning via `_append_warning_text` into the tool response.
  - **Span skeleton ready**: `icc_span_close` response includes `skeleton_path` + `next_step`; PostToolUse hook injects `[Span]` reminder into `additionalContext`.
- **SessionEnd hook** (`hooks/session_end.py`): clears stale `active_span_id` and `active_span_intent` from `state.json`. Registered in `hooks/hooks.json`. Complements `SessionStart` which also clears stale span state. Belt-and-suspenders cleanup for unresolvable open spans.
- **Stop/SubagentStop hooks** (`hooks/stop.py`): blocks CC stop when `active_span_id` is present in `state.json`, preventing incomplete flywheel WAL records. Block output: `{"decision": "block", "reason": "...call icc_span_close(outcome='aborted') first"}`. Registered in `hooks/hooks.json` for both `Stop` and `SubagentStop` events with 10-second timeout.
- **StopFailure hook** (`hooks/stop_failure.py`): fires when CC exits due to error (`rate_limit`, `billing_error`, `authentication_failed`, etc.). Clears `active_span_id`/`active_span_intent` from `state.json` so the next session starts clean. No decision control — cannot block the error. Output: top-level `systemMessage`.
- **TaskCompleted hook** (`hooks/task_completed.py`): fires when any task is marked completed (TaskUpdate or agent-team teammate finish). Checks for open span; if present, exits code 2 + stderr — blocks task completion and feeds message back to model as feedback. No hookSpecificOutput (not in allowed list). No matcher (per CC docs for TaskCompleted).
- **SubagentStart hook** (`hooks/subagent_start.py`): fires when a subagent is dispatched. If parent session has `active_span_id`, injects `systemMessage` guardrail: "do NOT call icc_span_close — parent session owns the span". Subagent PostToolUse hooks already record icc_* calls into the span WAL via the shared state.json.
- **PreToolUse format** (`hooks/pre_tool_use.py`): uses MCP 2025-11-25 `permissionDecision: deny` + `systemMessage` format for blocks (legacy `{"decision": "block"}` removed). Approval path continues to use `additionalContext`.
- **PostToolUse span injection** (`hooks/post_tool_use.py`): when `icc_exec` runs inside an active span, injects `_span_id` and `_span_intent` into `structuredContent` via `updatedMCPToolOutput`, allowing CC to correlate exec results with flywheel spans without a separate state read.
- **PostToolUse response parsing** (`hooks/post_tool_use.py`): reads MCP results from `tool_response` (not `tool_result`) so inner `verification_state` values (`verified`/`degraded`) propagate into state/risk tracking correctly.
- **PostToolUseFailure interrupt handling** (`hooks/post_tool_use_failure.py`): user interrupts (`is_interrupt=true`) do not call `mark_degraded`; only real tool failures degrade verification state and open a risk.
- **Resource subscriptions**: daemon advertises `resources.subscribe=True` (capability introduced in MCP 2025-03-26 and still used under 2025-11-25 negotiation). After every `_update_pipeline_registry` write, daemon emits `notifications/resources/list_changed` so CC can re-read `policy://current` without polling.
- **Context injection budgeting**: `format_context(budget_chars=N)` in `StateTracker` allocates at most 1/3 of `budget_chars` to the risk list, sorted by recency (newest first). Risks beyond the budget are collapsed to a count with a pointer to `state://deltas`. This prevents context inflation in high-risk-count sessions.
- **Recovery token span fields**: `FLYWHEEL_TOKEN` (emitted by `format_recovery_token`) includes `active_span_id` and `active_span_intent` (or `null`). These fields ensure span state survives context compaction via `PreCompact` systemMessage.
- **PreToolUse 2-part intent**: `pre_tool_use.py` provides a specific error message when `intent_signature` has exactly 2 parts, explaining the required `connector.mode.name` format and prompting the user to add the connector name.
- **PreToolUse `updatedInput` normalization**: when `intent_signature` contains uppercase letters, `pre_tool_use.py` normalizes to lowercase and returns `updatedInput: {"intent_signature": lowercased}` with `permissionDecision: allow` instead of blocking. Only applied when the normalized value would be valid. Tracked via `_sig_normalized_from`/`_sig_normalized_to` in `main()`.
- **plugin.json HTTP transport**: `mcpServers.emerge` uses `type: "http"`, `url: "http://localhost:8789/mcp"` (CC plugin schema now requires explicit `type` field in each mcpServer entry). In HTTP mode CC does not spawn the daemon process.
- **runner_notify MCP tool**: sends popup commands to a runner via daemon SSE. For `type=toast` (fire-and-forget), `request_popup` returns `{ok: True}` immediately without waiting and the runner does not post a popup-result. For all other types, blocks waiting for `/runner/popup-result` callback (correlation ID: `popup_id`). Requires HTTP daemon mode (`_http_server` is not None).
- **System tray → daemon event path**: `RunnerExecutor._start_tray()` launches a pystray icon on the runner machine (no-op if pystray/Pillow missing or `_team_lead_url` unset). Operator clicking "发送消息" opens `show_input_bubble`; on submit, `_post_operator_message` forwards an `operator_message` event (with `runner_profile`, `text`, `machine_id`, `ts_ms`) via `POST /runner/event`. The daemon's `_on_runner_event` writes the event to `events-{runner_profile}.jsonl` with `type` preserved as `"operator_message"` (not normalized to `"runner_event"`). `watch_emerge.py` formats it as `[Operator:<profile>] <text>` for the watcher agent. `PatternDetector` skips `operator_message` events.
- **hooks.json hook matchers**: `PreToolUse`, `PostToolUse`, `PostToolUseFailure` all use `mcp__plugin_.*emerge.*__icc_.*` to cover all current and future icc_ tools. `tool_audit.py` uses the inverse negative-lookahead. `SessionEnd`, `Stop`, `SubagentStop` are registered in `hooks/hooks.json` (matcher format). `plugin.json` only keeps `SessionStart → runner_sync.py` (runner sync runs separately from session_start.py). `StopFailure`, `TaskCompleted`, and `SubagentStart` are registered with `python3 ${CLAUDE_PLUGIN_ROOT}/hooks/stop_failure.py`, `task_completed.py`, and `subagent_start.py` respectively. `TeammateIdle` (matcher `.*`) registered with `teammate_idle.py`. `PermissionDenied` (matcher `mcp__plugin_.*emerge.*__icc_.*`) registered with `permission_denied.py`. `PermissionRequest` (matcher `mcp__plugin_.*emerge.*__icc_.*`) registered with `permission_request.py`. `InstructionsLoaded`, `WorktreeCreate`, `WorktreeRemove`, `TaskCreated` (all matcher `.*`) registered with `instructions_loaded.py`, `worktree_lifecycle.py`, `worktree_lifecycle.py`, `task_created.py` respectively.
- **Connector NOTES injection**: `session_start.py` no longer writes `.claude/rules/connector-*.md` stubs. It injects only a compact connector index into startup context. `pre_tool_use.py` injects `~/.emerge/connectors/<connector>/NOTES.md` on-demand (capped to 1200 chars) when the first `icc_exec`/`icc_span_open`/`icc_crystallize` call for that connector is approved, and records connector names in `state.json` `notes_injected` to avoid duplicate injection within the same session.
- **Cockpit status contract**: `/api/status` returns `{ok, pending, server_online, cc_active}`. `cc_active` is `true` when the daemon received a `POST /mcp` within the last 120 seconds. Submit availability depends only on `queue.length > 0 && !serverPending`. Frontend uses SSE (`/api/sse/status`) as primary status channel; `/api/status` is a 30s fallback poll. `_sse_broadcast()` pushes `{pending: true}` on successful submission.
- **Cockpit session reset span guard**: `cmd_control_plane_session_reset` checks `active_span_id` in state before resetting. If a span is open, returns `{ok: false, error: "active_span_open"}`. Mirrors the Stop hook safety contract.
- **`_normalize_state` span + notes preservation**: `_normalize_state` in `state_tracker.py` preserves `active_span_id`, `active_span_intent`, and normalized `notes_injected` from raw state. `SessionStart` and `SessionEnd` hooks explicitly pop active span fields for cleanup.
- **Hook output schema**: CC's hook validator accepts `hookSpecificOutput` for: `PreToolUse`, `UserPromptSubmit`, `PostToolUse`, `SessionStart`, `FileChanged`, `Setup`, `SubagentStart`, `PostToolUseFailure`, `Notification`, `PermissionRequest`, and `Elicitation`. All other events (`Stop`, `SubagentStop`, `SessionEnd`, `PreCompact`, `InstructionsLoaded`, `WorktreeCreate`, `WorktreeRemove`, `TaskCreated`) must use the **top-level `systemMessage`** field for any context they want to surface, or print `{}` when nothing needs to be reported. Using `hookSpecificOutput` on a non-allowed event triggers `Hook JSON output validation failed: Invalid input`. `PermissionRequest` uses `hookSpecificOutput.hookEventName="PermissionRequest"` + `decision.behavior="allow"|"deny"`. `Elicitation` uses `hookSpecificOutput.hookEventName="Elicitation"` + `action: "accept"|"decline"|"delegate"` + `content: {...}`. `ElicitationResult` is a read-only notification event — return `{}` only, `hookSpecificOutput` has no effect. The block path on `Stop` continues to use top-level `{"decision": "block", "reason": "..."}`. `TaskCompleted` uses exit code 2 + raw stderr text (not JSON). `StopFailure` and `SubagentStart` use top-level `systemMessage`.
- **Channel notification params shape**: `notifications/claude/channel` requires `params: {content, meta}` only — **never** include `serverName` (or any other extra field). Plugin MCP servers cannot use this channel (silently dropped); use Monitor tool + UserPromptSubmit hook path instead.
- **Cockpit pending-actions file lifecycle**: `pending-actions.json` (cockpit writes) → daemon EventRouter renames to `.processed.json` → `pending-actions.delivered.json` (`UserPromptSubmit` hook renames after injecting into `additionalContext`). Hook checks `.processed.json` first, then `.json` as fallback. Only one file in the chain should exist at a time.

## Documentation Update Rules

When making code changes, keep these in sync:

| Change | Update |
|---|---|
| New/renamed MCP tool or parameter | `emerge_daemon.py` tool schema + `README.md` MCP surface table |
| New MCP resource URI | `emerge_daemon.py` `_list_resources`/`_read_resource` + `README.md` Resources line + `CLAUDE.md` Architecture section |
| New env var | `README.md` configuration table in §"Remote runner — operations" |
| Policy lifecycle threshold change | `README.md` flywheel diagram + Glossary |
| Hook behavior change | `README.md` component table (Hooks row) + hook flow diagram |
| New hook matcher pattern or hooks.json entry | `CLAUDE.md` Key Invariants (hooks.json matchers line) |
| MCP server_max_version bump | `CLAUDE.md` Key Invariants (protocol version line) + `README.md` if protocol version is mentioned |
| New/deleted skill | `README.md` What ships table + `skills/` directory |
| Runner protocol change | `README.md` §"Remote runner — operations" + `skills/remote-runner-dev/SKILL.md` |
| Architecture change | `README.md` architecture diagram + component table |
| Data-flow or lifecycle diagram semantic change | `README.md` flow diagrams (canonical) + `CLAUDE.md` Architecture/Key Invariants wording |
| Test count change | `README.md` badge + Quick verification baseline |
| New observer or adapter interface change | `skills/writing-vertical-adapter/SKILL.md` |
| OperatorMonitor env var change | README.md env var table + `skills/operator-monitor-debug/SKILL.md` |
| Memory Hub config or sync flow change | `README.md` component table + `CLAUDE.md` Architecture section + `CLAUDE.md` Key Invariants |
| New `icc_hub` action or queue event type | `README.md` MCP Tools table + `CLAUDE.md` Key Invariants (queue contract) + `commands/hub.md` if setup flow is affected |
| Cockpit API contract change | `scripts/admin/cockpit.py` handler + `scripts/admin/cockpit/src/` consumer + `CLAUDE.md` Architecture section + tests |
| Admin submodule business logic change | `scripts/admin/{shared,api,control_plane,pipeline,cockpit,runner}.py` owning module + `scripts/repl_admin.py` if public CLI surface changes |
| Runner push architecture change | `README.md` runner endpoints + `CLAUDE.md` Architecture section |
| New cockpit tab | `scripts/admin/cockpit/src/App.svelte` tab wiring + `scripts/admin/cockpit/src/components/` implementation + `CLAUDE.md` Cockpit control plane bullet + `README.md` component table |
