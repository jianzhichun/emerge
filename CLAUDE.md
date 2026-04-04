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

# Runner bootstrap (remote setup)
python3 scripts/repl_admin.py runner-bootstrap --ssh-target "user@host" --target-profile "key" --runner-url "http://host:8787"

# Runner status
python3 scripts/repl_admin.py runner-status --pretty
```

## Architecture

**Single control plane**: `EmergeDaemon` (`scripts/emerge_daemon.py`) is the only MCP server. It handles all five tools (`icc_exec`, `icc_read`, `icc_write`, `icc_crystallize`, `icc_reconcile`) and all four resources (`policy://`, `runner://`, `state://`, `pipeline://`). There is no second server.

**Two execution paths for pipelines**: `icc_read`/`icc_write` run locally by default (daemon calls `PipelineEngine` in-process). When `RunnerRouter` resolves a client for the request, the daemon loads pipeline `.py`+`.yaml` locally, builds a self-contained inline `exec()` payload, and POSTs it as `icc_exec` to the remote runner. The runner never receives pipeline files — switching machines is a URL change only.

**Policy never leaves the daemon**: all lifecycle state (`candidates.json`, `pipelines-registry.json`), WAL, and metrics are written locally regardless of whether execution is local or remote.

**Flywheel bridge**: inside `icc_exec`, when a candidate matching `intent_signature`+`script_ref` is `stable`, execution short-circuits to the pipeline result without LLM inference. Bridge key: `flywheel::<pipeline_id>::<intent_signature>::<script_ref>`.

**`from __future__` stripping**: pipeline `.py` files may contain `from __future__ import annotations`. This line is stripped before injection into `exec()` payloads (it raises `SyntaxError` when not first in a string).

**Human-fix targeting**: `icc_reconcile` with `outcome=correct` increments `human_fix_rate` only on the candidate with the highest `last_ts_ms` matching the `intent_signature` — the most-recently-used one. Never all matching candidates.

## Test Infrastructure

`conftest.py` sets two `autouse` fixtures:
- `_mock_connector_root`: sets `EMERGE_CONNECTOR_ROOT` to `tests/connectors/` so `PipelineEngine` finds the mock connector
- `isolate_runner_config`: clears runner env vars so tests never hit a real remote runner

Tests that need a real runner (`test_remote_runner.py`) start their own in-process server via `_RunnerServer`.

Integration tests go in `test_mcp_tools_integration.py` and call `EmergeDaemon.call_tool(...)` directly — not through JSON-RPC. This is the primary integration surface.

## Key Invariants

- `icc_exec` **requires** `intent_signature` — enforced by `PreToolUse` hook which blocks the call with convention guidance if missing.
- Connector pipelines live in `~/.emerge/connectors/<connector>/pipelines/{read,write}/` (user-space). `tests/connectors/` is test fixture only, not shipped.
- Policy state files use atomic writes (temp file + rename). Never write directly.
- WAL entries with `no_replay=True` are excluded from both replay and crystallization. State setup entries must not use `no_replay`.

## Documentation Update Rules

When making code changes, keep these in sync:

| Change | Update |
|---|---|
| New/renamed MCP tool or parameter | `emerge_daemon.py` tool schema + `README.md` MCP surface table |
| New env var | `README.md` configuration table in §"Remote runner — operations" |
| Policy lifecycle threshold change | `README.md` flywheel diagram + Glossary |
| Hook behavior change | `README.md` component table (Hooks row) + hook flow diagram |
| New/deleted skill | `README.md` What ships table + `skills/` directory |
| Runner protocol change | `README.md` §"Remote runner — operations" + `skills/remote-runner-dev/SKILL.md` |
| Architecture change | `README.md` architecture diagram + component table |
| Test count change | `README.md` badge + Quick verification baseline |
