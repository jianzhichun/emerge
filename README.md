# Emerge

![Version](https://img.shields.io/badge/version-v0.3.50-blue)
![Python](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)
![License](https://img.shields.io/github/license/jianzhichun/emerge?cacheSeconds=300)
![Tests](https://img.shields.io/badge/tests-479%20passing-brightgreen?logo=pytest)

**Emerge** solves a core problem: AI operators repeat the same work but do not learn from it, so every session re-reasons from scratch. It uses a **dual flywheel** to crystallize repeated work into deterministic pipelines: a **forward flywheel** (`icc_exec`/`icc_span_open` tracking → policy promotion explore→canary→stable → auto-crystallized `.py+.yaml` pipelines → zero-LLM execution), and a **reverse flywheel** (`OperatorMonitor` observes human operators → `PatternDetector` detects repetition → elicitation captures intent → AI takes over).

**Emerge** is a Claude Code plugin that implements a **dual flywheel**: repeated AI work is tracked via `icc_exec` (ad-hoc code) and `icc_span_open/close` (intent spans), promoted through a **policy registry** (explore → canary → stable), and **crystallized** into connector pipelines that execute with zero LLM inference once stable. A reverse flywheel watches human operators via `OperatorMonitor`, detects repetition with `PatternDetector`, and hands tasks to the AI.

Design anchors:

- **Connector pipelines** — strict YAML metadata + Python under `~/.emerge/connectors/<connector>/pipelines/`, with verification and rollback policy baked in.
- **Persistent exec** — `icc_exec` runs Python in a durable local session (WAL + profiles). A remote runner is optional — local is the default.
- **Goal control plane** — active goal is decided by an append-only event ledger (`goal-ledger.jsonl`) and versioned snapshot (`goal-snapshot.json`), then injected into hook context.
- **State delta** — hooks and `state://deltas` keep deltas and open risks for context budgeting (`Goal` / `Delta` / `Open Risks`).

## Architecture

Emerge sits **inside the Claude Code process**: the plugin exposes one stdio MCP server and a set of hooks. The daemon is the single control plane; heavy or GUI work is delegated to an **optional HTTP remote runner** while all policy state, registry, and WAL stay local.

```mermaid
flowchart TB
  subgraph ccProcess [Claude Code process]
    CC[Claude Code Agent]
    HK[Plugin Hooks]
    DAEMON[EmergeDaemon]
    CORE[Runtime Core<br/>ExecSession · PipelineEngine · RunnerRouter]
    POLICY[Policy + Goal + State<br/>pipelines-registry · goal snapshot/ledger · state tracker]
  end

  subgraph remoteSide [Optional remote runner side]
    RUNNER[remote_runner.py]
    EVENTBUS[operator-events EventBus]
  end

  CC <-->|MCP stdio / JSON-RPC| DAEMON
  HK -->|context + guardrails| CC
  DAEMON --> CORE
  CORE --> POLICY
  CORE -->|when routed| RUNNER
  EVENTBUS -->|GET /operator-events polled by OperatorMonitor| DAEMON
  RUNNER -->|POST /operator-event from observers| EVENTBUS
```



**Component responsibilities:**


| Component           | Role                                                                                                                                                                                                                                                                                 |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **EmergeDaemon**    | MCP JSON-RPC control plane: routes tool calls, orchestrates exec, pipelines, policy updates, and crystallization.                                                                                                                                                                    |
| **ExecSession**     | Persistent Python execution per profile. WAL records every successful code path for replay and crystallization. One session per `target_profile`.                                                                                                                                    |
| **PipelineEngine**  | Resolves `~/.emerge/connectors/` (or `EMERGE_CONNECTOR_ROOT`), loads strict YAML metadata + Python steps, runs `run_read`/`run_write`/`verify`/`rollback`. Also provides `_load_pipeline_source()` for remote inline execution.                                                      |
| **Policy Registry** | Tracks per-candidate lifecycle (`explore → canary → stable`), rollout %, `synthesis_ready` signal, `human_fix_rate`, and `last_execution_path` (`local`/`remote`). Written to `pipelines-registry.json`.                                                                                                                           |
| **GoalControlPlane** | Owns active goal decisioning (`goal-snapshot.json`) and append-only audit trail (`goal-ledger.jsonl`). All writers submit events; readers consume snapshot.                                                                                                                          |
| **StateTracker**    | Maintains `Delta` / `Open Risks` session state and recovery token budgeting. Goal text is injected from GoalControlPlane snapshot.                                                                                                                                                |
| **RunnerRouter**    | Selects a `RunnerClient` by `target_profile` / `runner_id` (map), consistent hash (pool), or default URL. Returns `None` when no runner is configured → local execution.                                                                                                             |
| **Flywheel bridge** | Short-circuit inside `icc_exec`: when the matching candidate is `stable`, execution is redirected to the pipeline result without LLM inference. Zero overhead path once a pattern is trusted.                                                                                        |
| **Hooks**           | Inject minimal context at session/prompt boundaries; record `Delta` after each `icc_`* call; preserve critical state across **PreCompact**; guard stop/exit with active-span safety checks. `PreToolUse` enforces `intent_signature` conventions, auto-normalizes uppercase signatures via `updatedInput`, and returns `ask` for irreversible `icc_goal_rollback`. Not a second MCP server. |
| **`emerge_sync.py`** | Memory Hub sync agent. Bidirectional connector asset sync via orphan-branch git repo; event-driven push on stable promotion, periodic pull, AI-assisted conflict resolution via `icc_hub` MCP tool. |


## Flows

### 1. Muscle-memory flywheel lifecycle

The full lifecycle from exploratory exec to stable pipeline:

```mermaid
flowchart TD
  A([icc_exec with intent_signature])
  B[ExecSession execute and write WAL]
  C[record exec event to candidates.json]
  D[update policy registry and quality counters]

  subgraph lc [Policy lifecycle]
    E1[explore]
    E2[canary rollout 20 percent]
    E3[stable rollout 100 percent]
  end

  F[synthesis_ready true]
  G([icc_crystallize WAL to pipeline py and yaml])
  H[exec bridge in icc_exec short-circuits to stable pipeline]
  I[span bridge in icc_span_open executes stable pipeline]

  A --> B --> C --> D --> E1
  E1 -->|"attempts >= 20, success >= 95, fix <= 5"| E2
  E2 -->|"attempts >= 40, success >= 97, verify >= 99"| E3
  E2 -->|"2 consecutive failures"| E1
  E3 -->|"2 failures or window success < 90"| E1
  E1 -->|"WAL has replayable code"| F
  F --> G
  E3 --> H
  E3 --> I
  H -->|pipeline events feed registry| D
  I -->|pipeline events feed registry| D
```



### 2. Pipeline execution

Stable pipeline execution has two paths depending on whether a remote runner is configured. User-facing path is `icc_span_open` bridge; `icc_read`/`icc_write` remain internal compatibility paths.

**Local (default).** The daemon calls the pipeline engine in-process. No network, no subprocess.

```
icc_span_open { intent_signature }
  → bridge resolves stable pipeline
  → PipelineEngine.run_read(args)
  → { rows, verify_result, verification_state }
```

**Remote.** When `RunnerRouter` resolves a client for the request, the daemon loads the pipeline source locally, builds a self-contained `icc_exec` payload, and dispatches it over HTTP. The runner machine never needs connector files — a machine change is a URL change only. Pipeline calls request a structured `result_var` from the runner, so parsing does not depend on stdout text. Local and remote paths return the same response shape and verification semantics.

```mermaid
flowchart TD
  OPEN([icc_span_open with intent_signature]) --> RESOLVE[resolve stable pipeline id]
  RESOLVE --> ROUTE{RunnerRouter has client}
  ROUTE -->|No| LOCAL[run PipelineEngine in-process]
  ROUTE -->|Yes| REMOTELOAD[load pipeline source and metadata locally]
  REMOTELOAD --> REMOTEPAYLOAD[build inline icc_exec payload]
  REMOTEPAYLOAD --> REMOTEPOST[POST run to remote runner]
  REMOTEPOST --> REMOTERESULT[receive structured result_var payload]
  LOCAL --> NORMALIZE[normalize rows verify_result verification_state]
  REMOTERESULT --> NORMALIZE
  NORMALIZE --> RETURN([return uniform response to Claude Code])
```



### 3. Remote runner — operations

The runner is a **stateless Python executor** — it accepts `icc_exec` only. All pipeline logic, policy decisions, and state writes happen in the daemon.

**Endpoints**


| Endpoint        | Purpose                                  |
| --------------- | ---------------------------------------- |
| `POST /run`     | Execute one `icc_exec` call              |
| `GET /health`   | Liveness — `{"ok": true, "uptime_s": N}` |
| `GET /status`   | Process info (pid, python, root)         |
| `GET /logs?n=N` | Last N log lines                         |


**Configuration**


| Env var                   | Purpose                                         | Default        |
| ------------------------- | ----------------------------------------------- | -------------- |
| `EMERGE_RUNNER_URL`       | Single default runner                           | —              |
| `EMERGE_RUNNER_MAP`       | JSON `target_profile → URL`                     | —              |
| `EMERGE_RUNNER_URLS`      | Comma-separated URL pool                        | —              |
| `EMERGE_RUNNER_TIMEOUT_S` | Per-request timeout (s)                         | `30`           |
| `EMERGE_OPERATOR_MONITOR` | Enable OperatorMonitor thread in daemon         | `0`            |
| `EMERGE_MONITOR_POLL_S`   | EventBus poll interval (seconds)                | `5`            |
| `EMERGE_MONITOR_MACHINES` | Comma-separated runner profile names to monitor | `default` |
| `EMERGE_STATE_ROOT`         | Override where session state (WAL, checkpoints, registry) is written | `~/.emerge/sessions` |
| `EMERGE_SESSION_ID`         | Override the derived session identifier                               | derived from cwd+git  |
| `EMERGE_RUNNER_CONFIG_PATH` | Path to `runner-map.json` (overrides default location)               | `~/.emerge/runner-map.json` |
| `EMERGE_SETTINGS_PATH`      | Override settings file path                                           | `~/.emerge/settings.json` |
| `EMERGE_SCRIPT_ROOTS`       | Comma-separated allowed roots for `script_ref` resolution             | project root |
| `EMERGE_TARGET_PROFILE`     | Default runner target profile for `repl_admin` commands              | `default` |
| `EMERGE_COCKPIT_DISABLE`    | `1` to disable the `EventRouter` watchdog in the daemon              | enabled   |
| `EMERGE_REPL_ROOT`          | Override the repl state root directory                               | `~/.emerge/repl` |
| `CLAUDE_PLUGIN_DATA`        | Hook + Goal Control state root (state tracker + goal snapshot/ledger) | `~/.claude/plugin-data` |


Persisted route map (`~/.emerge/runner-map.json`):

```json
{
  "default_url": "http://127.0.0.1:8787",
  "map":  { "cad-win": "http://10.0.0.11:8787" },
  "pool": [ "http://10.0.0.11:8787", "http://10.0.0.12:8787" ]
}
```

`map` keys match `target_profile` in tool arguments. `pool` uses consistent hashing so the same profile always lands on the same host.

**Starting**

```bash
# Standard — logs to .runner.log
python3 scripts/remote_runner.py --host 0.0.0.0 --port 8787

# With watchdog — auto-restarts on crash or .watchdog-restart signal
pythonw scripts/runner_watchdog.py --host 0.0.0.0 --port 8787
```

> **Windows / GUI workloads** (AutoCAD, ZWCAD, COM objects): launch from an interactive desktop session (RDP/console), not a Windows service. COM objects are session-scoped.

**One-command bootstrap** (deploy → start → health-check → persist route):

```bash
python3 scripts/repl_admin.py runner-bootstrap \
  --ssh-target "user@10.0.0.11" \
  --target-profile "cad-win" \
  --runner-url "http://10.0.0.11:8787"
```

### 4. Hook and context flow

```mermaid
flowchart LR
  subgraph session [Session lifecycle]
    SS[SessionStart — inject Goal + open risks]
    UPS[UserPromptSubmit — inject Delta summary]
    SE[SessionEnd — clear stale active span fields]
    PC[PreCompact — serialize recovery token]
  end

  subgraph percall [Per tool call]
    PTU[PreToolUse — enforce conventions + normalize args]
    EX[Tool executes]
    POTU[PostToolUse — record Delta]
    PTF[PostToolUseFailure — mark degraded on real failures]
  end

  subgraph stopguard [Stop guard]
    ST[Stop and SubagentStop — block on active span]
  end

  SS -->|additionalContext| Agent
  UPS -->|additionalContext| Agent
  SE -->|cleanup| Agent
  PC -->|additionalContext| Agent
  Agent --> PTU --> EX --> POTU
  EX --> PTF
  Agent --> ST
```



## MCP surface

**Tools:**


| Tool              | Purpose                                                                                                                                                                                      |
| ----------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `icc_span_open`   | Open an intent span to track a multi-step MCP tool call sequence. PostToolUse records every subsequent tool call into the span buffer. At stable, bridges directly to PipelineEngine (zero LLM inference). |
| `icc_span_close`  | Close the current span and commit to the span WAL. At stable, auto-generates a Python skeleton in `_pending/` for human review.                                                              |
| `icc_span_approve` | Move the completed skeleton from `_pending/` to the real pipeline directory and generate YAML metadata. Activates the span bridge for future calls.                                         |
| `icc_exec`        | Execute Python in a persistent local session. Tracks `intent_signature` for flywheel policy. Optionally routes to a remote runner when `target_profile` is mapped — local is the default. Supports `result_var` to return structured JSON from a named global variable. At synthesis_ready, auto-crystallizes WAL into a pipeline. |
| `icc_crystallize` | Generate `.py` + `.yaml` pipeline files from WAL history (manual override). Always writes locally; force-overwrites existing files.                                                          |
| `icc_reconcile`   | Confirm or correct a state delta. `outcome=correct` + `intent_signature` increments `human_fix_rate` on the most-recently-used matching candidate.                                           |
| `icc_goal_ingest` | Submit goal events (`human_edit` / `hook_payload` / `system_*`) to Goal Control Plane. Returns acceptance decision + active snapshot metadata.                                               |
| `icc_goal_read`   | Read active goal snapshot plus recent goal ledger events.                                                                                                                                    |
| `icc_goal_rollback` | Roll back active goal to a previous goal event id and produce a new snapshot version.                                                                                                      |
| `icc_hub`           | Memory Hub management: `configure` (first-time setup — saves config + initialises git worktree, callable from CC via natural language) · `list` config · `add`/`remove` connectors · `sync` (enqueue push+pull) · `status` (pending conflicts + awaiting-application count) · `resolve` conflicts (`ours`/`theirs`/`skip`). |

> `icc_read` / `icc_write` are deprecated and removed from schema. Use `icc_span_open` instead — the span bridge executes the pipeline automatically when stable.


**Resources:** `policy://current` · `runner://status` · `state://deltas` · `state://goal` · `state://goal-ledger` · `pipeline://{connector}/{mode}/{name}` · `connector://{vertical}/notes` · `connector://{vertical}/spans`

**Prompts:** `icc_explore`

**Hooks** (`hooks/hooks.json`): `Setup` · `SessionStart` · `SessionEnd` · `UserPromptSubmit` · `PreToolUse` · `PostToolUse` · `PostToolUseFailure` · `PreCompact` · `Stop` · `SubagentStop`

### MCP protocol compliance (2025-11-25)

Emerge follows MCP 2025-11-25 style metadata and hook control semantics:

- Tool schemas include `title`, `annotations`, and `outputSchema`.
- Server version negotiation returns `min(client_version, "2025-11-25")`.
- `PreToolUse` uses `hookSpecificOutput.permissionDecision` (`allow`/`deny`/`ask`) rather than legacy top-level block format.
- `PostToolUse` can inject `updatedMCPToolOutput` for span correlation (`_span_id`, `_span_intent`).

## What ships in this repo


| Area                     | Location                                                                                                                                       |
| ------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| Plugin manifest          | `.claude-plugin/plugin.json` (`name`: `emerge`), `.claude-plugin/marketplace.json`                                                             |
| Local MCP wiring (dev)   | `.mcp.json` → `scripts/emerge_daemon.py`                                                                                                       |
| MCP server               | `scripts/emerge_daemon.py` (`EmergeDaemon`, stdio JSON-RPC)                                                                                    |
| Pipeline engine & policy | `scripts/pipeline_engine.py`, `scripts/policy_config.py`                                                                                       |
| ExecSession & WAL        | `scripts/exec_session.py`                                                                                                                      |
| State & metrics          | `scripts/state_tracker.py`, `scripts/metrics.py`                                                                                               |
| Remote runner            | `scripts/remote_runner.py`, `scripts/runner_client.py`, `scripts/runner_watchdog.py`                                                           |
| Observer framework       | `scripts/observer_plugin.py`, `scripts/observers/`                                                                                             |
| Pattern detector         | `scripts/pattern_detector.py`                                                                                                                  |
| Distiller                | `scripts/distiller.py`                                                                                                                         |
| Operator monitor         | `scripts/operator_monitor.py`                                                                                                                  |
| Ops / bootstrap / cockpit | `scripts/repl_admin.py` — HTTP cockpit with SSE real-time status, `cockpit_shell.html` SPA frontend                                           |
| Memory Hub sync agent    | `scripts/emerge_sync.py`, `scripts/hub_config.py` — bidirectional connector asset sync via orphan-branch git repo; `icc_hub` MCP tool in daemon |
| Test connector (mock)    | `tests/connectors/mock/pipelines/`                                                                                                             |
| Slash commands           | `commands/` (`init`, `cockpit`, `runner-status`, `import`, `export`, `hub`)                                                                     |
| Skills                   | `skills/` (`initializing-vertical-flywheel`, `remote-runner-dev`, `writing-vertical-adapter`, `operator-monitor-debug`, `policy-optimization`) |
| Reference (submodule)    | `references/claude-code`                                                                                                                       |


**Slash commands:**


| Command          | Description                                                                                      |
| ---------------- | ------------------------------------------------------------------------------------------------ |
| `/init`          | Initialize a vertical flywheel from natural language context                                     |
| `/cockpit`       | Browser control plane — SSE real-time status, intent overview, delta/risk/span/exec panels, audit trail, session mgmt |
| `/runner-status` | Show remote runner health status                                                                 |
| `/import`        | Import a connector asset package zip into local connector/pipeline state                         |
| `/export`        | Export a connector asset package zip (connector files + registry entries)                        |
| `/hub`           | Memory Hub status — show pending conflicts, awaiting-application count, and resolution guidance  |


## Cockpit Preview

<p align="center">
  <a href="docs/images/cockpit/cockpit-overview.jpg">
    <img src="docs/images/cockpit/cockpit-overview.jpg" alt="Cockpit Overview" width="31%" />
  </a>
  <a href="docs/images/cockpit/cockpit-state.jpg">
    <img src="docs/images/cockpit/cockpit-state.jpg" alt="Cockpit State" width="31%" />
  </a>
  <a href="docs/images/cockpit/cockpit-session.jpg">
    <img src="docs/images/cockpit/cockpit-session.jpg" alt="Cockpit Session" width="31%" />
  </a>
</p>

<p align="center">
  <sub>
    <b>Overview</b> · policy posture & rollout
    &nbsp;&nbsp;|&nbsp;&nbsp;
    <b>State</b> · L3 diagnostics and intent-linked timeline
    &nbsp;&nbsp;|&nbsp;&nbsp;
    <b>Session</b> · WAL/checkpoint/recovery controls
  </sub>
</p>

<p align="center"><sub>Tip: click any image to open full size.</sub></p>


## Requirements

- **Python** 3.11+
- **PyYAML** — pipeline metadata loading at runtime
- **pytest** — test suite only

## Quick verification

```bash
python -m pytest tests -q
```

Current baseline: **479** tests passing.

Documentation release checklist: `docs/doc-consistency-checklist.md`

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

## Roadmap


|             |                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 🟢 shipped  | **Solo Flywheel** Per-session learning on a single machine. `icc_exec` accumulates history → `icc_crystallize` generates a pipeline → explore → canary → stable. Stable pipelines short-circuit at the tool layer with zero LLM overhead. Remote runner dispatch included — daemon sends self-contained inline code, runner needs no connector files.                                                                                                                                                                                                                                                        |
| 🟢 shipped  | **Operator Intelligence Loop** A reverse flywheel that observes the *human*, not just the AI. A background monitor audits operator behavior on a configurable time window (default 5 min) — surfacing a native GUI popup: *"you've done this 8 times today — why? want me to take it?"* Intent is captured, patterns are distilled into operator skill profiles, and repetitive sequences are handed off to the AI layer. The goal: progressively free operators from work that is mechanical, high-frequency, or already crystallized somewhere in the pipeline registry. Operator as author, not executor. |
| 🟢 shipped  | **Memory Hub** Bidirectional connector asset sync via self-hosted git orphan branch. Stable pipelines auto-push on policy promotion; periodic background pull keeps all machines in sync. Conflicts surfaced via `icc_hub status` and resolved with `ours`/`theirs`/`skip`. Assets shared: pipeline `.py`+`.yaml`, `NOTES.md`, stable `spans.json`. Never synced: policy registry, session state, credentials.                                                                                                                                                                                              |
| 🟡 planned  | **Federated Execution Grid** Multiple runners with capability tags (`zwcad`, `cuda12`, `android-emu`). `RunnerRouter` picks by capability, not just URL. Failover to next capable host. Cross-session policy: a failure on one machine can demote the pipeline globally.                                                                                                                                                                                                                                                                                                                                     |
| 🔮 research | **Split-Personality Flywheel** Today the flywheel crystallizes *actions* → deterministic pipelines (no LLM). Next: crystallize *reasoning patterns* → specialized subagent personas (compressed system prompt + tools + few-shot traces). Subagents dispatch to stable pipelines. Two tiers of crystallization — code where the task is deterministic, compressed mind where it isn't.                                                                                                                                                                                                                       |


## Glossary


| Term                          | Definition                                                                                                                                                                                                                                                                                                                                                                                           |
| ----------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Adapter**                    | An `ObserverPlugin` subclass that provides application-specific observation and takeover capability for a specific vertical (e.g. ZWCAD COM, Excel). Generic built-in observers (`accessibility`, `filesystem`, `clipboard`) ship with the framework; vertical adapters are crystallized from WAL history via `icc_crystallize mode=adapter` and live in `~/.emerge/adapters/<vertical>/adapter.py`. |
| **Candidate**                  | A tracked execution pattern identified by `intent_signature`. Carries policy counters (attempts, successes, human-fix rate) that drive lifecycle transitions. Multiple candidates can share the same `intent_signature` (e.g. exec vs pipeline variants).                                                                                                                                             |
| **Connector**                  | A named integration target (e.g. `zwcad`, `mock`). Owns pipeline definitions under `~/.emerge/connectors/<connector>/pipelines/read/` and `.../write/`.                                                                                                                                                                                                                                               |
| **Crystallization**            | Generating a deterministic `.py` + `.yaml` pipeline from WAL history via `icc_crystallize`. Converts accumulated exec knowledge into a reusable, verifiable pipeline.                                                                                                                                                                                                                                 |
| **EventBus**                   | Append-only JSONL file per machine at `~/.emerge/operator-events/<machine_id>/events.jsonl`. Written by `ObserverPlugin` instances on the operator machine via `POST /operator-event` to the remote runner. Consumed by `OperatorMonitor` via `GET /operator-events`.                                                                                                                                |
| **Flywheel bridge**            | Short-circuit inside `icc_exec`: when the matching candidate is `stable`, the call is redirected to the pipeline result with zero LLM inference.                                                                                                                                                                                                                                                      |
| **Intent signature**           | Dot-notation string (e.g. `zwcad.read.state`) that identifies the semantic intent of an `icc_exec` call. The policy flywheel tracks all counters per intent signature.                                                                                                                                                                                                                                |
| **ObserverPlugin**             | Abstract base class for operator behavior observation. Defines four methods: `start(config)`, `stop()`, `get_context(hint) -> dict` (pre-elicitation context read), `execute(intent, params) -> dict` (takeover). Mirrors the `Pipeline` contract for the reverse flywheel.                                                                                                                           |
| **OperatorMonitor**            | Background thread inside `EmergeDaemon` (enabled via `EMERGE_OPERATOR_MONITOR=1`). Polls remote runners for operator events, runs `PatternDetector`, calls `adapter.get_context()` for pre-elicitation context, then pushes to CC via MCP channel notification (explore stage) or `ElicitRequest` (canary/stable).                                                                                    |
| **PatternDetector**            | Analyses batches of operator events and emits `PatternSummary` objects when thresholds are crossed. Pluggable strategies: frequency (3 same-type events in 20 min), error-rate (undo ratio >= 0.4), cross-machine (same pattern on >=2 machines). Filters out `session_role=monitor_sub` events to prevent AI self-monitoring.                                                                         |
| **Pipeline**                   | Strict-YAML metadata + Python pair implementing a deterministic `run_read` / `run_write` / `verify` / `rollback` contract. JSON-style metadata in `.yaml` files is not supported. Lives in the connector directory; never needs to exist on the runner machine.                                                                                                                                       |
| **Policy lifecycle**           | Three-stage promotion path: `explore` (accumulating history, 0% rollout) -> `canary` (partial rollout, 20%) -> `stable` (full trust, 100%). Demotion on consecutive failures or low window success rate.                                                                                                                                                                                              |
| **Reverse flywheel**           | The Operator Intelligence Loop: observes the human operator (not the AI), detects repeated patterns, surfaces a CC dialog to capture intent, and hands off to the AI layer. Feeds the same policy registry and crystallization mechanism as the forward flywheel.                                                                                                                                     |
| **State delta**                | A recorded change in system state maintained by `StateTracker`. Surfaced via hooks as `additionalContext` to keep the agent aware of what has changed since the last prompt.                                                                                                                                                                                                                          |
| **Target profile**             | String key (e.g. `default`, `cad-win`) that identifies an execution environment. Routes `icc_exec` to the matching remote runner or local `ExecSession`.                                                                                                                                                                                                                                              |
| **WAL**                        | Write-ahead log — append-only record of successful `icc_exec` code paths per session profile. Primary source material for crystallization.                                                                                                                                                                                                                                                            |


## Reference sources

Claude Code source is vendored under `references/` as read-only context so the Emerge implementation can evolve independently.