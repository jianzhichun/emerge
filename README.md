# Emerge

![Version](https://img.shields.io/badge/version-v0.2.0-blue)
![Python](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)
![License](https://img.shields.io/github/license/jianzhichun/emerge)
![Tests](https://img.shields.io/badge/tests-133%20passing-brightgreen?logo=pytest)

**Emerge** is a Claude Code plugin (v0.2.0) that implements a **muscle-memory flywheel**: repeated work is tracked via `icc_exec`, promoted through a **policy registry** (explore → canary → stable), and can be **crystallized** into connector pipelines so the same tasks run as structured `icc_read` / `icc_write` instead of ad-hoc code.

Design anchors:

- **A-track pipelines** — YAML + Python under `~/.emerge/connectors/<connector>/pipelines/` with read/write verification and rollback policy.
- **Persistent exec** — `icc_exec` runs Python in a durable session (WAL, profiles, optional remote runner).
- **State delta** — hooks and `state://deltas` keep goals, deltas, and open risks for context budgeting (`Goal` / `Delta` / `Open Risks`).

## Architecture

Emerge sits **inside the Claude Code process**: the plugin exposes one stdio MCP server and a set of hooks. The daemon is the single control plane; heavy or GUI work is delegated to an **optional HTTP remote runner** while all policy state, registry, and WAL stay local.

```mermaid
flowchart TB
  subgraph cc [Claude Code]
    Agent[Model / Agent]
    Hooks[Hook scripts]
  end

  subgraph daemon [EmergeDaemon — scripts/emerge_daemon.py]
    T[icc_exec · icc_read · icc_write · icc_crystallize · icc_reconcile]
    R[policy:// · runner:// · state:// · pipeline://]
  end

  subgraph core [Runtime Core]
    ES[ExecSession + WAL]
    PE[PipelineEngine]
    PR[Policy Registry]
    ST[StateTracker]
    Met[Metrics sink]
    RR[RunnerRouter]
  end

  subgraph persist [Persistence]
    SD[session/ — wal.jsonl · candidates.json · events]
    Reg[~/.emerge/ — pipelines-registry · connectors]
    PD[.plugin-data/ — state.json · metrics.jsonl]
  end

  subgraph runner [Remote Runner — optional]
    RC[RunnerClient]
    RProc[remote_runner.py — pure icc_exec executor]
  end

  Agent <-->|MCP stdio / JSON-RPC 2.0| daemon
  Hooks -->|additionalContext — Goal · Delta| Agent

  T --> ES & PE & PR & ST & Met & RR

  ES --> SD
  PR --> SD & Reg
  PE --> Reg
  ST & Met --> PD

  RR --> RC -->|HTTP POST /run — icc_exec only| RProc
```

> **Remote pipeline execution:** When a runner is configured, `icc_read`/`icc_write` are **not** sent to the runner as-is. The daemon loads the pipeline `.py` + `.yaml` files **locally**, builds self-contained inline code, and sends it as `icc_exec` to the runner. Pipeline files never need to be copied to remote machines — switching runners is a URL change only.

**Component responsibilities:**

| Component | Role |
|-----------|------|
| **EmergeDaemon** | MCP JSON-RPC control plane: routes tool calls, orchestrates exec, pipelines, policy updates, and crystallization. |
| **ExecSession** | Persistent Python execution per profile. WAL records every successful code path for replay and crystallization. One session per `target_profile`. |
| **PipelineEngine** | Resolves `~/.emerge/connectors/` (or `EMERGE_CONNECTOR_ROOT`), loads YAML metadata + Python steps, runs `run_read`/`run_write`/`verify`/`rollback`. Also provides `_load_pipeline_source()` for remote inline execution. |
| **Policy Registry** | Tracks per-candidate lifecycle (`explore → canary → stable`), rollout %, `synthesis_ready` signal, `human_fix_rate`. Written to `pipelines-registry.json`. |
| **StateTracker** | Maintains `Goal` / `Delta` / `Open Risks` session state. Exposed via `state://deltas` resource and hook `additionalContext`. |
| **RunnerRouter** | Selects a `RunnerClient` by `target_profile` / `runner_id` (map), consistent hash (pool), or default URL. Returns `None` when no runner is configured → local execution. |
| **Hooks** | Inject minimal context at session/prompt boundaries; record `Delta` after each `icc_*` call; preserve critical state across **PreCompact**. Not a second MCP server. |

## Flows

### 1. Muscle-memory flywheel

The full lifecycle from exploratory exec to stable pipeline:

```mermaid
flowchart TD
  A([icc_exec + intent_signature])
  B[ExecSession — execute + write WAL]
  C[record exec event — candidates.json]
  D[update policy registry — recompute rates]

  subgraph lc [Policy lifecycle]
    E1[explore]
    E2[canary — rollout 20%]
    E3[stable — rollout 100%]
  end

  F[synthesis_ready = true]
  G([icc_crystallize — WAL to .py + .yaml])
  H[icc_read / icc_write — validate pipeline]
  I[flywheel bridge — exec short-circuits to pipeline]

  A --> B --> C --> D --> E1
  E1 -->|attempts≥20 · success≥95% · fix≤5%| E2
  E2 -->|attempts≥40 · success≥97% · verify≥99%| E3
  E2 -->|2 consecutive failures| E1
  E3 -->|2 failures or window rate under 90%| E1
  E1 -->|WAL has replayable code| F
  F --> G --> H -->|pipeline events feed registry| D
  E3 --> I
```

Design detail: `docs/superpowers/specs/2026-04-04-muscle-memory-flywheel-design.md`

### 2. Remote pipeline execution

When a remote runner is configured, `icc_read` / `icc_write` still run pipeline logic on the remote host — but the runner never needs connector files:

```mermaid
sequenceDiagram
  participant CC as Claude Code
  participant D as EmergeDaemon
  participant PE as PipelineEngine
  participant RR as RunnerRouter
  participant R as Remote Runner

  CC->>D: icc_read { connector, pipeline, target_profile }
  D->>RR: find_client(arguments)
  RR-->>D: RunnerClient
  D->>PE: _load_pipeline_source(connector, mode, pipeline)
  PE-->>D: metadata dict + py_source text
  Note over D: build self-contained icc_exec payload
  D->>R: POST /run { tool_name: icc_exec, code: inline, no_replay: true }
  R-->>D: { ok: true, result: { content: stdout JSON } }
  Note over D: parse stdout, assemble icc_read response
  D-->>CC: { rows, verify_result, verification_state }
```

### 3. Remote runner — setup and protocol

The runner is a **pure Python executor**: it only accepts `icc_exec`. Pipeline operations (`icc_read`, `icc_write`) are handled by the daemon, which loads `.py` + `.yaml` locally, builds self-contained inline code, and sends it as `icc_exec`. No connector files ever need to exist on the runner machine — switching machines is a URL change only.

**HTTP endpoints**

| Endpoint | Purpose |
|----------|---------|
| `POST /run` | Execute one `icc_exec` call |
| `GET /health` | Liveness probe |
| `GET /status` | Process info (pid, uptime, root) |
| `GET /logs?n=N` | Last N log lines (default 100) |

Request / response shape:
```json
{ "tool_name": "icc_exec", "arguments": { "code": "...", "target_profile": "default", "no_replay": false } }
{ "ok": true,  "result": { "isError": false, "content": [{ "type": "text", "text": "..." }] } }
{ "ok": false, "error": "string message" }
```

**Configuration**

Env vars override persisted config at runtime:

| Var | Description | Default |
|-----|-------------|---------|
| `EMERGE_RUNNER_URL` | Single default runner URL | — |
| `EMERGE_RUNNER_MAP` | JSON object: `target_profile` → URL | — |
| `EMERGE_RUNNER_URLS` | Comma-separated pool of URLs | — |
| `EMERGE_RUNNER_TIMEOUT_S` | Per-request timeout (seconds) | `30` |
| `EMERGE_RUNNER_CONFIG_PATH` | Override persisted config path | `~/.emerge/runner-map.json` |

Persisted config (`~/.emerge/runner-map.json`):
```json
{
  "default_url": "http://127.0.0.1:8787",
  "map": { "mycader-1": "http://10.0.0.11:8787" },
  "pool": ["http://10.0.0.11:8787", "http://10.0.0.12:8787"]
}
```

`map` keys match `target_profile` in tool arguments. `pool` uses consistent hashing on profile/connector to pick a stable endpoint.

**Starting the runner**

```bash
# Foreground — logs written to .runner.log
python3 scripts/remote_runner.py --host 0.0.0.0 --port 8787

# Watchdog — auto-restarts on crash or .watchdog-restart signal file
pythonw scripts/runner_watchdog.py --host 0.0.0.0 --port 8787
```

For GUI/COM workloads (e.g. AutoCAD), the runner must run in an **interactive user session**, not a Windows service session.

Bootstrap helper (remote deploy + health probe + runner-map write):
```bash
python3 scripts/repl_admin.py runner-bootstrap \
  --ssh-target "user@host" \
  --target-profile "mycader-1" \
  --runner-url "http://10.0.0.11:8787"
```

### 4. Hook and context flow

```mermaid
flowchart LR
  subgraph session [Session lifecycle]
    SS[SessionStart — inject Goal + open risks]
    UPS[UserPromptSubmit — inject Delta summary]
    PC[PreCompact — serialize recovery token]
  end

  subgraph percall [Per tool call]
    PTU[PreToolUse — validate args]
    EX[Tool executes]
    POTU[PostToolUse — record Delta]
  end

  SS -->|additionalContext| Agent
  UPS -->|additionalContext| Agent
  PC -->|additionalContext| Agent
  Agent --> PTU --> EX --> POTU
```

## MCP surface

**Tools:**

| Tool | Purpose |
|------|---------|
| `icc_exec` | Execute Python in a persistent session. Tracks `intent_signature` for flywheel policy. Routes to remote runner if `target_profile` is mapped. |
| `icc_read` | Run a read pipeline with verification. Returns `{ rows, verify_result, verification_state }`. Falls back to structured `pipeline_missing` hint when no pipeline exists yet. |
| `icc_write` | Run a write pipeline with verification and rollback/stop policy enforcement. |
| `icc_crystallize` | Generate `.py` + `.yaml` pipeline files from WAL history. Call when `synthesis_ready: true` appears in `policy://current`. Always writes locally. |
| `icc_reconcile` | Confirm / correct / retract a StateTracker delta. `outcome=correct` + `intent_signature` increments `human_fix_rate` for the matching candidate. |

**Resources:** `policy://current` · `runner://status` · `state://deltas` · `pipeline://{connector}/{mode}/{name}`

**Prompts:** `icc_explore`

**Hooks** (`hooks/hooks.json`): `Setup` · `SessionStart` · `UserPromptSubmit` · `PreToolUse` · `PostToolUse` · `PostToolUseFailure` · `PreCompact`

## What ships in this repo

| Area | Location |
|------|----------|
| Plugin manifest | `.claude-plugin/plugin.json` (`name`: `emerge`), `.claude-plugin/marketplace.json` |
| Local MCP wiring (dev) | `.mcp.json` → `scripts/emerge_daemon.py` |
| MCP server | `scripts/emerge_daemon.py` (`EmergeDaemon`, stdio JSON-RPC) |
| Pipeline engine & policy | `scripts/pipeline_engine.py`, `scripts/policy_config.py` |
| Exec session & WAL | `scripts/exec_session.py` |
| State & metrics | `scripts/state_tracker.py`, `scripts/metrics.py` |
| Remote runner | `scripts/remote_runner.py`, `scripts/runner_client.py`, `scripts/runner_watchdog.py` |
| Ops / bootstrap | `scripts/repl_admin.py` |
| Test connector (mock) | `tests/connectors/mock/pipelines/` |
| Slash commands | `commands/` (`init`, `policy`, `runner-status`) |
| Skills | `skills/` (`initializing-vertical-flywheel`, `muscle-memory-flywheel`, `remote-runner-dev`) |
| Design spec | `docs/superpowers/specs/2026-04-04-muscle-memory-flywheel-design.md` |
| Reference (submodule) | `references/claude-code` |

## Requirements

- **Python** 3.11+
- **PyYAML** — pipeline metadata loading at runtime
- **pytest** — test suite only

## Quick verification

```bash
python -m pytest tests -q
```

Current baseline: **133** tests passing.

## Repository layout

```
scripts/            MCP daemon and runtime core
hooks/              Claude Code hook scripts
tests/              Unit and integration tests
tests/connectors/   Mock connector pipelines (test fixture, not shipped)
commands/           Slash commands bundled with plugin
skills/             Skill docs bundled with plugin
docs/superpowers/specs/   Design specifications
references/         External reference codebases (git submodule)
```

## Reference sources

Claude Code source is vendored under `references/` as read-only context so the Emerge implementation can evolve independently.
