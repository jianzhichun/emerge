# Connector Context Layer — Design Spec

**Date:** 2026-04-05  
**Status:** Approved  
**Scope:** ConnectorContext, Connector Manifest, Profile Binding, Crystallize Parameterization, Skills, Memory Hub Publish Contract

---

## Problem Statement

Pipeline `.py` files currently hardcode environment-specific values (IP addresses, ports, timeouts) as module-level constants with optional `args.get()` overrides. This creates three concrete problems:

1. **Memory Hub portability blocked**: `icc_crystallize` copies WAL code verbatim — literal IPs/ports get baked into published pipelines.
2. **Multi-remote unmanageable**: "which machine runs the code" (`RunnerRouter`) and "which socket/COM endpoint the code connects to" are completely separate concerns with no unified management surface.
3. **Parameter undiscoverability**: Extra connector params (`hm_host`, `hm_port`, etc.) flow through `args.get()` at runtime but are absent from the MCP tool schema — callers and installers cannot discover what to configure.

---

## Design Decisions (Non-Negotiable)

- Pipeline function signature (`run_read(metadata, args)`) **will be replaced** — no compatibility shim.
- Existing `runner-map.json` **will be replaced** by `profiles` in `settings.json`.
- Pipeline `.py` files **must not** contain literal IP addresses, hostnames, or port numbers after this change.
- Memory Hub publish gate **blocks** pipelines with unresolved literals or missing manifest.

---

## §1 — ConnectorContext: Pipeline's Single Entry Point

### Motivation

The current two-argument signature (`metadata`, `args`) forces pipeline code to own its own parameter resolution: `host = args.get("hm_host", "192.168.122.21")`. The literal default sits in the pipeline, not in a configuration artifact that can be inspected, overridden per profile, or stripped before publishing.

### Data Model

```python
from dataclasses import dataclass, field
from typing import Any

@dataclass
class ExecutionInfo:
    target_profile: str       # e.g. "hm-vm-a", "local"
    execution_mode: str       # "local" | "remote" | "auto"
    connector: str            # e.g. "hypermesh"
    pipeline: str             # e.g. "state"
    mode: str                 # "read" | "write"

@dataclass
class ConnectorContext:
    params: dict[str, Any]        # fully resolved connector+pipeline params
    metadata: dict[str, Any]      # from pipeline .yaml (intent_signature, steps, policy)
    call_args: dict[str, Any]     # business args from the MCP call (tcl_cmd, model_name, etc.)
    execution: ExecutionInfo
```

### New Pipeline Function Signatures

All five pipeline functions receive `ctx` as first (and only positional) argument:

```python
def run_read(ctx: ConnectorContext) -> list[dict[str, Any]]: ...
def verify_read(ctx: ConnectorContext, rows: list[dict]) -> dict[str, Any]: ...
def run_write(ctx: ConnectorContext) -> dict[str, Any]: ...
def verify_write(ctx: ConnectorContext, action_result: dict) -> dict[str, Any]: ...
def rollback_write(ctx: ConnectorContext, action_result: dict) -> dict[str, Any]: ...
```

### Example: HyperMesh state.py (before → after)

**Before:**
```python
_DEFAULT_HM_HOST = "192.168.122.21"
_DEFAULT_HM_PORT = 9999

def run_read(metadata, args):
    host = str(args.get("hm_host", _DEFAULT_HM_HOST))
    port = int(args.get("hm_port", _DEFAULT_HM_PORT))
    timeout = float(args.get("hm_timeout", 2.0))
```

**After:**
```python
def run_read(ctx):
    host = ctx.params["hm_host"]
    port = ctx.params["hm_port"]
    timeout = ctx.params["hm_timeout"]
```

All params are pre-resolved by the daemon before pipeline invocation. The pipeline contains zero defaults.

### Params Merge Priority (high → low)

1. `call_args` keys that match a declared manifest param name (single-call override)
2. `profiles.<target_profile>.connectors.<connector>` in `settings.json`
3. `manifest.yaml` param `default` values

Merge is performed by `ProfileRegistry.resolve()` in the daemon before `PipelineEngine` is called. `PipelineEngine` receives already-resolved params.

---

## §2 — Connector Manifest: Parameter Declaration and Discovery

### File Location

```
~/.emerge/connectors/<connector>/manifest.yaml
```

### Schema

```yaml
connector: hypermesh
version: "1.0"
description: "Altair HyperMesh via TCP/Tcl socket bridge"

params:
  hm_host:
    type: string
    default: "127.0.0.1"
    description: "HyperMesh Tcl server hostname or IP"
  hm_port:
    type: integer
    default: 9999
    description: "Tcl socket server port"
  hm_timeout:
    type: number
    default: 2.0
    description: "TCP connect/read timeout in seconds"

capabilities:
  - tcl_socket
```

### Rules

- `default` **must be a safe value** — `127.0.0.1` not a private network IP. Memory Hub consumers must be able to run against the default without connecting to a developer's machine.
- Params declared in `manifest.yaml` are **connector-scoped** — shared by all pipelines in this connector.
- Params declared in a pipeline's `.yaml` `params:` block are **pipeline-scoped** — they union with connector params, and pipeline-level declarations override connector-level for the same key.
- `capabilities` is reserved for future Federated Execution Grid routing. Currently declared but not consumed.
- If `manifest.yaml` is absent, `PipelineEngine` treats the connector as having zero params (no merge, empty `ctx.params`). This is the transition-period behavior — Memory Hub publish gate requires manifest to be present.

### Pipeline-Level Param Extension

A pipeline `.yaml` may declare additional params not in the connector manifest:

```yaml
intent_signature: write.hypermesh.apply-change
write_steps: [execute_tcl_command]
verify_steps: [read_back_state]
rollback_or_stop_policy: rollback

params:
  batch_size:
    type: integer
    default: 1
    description: "Number of Tcl commands to batch per round-trip"
```

At merge time, the effective param schema is `union(manifest.params, pipeline.params)`.

---

## §3 — Profile Binding: Unified Environment Management

### Motivation

`runner-map.json` handles runner URL routing. `settings.json` has no connector params section. These are two separate files managing two halves of the same concept: "which environment am I targeting?" This split is replaced by a unified `profiles` section in `settings.json`.

### `~/.emerge/settings.json` New Structure

```json
{
  "policy": { ... },
  "profiles": {
    "local": {
      "execution": "local",
      "connectors": {
        "hypermesh": { "hm_host": "127.0.0.1", "hm_port": 9999 }
      }
    },
    "hm-vm-a": {
      "execution": "remote",
      "runner_url": "http://192.168.122.21:8787",
      "connectors": {
        "hypermesh": { "hm_host": "192.168.122.21", "hm_port": 9999 }
      }
    },
    "hm-vm-b": {
      "execution": "remote",
      "runner_url": "http://192.168.122.22:8787",
      "connectors": {
        "hypermesh": { "hm_host": "192.168.122.22", "hm_port": 9999 }
      }
    }
  },
  "default_profile": "local",
  "runner": { "timeout_s": 30, "retry_max_attempts": 3, ... },
  "metrics_sink": "local_jsonl"
}
```

### Profile Fields

| Field | Required | Description |
|-------|----------|-------------|
| `execution` | yes | `"local"` \| `"remote"` \| `"auto"` |
| `runner_url` | if remote | HTTP URL of the remote runner |
| `connectors` | no | Per-connector param overrides for this profile |

`execution: "auto"` tries remote first; falls back to local if runner is unreachable.

### Migration: runner-map.json → profiles

`runner-map.json` is deprecated and no longer read. Each entry in `runner-map.json` maps to a profile:

| runner-map.json | profiles entry |
|-----------------|----------------|
| `default_url` | profile with `"execution": "remote"` set as `default_profile` |
| `map.<key>` | profile named `<key>` with `"execution": "remote"` |
| `pool` entries | profiles named `pool-0`, `pool-1`, etc. |

`EMERGE_RUNNER_URL` env var override still works — it sets the runner_url for the resolved profile.

### ProfileRegistry

New class in `scripts/profile_registry.py`:

```python
class ProfileRegistry:
    def __init__(self, settings: dict) -> None: ...

    def resolve(
        self,
        target_profile: str | None,
        connector: str,
        manifest_params: dict[str, Any],
        call_args: dict[str, Any],
    ) -> ResolvedProfile: ...
```

```python
@dataclass
class ResolvedProfile:
    profile_name: str
    execution_mode: str          # "local" | "remote"
    runner_client: RunnerClient | None
    resolved_params: dict[str, Any]
```

`resolve()` performs the three-layer merge:
1. Start with `manifest_params` defaults
2. Overlay `profiles.<name>.connectors.<connector>` values
3. Overlay `call_args` keys that exist in the merged param schema

`EmergeDaemon` instantiates `ProfileRegistry` at startup from loaded settings and holds it as `self._profile_registry`.

---

## §4 — Crystallize Parameterization

### Problem

`_crystallize()` takes the last WAL entry's `code` string and indents it verbatim into the generated `.py`. Any literal in that code — including `socket.create_connection(("192.168.122.21", 9999))` — gets baked into the pipeline file.

### Strategy: Hybrid (auto-parameterize simple, flag complex)

#### 4a. Auto-Parameterization

After loading the WAL code and before emitting the pipeline `.py`:

1. Load the connector's `manifest.yaml` to get the param names and their **current resolved values** (from the active profile at crystallize time).
2. Scan the WAL code for string and numeric literals.
3. For each literal, check if its value matches the current resolved value of any declared param.
4. Replace matched literals with `ctx.params["<param_name>"]`.

Example:

WAL code:
```python
sock = socket.create_connection(("192.168.122.21", 9999), timeout=2.0)
```

Active profile has `hm_host="192.168.122.21"`, `hm_port=9999`, `hm_timeout=2.0`. After substitution:

```python
sock = socket.create_connection((ctx.params["hm_host"], ctx.params["hm_port"]), timeout=ctx.params["hm_timeout"])
```

#### 4b. Incomplete Parameterization Flagging

After substitution, scan the generated code for remaining string literals that look like IP addresses (regex: `\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}`) or port numbers in connection contexts.

If any are found:
- Add `needs_review: true` to the pipeline's `.yaml`
- Add a warning comment block to the top of the generated `.py`:

```python
# WARNING: auto-parameterization incomplete — review before publishing to Memory Hub
# Unresolved literals detected: ["10.0.0.5"] (line 14)
# Replace with ctx.params["<param_name>"] after adding param to manifest.yaml
```

Memory Hub publish gate rejects pipelines with `needs_review: true`.

#### 4c. Generated Code Shape

Crystallize always emits the new `ctx` signature:

```python
# auto-generated by icc_crystallize — review before promoting
# intent_signature: write.hypermesh.apply-change
# synthesized_at: <ts>

def run_write(ctx):
    # --- CRYSTALLIZED (parameterized) ---
    <parameterized code>
    # --- END ---
    return __action  # exec code must set __action = {"ok": True, ...}


def verify_write(ctx, action_result):
    return {"ok": bool(action_result.get("ok"))}
```

#### 4d. Remote WAL Writeback

`_run_pipeline_remotely()` currently does not write to the daemon's WAL. After a successful remote pipeline exec, the daemon appends a WAL entry:

```json
{
  "status": "success",
  "no_replay": false,
  "source": "remote",
  "target_profile": "<profile>",
  "code": "<py_source of the pipeline>",
  "metadata": { "intent_signature": "...", ... },
  "ts_ms": <timestamp>
}
```

`icc_crystallize` prefers entries with `source: "remote"` when multiple entries match `intent_signature` — remote execution is the ground truth for connector-facing code.

**Important:** The `code` field written back is the **original `py_source`** of the pipeline file (as loaded by `_load_pipeline_source()`), not the inline exec payload constructed by `_run_pipeline_remotely()`. The exec payload wraps `py_source` with serialized metadata/args and dispatch boilerplate — it is not suitable for crystallization. `py_source` is what crystallize should parameterize and emit.

---

## §5 — Skills: Knowledge Process Codification

### 5a. `binding-execution-profile` (new)

**Trigger:** User wants to add a new machine, configure a new remote runner, or switch execution target.

**Checklist:**
1. Check `~/.emerge/settings.json` exists; create skeleton if not
2. Prompt for profile name, execution mode (`local` / `remote` / `auto`)
3. If remote: collect runner URL, run `runner-bootstrap`, verify health
4. Load target connector's `manifest.yaml`; display required params
5. For each param: prompt for value or accept default
6. Write profile into `settings.json` under `profiles.<name>`
7. Run `icc_read` smoke test to verify connectivity
8. Output change summary (profile name, params set, smoke test result)

**Pass criteria:** Profile written + smoke test passes.

### 5b. `installing-memory-hub-pipeline` (new)

**Trigger:** User wants to install a pipeline package downloaded from Memory Hub.

**Checklist:**
1. Unpack pipeline package to `~/.emerge/connectors/<connector>/`
2. Read `manifest.yaml`; display required params
3. Check if a matching profile exists; if not, invoke `binding-execution-profile`
4. Run `icc_read` + `icc_write` smoke tests
5. Check for `needs_review: true` in any pipeline `.yaml`; if found, display unresolved literals and block proceed until confirmed
6. Verify pipeline key appears in policy registry

**Pass criteria:** read/write pass + policy visible + no unconfirmed `needs_review`.

### 5c. `initializing-vertical-flywheel` (update)

**Changes to existing skill:**
- Creating a connector **must** include creating `manifest.yaml` with all connection params declared
- Pipeline `.py` boilerplate uses `run_read(ctx)` / `run_write(ctx)` signatures
- After connector creation: prompt to bind a profile (invoke `binding-execution-profile`)
- Verification checklist adds: "`manifest.yaml` present and params complete"

**Unchanged:** TDD flow (RED→GREEN→REFACTOR), remote runner bootstrap, policy observability, reverse flywheel prompt.

### Skill Invocation Graph

```
initializing-vertical-flywheel
  → creates connector + manifest.yaml + pipelines (ctx signatures)
  → prompts binding-execution-profile
      → writes profiles.<name> to settings.json
      → runner-bootstrap (if remote)
      → smoke test

installing-memory-hub-pipeline
  → unpacks pipeline package
  → reads manifest.yaml
  → prompts binding-execution-profile (if no matching profile)
  → smoke test + needs_review check
```

---

## §6 — Component Refactoring Summary

### PipelineEngine

**Signature changes:**

```python
# Before
def run_read(self, args: dict) -> dict
def run_write(self, args: dict) -> dict

# After
def run_read(self, connector: str, pipeline: str, resolved_params: dict, call_args: dict, exec_info: ExecutionInfo) -> dict
def run_write(self, connector: str, pipeline: str, resolved_params: dict, call_args: dict, exec_info: ExecutionInfo) -> dict
```

**New responsibilities:**
- Load `manifest.yaml` and validate `resolved_params` covers all params without defaults
- Build `ConnectorContext` from inputs
- Call `module.run_read(ctx)` / `module.run_write(ctx)` etc.

**Removed responsibilities:**
- `args.get("connector")` / `args.get("pipeline")` extraction (moved to caller)
- Params merge (moved to `ProfileRegistry`)

### EmergeDaemon

**New:**
- `self._profile_registry: ProfileRegistry` (initialized at startup)
- `icc_read`/`icc_write` call `ProfileRegistry.resolve()` before `PipelineEngine`
- `_run_pipeline_remotely()` receives `resolved_params` + `call_args` separately; serializes both into exec payload; writes WAL entry on success

**Removed:**
- `RunnerRouter` (replaced by `ProfileRegistry` which embeds runner client resolution)
- `runner-map.json` loading

**MCP Tool Schema update:**
- `icc_read` / `icc_write` `inputSchema` gains a `params` property (pass-through connector param overrides) alongside `connector`, `pipeline`, `target_profile`

### ProfileRegistry (new file: `scripts/profile_registry.py`)

Owns profile loading, resolution, and three-layer param merge. Isolated from daemon business logic — unit-testable independently.

### Settings Schema (`policy_config.py`)

New validated keys in `_DEFAULTS`:

```python
_DEFAULTS = {
    ...
    "profiles": {},           # dict[str, ProfileConfig]
    "default_profile": "local",
    ...
}
```

Profile validation: `execution` must be `"local"` / `"remote"` / `"auto"`; `runner_url` required when `execution == "remote"`.

---

## §7 — Memory Hub Publish Contract

A publishable pipeline package contains:

| File | Required | Notes |
|------|----------|-------|
| `manifest.yaml` | Yes | Connector params with safe defaults |
| `pipelines/<mode>/<name>.yaml` | Yes | Pipeline metadata; may extend params |
| `pipelines/<mode>/<name>.py` | Yes | Uses `ctx` signatures; zero literals |
| `README.md` | No | Human-readable install notes |

### Publish Gate Checks (enforced by `icc_publish` or manual pre-check)

1. `manifest.yaml` present with at least one declared param (or explicit `params: {}` for parameterless connectors)
2. All `.py` files: no IP address literals (regex `\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}`)
3. All `.py` files: pipeline functions use `ctx` signature, not `(metadata, args)`
4. No pipeline `.yaml` has `needs_review: true`
5. Policy registry contains at least one entry for this pipeline (proof of use)

---

## §8 — Test Strategy

| Area | Test Type | Location |
|------|-----------|----------|
| ConnectorContext dataclass + params merge | Unit | `tests/test_connector_context.py` |
| ProfileRegistry resolution + merge priority | Unit | `tests/test_profile_registry.py` |
| PipelineEngine with `ctx` signature | Integration | `tests/test_pipeline_engine.py` |
| Daemon `icc_read`/`icc_write` with profile | Integration | `tests/test_mcp_tools_integration.py` |
| Crystallize: auto-parameterization | Unit | `tests/test_crystallize.py` |
| Crystallize: `needs_review` flag on unresolved literals | Unit | `tests/test_crystallize.py` |
| Remote WAL writeback | Integration | `tests/test_mcp_tools_integration.py` |
| Profile `execution: "auto"` — remote success takes remote path | Integration | `tests/test_mcp_tools_integration.py` |
| Profile `execution: "auto"` — remote unreachable falls back to local | Integration | `tests/test_mcp_tools_integration.py` |
| Publish gate checks | Unit | `tests/test_publish_gate.py` |
| HyperMesh connector with new `ctx` API | Integration | `tests/test_mcp_tools_integration.py` |

---

## §9 — File Changelist

| File | Change |
|------|--------|
| `scripts/connector_context.py` | **New** — `ConnectorContext`, `ExecutionInfo`, `ResolvedProfile` |
| `scripts/profile_registry.py` | **New** — `ProfileRegistry` |
| `scripts/pipeline_engine.py` | **Modify** — new `run_read/write` signature, manifest loading, ctx construction |
| `scripts/emerge_daemon.py` | **Modify** — replace RunnerRouter with ProfileRegistry, remote exec writeback, MCP schema |
| `scripts/policy_config.py` | **Modify** — add `profiles`, `default_profile` to schema + validation |
| `scripts/runner_client.py` | **Modify** — `RunnerRouter` removed; `RunnerClient` kept as pure HTTP client |
| `~/.emerge/connectors/hypermesh/manifest.yaml` | **New** |
| `~/.emerge/connectors/hypermesh/pipelines/read/state.py` | **Modify** — `ctx` signature |
| `~/.emerge/connectors/hypermesh/pipelines/write/apply-change.py` | **Modify** — `ctx` signature |
| `~/.emerge/connectors/zwcad/manifest.yaml` | **New** |
| `~/.emerge/connectors/zwcad/pipelines/read/state.py` | **Modify** — `ctx` signature |
| `~/.emerge/connectors/zwcad/pipelines/write/apply-change.py` | **Modify** — `ctx` signature |
| `tests/connectors/mock/pipelines/read/layers.py` | **Modify** — `ctx` signature |
| `tests/connectors/mock/pipelines/write/add-wall.py` | **Modify** — `ctx` signature |
| `tests/connectors/mock/pipelines/write/add-wall-rollback.py` | **Modify** — `ctx` signature |
| `tests/connectors/mock/manifest.yaml` | **New** |
| `tests/test_connector_context.py` | **New** |
| `tests/test_profile_registry.py` | **New** |
| `tests/test_publish_gate.py` | **New** |
| `tests/test_crystallize.py` | **Modify** — parameterization tests |
| `tests/test_pipeline_engine.py` | **Modify** — ctx signature tests |
| `tests/test_mcp_tools_integration.py` | **Modify** — profile-aware tests, WAL writeback |
| `skills/binding-execution-profile/SKILL.md` | **New** |
| `skills/installing-memory-hub-pipeline/SKILL.md` | **New** |
| `skills/initializing-vertical-flywheel/SKILL.md` | **Modify** — manifest + ctx requirements |
| `README.md` | **Modify** — architecture diagram, component table, env vars, glossary |
| `CLAUDE.md` | **Modify** — update Documentation Update Rules table |

---

## §10 — Glossary Additions

| Term | Definition |
|------|-----------|
| **ConnectorContext (连接器上下文)** | Single entry-point object passed to all pipeline functions. Contains pre-resolved `params`, `metadata`, `call_args`, and `execution` info. Pipelines read connection params exclusively from `ctx.params`. |
| **Connector Manifest (连接器清单)** | `manifest.yaml` at the connector root. Declares all connection parameters with types, descriptions, and safe default values. Required for Memory Hub publishing. |
| **Profile (执行配置)** | A named entry in `settings.json` that binds `execution_mode`, `runner_url`, and per-connector param overrides. Replaces `runner-map.json`. |
| **ProfileRegistry (配置注册表)** | Runtime object that resolves a `target_profile` + `connector` into a `ResolvedProfile` (execution mode, runner client, merged params). |
| **Publish Gate (发布门控)** | Set of checks run before a pipeline is published to Memory Hub. Rejects pipelines with hardcoded literals, old function signatures, or unconfirmed `needs_review` flags. |
| **needs_review** | YAML flag set by `icc_crystallize` when auto-parameterization finds unresolved literals. Blocks Memory Hub publishing until a human confirms or fixes the pipeline. |
| **Remote WAL Writeback (远端 WAL 回写)** | After a successful remote pipeline exec, the daemon appends a `source: "remote"` WAL entry locally, enabling `icc_crystallize` to see code that ran on a remote machine. |
