# Emerge

Emerge is a minimal Claude Code plugin kernel focused on:

- `read` / `write` / `bash` execution primitives via MCP tools
- A-track pipeline execution (`icc_read` / `icc_write`)
- persistent REPL execution (`icc_exec`)
- state-delta context compression (`Goal` / `Delta` / `Open Risks`)

## Implemented foundation

This repository now includes a runnable plugin foundation:

- `.claude-plugin/plugin.json` - plugin metadata (`name: emerge`)
- `.mcp.json` - stdio MCP server wiring (`scripts/repl_daemon.py`)
- `hooks/hooks.json` - `SessionStart`, `UserPromptSubmit`, `PostToolUse`, `PreCompact`
- `scripts/` - daemon, pipeline engine, REPL state, state tracker
- `connectors/mock/pipelines/` - mock read/write A-track pipelines
- `tests/` - unit + integration coverage for all core paths

## Quick verification

Run all implemented checks:

```bash
pytest tests -q
```

Expected result (current baseline): `13 passed`.

## Repository layout

- `docs/superpowers/specs/` - product and architecture specifications
- `docs/superpowers/plans/` - implementation plans
- `scripts/` - MCP daemon/runtime core
- `hooks/` - Claude Code hook scripts
- `connectors/` - A-track pipeline definitions and actions
- `tests/` - verification suite
- `references/` - external reference codebases (git submodules)

## Reference sources

Claude Code source is kept under `references/` as reference material, so Emerge core implementation remains isolated and easier to evolve.