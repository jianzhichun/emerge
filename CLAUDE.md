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

# Runner bootstrap (remote setup, first time only)
python3 scripts/repl_admin.py runner-bootstrap --ssh-target "user@host" --target-profile "key" --runner-url "http://host:8787"

# Runner deploy (push updated scripts + hot-reload watchdog — use after any scripts/ change)
python3 scripts/repl_admin.py runner-deploy --target-profile mycader-1

# Runner status
python3 scripts/repl_admin.py runner-status --pretty
```

## Architecture

**Single control plane**: `EmergeDaemon` (`scripts/emerge_daemon.py`) is the only MCP server. It handles all goal + flywheel tools (`icc_span_open`, `icc_span_close`, `icc_span_approve`, `icc_exec`, `icc_crystallize`, `icc_reconcile`, `icc_goal_ingest`, `icc_goal_read`, `icc_goal_rollback`) and all resources (`policy://`, `runner://`, `state://deltas`, `state://goal`, `state://goal-ledger`, `pipeline://`, `connector://`). There is no second server.

**Two execution paths for pipelines**: `PipelineEngine` is called in-process by the span bridge (`icc_span_open` when stable) and directly by `icc_read`/`icc_write` (internal, schema-hidden). When `RunnerRouter` resolves a client, the daemon builds a self-contained inline `exec()` payload and POSTs to the remote runner. The runner never receives pipeline files — switching machines is a URL change only.

**Auto-crystallize**: `icc_exec` synthesis_ready triggers daemon to auto-extract WAL code and write `.py`+`.yaml` pipeline (intent_signature encodes connector/mode/name). Skipped if file exists; `icc_crystallize` manual call can force-overwrite.

**Span path**: `icc_span_open` → [any MCP tool calls, PostToolUse records] → `icc_span_close` → span-wal + span-candidates update policy. At stable, auto-generates Python skeleton to `_pending/`. `icc_span_approve` moves skeleton to real dir and generates YAML, activating the bridge.

**Span bridge**: `icc_span_open` detects stable + pipeline exists → PipelineEngine executes directly and returns result, zero LLM inference. `_record_pipeline_event` called, pipeline quality enters pipelines-registry normal tracking.

**Single span constraint**: at most one active span at any time. SessionStart hook clears stale `active_span_id`. `icc_exec` calls are excluded from span action recording.

**Deprecated**: `icc_read`, `icc_write` removed from schema. Replaced by `icc_span_open` bridge path. Still callable internally for backward compatibility.

**`connector://` resource**: `connector://<name>/notes` reads `~/.emerge/connectors/<name>/NOTES.md` — operational notes, COM patterns, API quirks, and known issues for a vertical. `connector://<name>/spans` — JSON index of span intent policy states for that connector. Listed automatically when data is present.

**Goal Control Plane**: active goal no longer lives in `state.json`. Writers submit append-only events to `goal-ledger.jsonl`; decision output is persisted in `goal-snapshot.json` (versioned, auditable). Hooks and policy-status read the snapshot.

**Policy never leaves the daemon**: all lifecycle state (`candidates.json`, `pipelines-registry.json`), WAL, and metrics are written locally regardless of whether execution is local or remote.

**Flywheel bridge**: inside `icc_exec`, when a candidate matching `intent_signature`+`script_ref` is `stable`, execution short-circuits to the pipeline result without LLM inference. Bridge key: `flywheel::<pipeline_id>::<intent_signature>::<script_ref>`.

**`from __future__` stripping**: pipeline `.py` files may contain `from __future__ import annotations`. This line is stripped before injection into `exec()` payloads (it raises `SyntaxError` when not first in a string).

**Human-fix targeting**: `icc_reconcile` with `outcome=correct` increments `human_fix_rate` only on the candidate with the highest `last_ts_ms` matching the `intent_signature` — the most-recently-used one. Never all matching candidates.

**Delta enrichment**: Each delta in `StateTracker` carries `intent_signature`, `tool_name`, and `ts_ms` alongside the original `id`, `message`, `level`, `verification_state`, `provisional` fields. `_normalize_state` fills missing fields with `None`/`None`/`0` for backward compatibility.

**Risk object model**: `open_risks` are now dicts with `risk_id`, `text`, `status` (open/handled/snoozed), `created_at_ms`, `snoozed_until_ms`, `handled_reason`, `source_delta_id`, `intent_signature`. `_normalize_state` migrates bare string risks to objects. `update_risk(risk_id, action)` handles lifecycle transitions.

**Frozen flag**: Both `pipelines-registry.json` entries and `span-candidates.json` entries support a `frozen: bool` field. When frozen, `_update_pipeline_registry` skips all auto-transitions (stats still update) and `get_policy_status` returns `"explore"`. Set/unset via cockpit `/api/control-plane/policy/freeze` and `/unfreeze`.

**Memory Hub**: `emerge_sync.py` is a standalone sync agent that shares connector assets (pipelines, NOTES.md, spans.json) via a self-hosted git repo's orphan branch (`emerge-hub`). The daemon writes a `stable` event to `~/.emerge/sync-queue.jsonl` when a pipeline is promoted to stable; emerge_sync polls the queue and triggers a push flow. A background timer drives periodic pull. Conflicts are written to `~/.emerge/pending-conflicts.json` and resolved via `icc_hub(action="resolve", ...)`. Hub config lives in `~/.emerge/hub-config.json`. Never synced: credentials, operator-events, `pipelines-registry.json`.

**Cockpit control plane**: `repl_admin.py` exposes `/api/control-plane/*` read endpoints (state, intents, session, exec-events, pipeline-events, spans, span-candidates) and write endpoints (delta/reconcile, risk/update, risk/add, policy/freeze, policy/unfreeze, session/export, session/reset). The cockpit HTML has an Overview intent table, connector sub-panels (Deltas/Risks/Spans/Exec Events), and global Audit/Session/Operator tabs.

## Test Infrastructure

`conftest.py` sets two `autouse` fixtures:
- `_mock_connector_root`: sets `EMERGE_CONNECTOR_ROOT` to `tests/connectors/` so `PipelineEngine` finds the mock connector
- `isolate_runner_config`: clears runner env vars so tests never hit a real remote runner

Tests that need a real runner (`test_remote_runner.py`) start their own in-process server via `_RunnerServer`.

Integration tests go in `test_mcp_tools_integration.py` and call `EmergeDaemon.call_tool(...)` directly — not through JSON-RPC. This is the primary integration surface.

## Key Invariants

- `icc_exec` **requires** `intent_signature` — enforced by `PreToolUse` hook which blocks the call with convention guidance if missing.
- Goal ownership invariant: active goal comes from `goal-snapshot.json`; `state.json` tracks deltas/risks only.
- Connector pipelines live in `~/.emerge/connectors/<connector>/pipelines/{read,write}/` (user-space). `tests/connectors/` is test fixture only, not shipped.
- Pipeline metadata files (`*.yaml`) are strict YAML only. JSON-style object/array payloads in `.yaml` are invalid.
- Policy state files use atomic writes (temp file + rename). Never write directly.
- WAL entries with `no_replay=True` are excluded from both replay and crystallization. State setup entries must not use `no_replay`.
- `EMERGE_OPERATOR_MONITOR=1` enables `OperatorMonitor` thread in the daemon. Off by default. Polls remote runners via `GET /operator-events`, runs `PatternDetector`, pushes to CC via MCP channel notification (explore) or `ElicitRequest` (canary/stable).
- `ObserverPlugin` (`scripts/observer_plugin.py`) is the ABC for all operator observation. `AdapterRegistry` loads built-in observers (`scripts/observers/`) and vertical adapters from `~/.emerge/adapters/<vertical>/adapter.py`. Vertical adapters are user-authored Python files that subclass `ObserverPlugin` (not shipped, authored per-user).
- `EventBus`: `~/.emerge/operator-events/<machine_id>/events.jsonl` — append-only. Written via `POST /operator-event` on the remote runner. `session_role=monitor_sub` events are filtered by `PatternDetector` to prevent AI self-monitoring.
- **Silence principle (operator interruption):** Show a popup (`show_notify`) only when the operator's input genuinely changes the outcome — intent is unclear, or the action is irreversible and high-risk. Never show a popup for: execution started/in-progress/completed, read-only operations (`icc_read`, state queries), status updates, or errors CC can resolve autonomously. Default is silence; interrupt only when necessary.

## Documentation Update Rules

When making code changes, keep these in sync:

| Change | Update |
|---|---|
| New/renamed MCP tool or parameter | `emerge_daemon.py` tool schema + `README.md` MCP surface table |
| New MCP resource URI | `emerge_daemon.py` `_list_resources`/`_read_resource` + `README.md` Resources line + `CLAUDE.md` Architecture section |
| New env var | `README.md` configuration table in §"Remote runner — operations" |
| Policy lifecycle threshold change | `README.md` flywheel diagram + Glossary |
| Hook behavior change | `README.md` component table (Hooks row) + hook flow diagram |
| New/deleted skill | `README.md` What ships table + `skills/` directory |
| Runner protocol change | `README.md` §"Remote runner — operations" + `skills/remote-runner-dev/SKILL.md` |
| Architecture change | `README.md` architecture diagram + component table |
| Test count change | `README.md` badge + Quick verification baseline |
| New observer or adapter interface change | `skills/writing-vertical-adapter/SKILL.md` |
| OperatorMonitor env var change | README.md env var table + `skills/operator-monitor-debug/SKILL.md` |
| Memory Hub config or sync flow change | `README.md` component table + `CLAUDE.md` Architecture section |
