# Documentation Consistency Checklist

Use this checklist before merge/release to keep `README.md`, `CLAUDE.md`, `commands/`, and `skills/` aligned with shipped behavior.

## 1) Version and Verification Baseline

- `README.md` version badge matches `.claude-plugin/plugin.json` version.
- `README.md` test badge and "Quick verification baseline" match latest full-suite result.
- Commands in `CLAUDE.md` "Commands" section are executable and still valid.

## 2) MCP Surface Consistency

- `README.md` MCP tools table matches current daemon tool schema in `scripts/emerge_daemon.py`.
- `README.md` resources line matches `_list_resources` and `_read_resource` behavior.
- If protocol behavior changed, `README.md` + `CLAUDE.md` both mention the same protocol version semantics.
- `CLAUDE.md` key invariants for hooks/tools reflect current implementation (especially `PreToolUse`/`PostToolUse` behavior).

## 3) Hook Lifecycle and Semantics

- `hooks/hooks.json` event registrations match README hook list.
- `SessionEnd` / `Stop` / `SubagentStop` registration location is documented consistently.
- `PreToolUse` decision format (`permissionDecision`) and any `updatedInput` behavior are documented consistently.
- `PostToolUseFailure` semantics (real failure vs interrupt) are reflected in docs and tests.

## 4) Architecture and Flow Diagrams

- `README.md` is the canonical source for architecture/data-flow diagrams.
- Diagram semantics match code reality:
  - stable `icc_exec` bridge path
  - stable `icc_span_open` bridge path
  - local vs remote execution branching
  - hook lifecycle gates and stop guards
- `CLAUDE.md` text invariants do not contradict README diagrams.

## 5) Remote Runner and Operations

- Env var table in README reflects current `scripts/policy_config.py`, `scripts/repl_admin.py`, and runner routing behavior.
- Endpoint list (`/run`, `/health`, `/status`, `/logs`) matches `scripts/remote_runner.py`.
- Any runner protocol change is mirrored in `skills/remote-runner-dev/SKILL.md`.

## 6) Memory Hub and Hub Tooling

- `README.md` Memory Hub section matches `scripts/emerge_sync.py` and `icc_hub` actions.
- Queue contract and conflict states align with `CLAUDE.md` invariants.
- If new `icc_hub` actions/event types were added, update:
  - README MCP tools table
  - `CLAUDE.md` invariants
  - `commands/hub.md` (if user workflow changed)

## 7) Commands and Skills Cross-Check

- `commands/*.md` usage still reflects current tool arguments and behavior.
- `skills/*/SKILL.md` examples and assumptions match current architecture.
- No command/skill references deprecated public tools as primary paths.

## 8) Final Gate (Evidence Before Claims)

- Run full verification: `python -m pytest tests -q`.
- Re-check changed docs for stale numbers/terms (`rg "377|2025-03-26"` etc., scoped appropriately).
- Ensure no contradictory statements remain between `README.md` and `CLAUDE.md`.

## Suggested Release Workflow

1. Implement code changes.
2. Run tests and collect actual baseline numbers.
3. Update docs with this checklist.
4. Re-run a quick targeted grep for known stale tokens.
5. Commit code + docs together when possible.
