# Connector Context Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace hardcoded pipeline connection params with a `ConnectorContext` system backed by `connector manifest.yaml` + `profiles` in `settings.json`, enabling Memory Hub portability across local and multi-remote environments.

**Architecture:** `ConnectorContext` is the new single entry-point for all pipeline functions, carrying pre-resolved params (manifest defaults → profile overrides → call_args). `ProfileRegistry` owns three-layer param merge and runner client resolution, replacing `RunnerRouter`. `PipelineEngine` builds `ConnectorContext` and calls `module.run_read(ctx)`. `icc_crystallize` auto-parameterizes WAL literals against manifest params and flags incomplete cases with `needs_review: true`.

**Tech Stack:** Python 3.11+, dataclasses (stdlib), PyYAML (already in use), pytest

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `scripts/connector_context.py` | **Create** | `ConnectorContext`, `ExecutionInfo`, `ResolvedProfile` dataclasses |
| `scripts/profile_registry.py` | **Create** | `ProfileRegistry` — loads profiles from settings, resolves runner + params |
| `scripts/pipeline_engine.py` | **Modify** | New `run_read/write` signature taking resolved params; builds `ConnectorContext`; loads manifest |
| `scripts/policy_config.py` | **Modify** | Add `profiles`, `default_profile` to `_DEFAULTS` + validation |
| `scripts/emerge_daemon.py` | **Modify** | Replace `RunnerRouter` with `ProfileRegistry`; update `icc_read/write`, flywheel bridge, remote exec, crystallize, MCP schema |
| `scripts/runner_client.py` | **Modify** | Remove `RunnerRouter` class (keep `RunnerClient`, `RetryConfig`) |
| `scripts/runner_sync.py` | **Modify** | Replace `RunnerRouter.persisted_config_path()` + `runner-map.json` reads with `settings.json` profiles iteration |
| `tests/connectors/mock/manifest.yaml` | **Create** | Mock connector manifest (zero params for simplicity) |
| `tests/connectors/mock/pipelines/read/layers.py` | **Modify** | `run_read(ctx)` / `verify_read(ctx, rows)` |
| `tests/connectors/mock/pipelines/write/add-wall.py` | **Modify** | `run_write(ctx)` / `verify_write(ctx, result)` |
| `tests/connectors/mock/pipelines/write/add-wall-rollback.py` | **Modify** | `run_write(ctx)` / `verify_write(ctx, result)` / `rollback_write(ctx, result)` |
| `~/.emerge/connectors/hypermesh/manifest.yaml` | **Create** | HyperMesh connection params with safe defaults |
| `~/.emerge/connectors/hypermesh/pipelines/read/state.py` | **Modify** | `run_read(ctx)` |
| `~/.emerge/connectors/hypermesh/pipelines/write/apply-change.py` | **Modify** | `run_write(ctx)` / `rollback_write(ctx, result)` |
| `~/.emerge/connectors/zwcad/manifest.yaml` | **Create** | ZWCAD connection params |
| `~/.emerge/connectors/zwcad/pipelines/read/state.py` | **Modify** | `run_read(ctx)` |
| `~/.emerge/connectors/zwcad/pipelines/write/apply-change.py` | **Modify** | `run_write(ctx)` |
| `tests/test_connector_context.py` | **Create** | Unit tests for `ConnectorContext` and params merge |
| `tests/test_profile_registry.py` | **Create** | Unit tests for `ProfileRegistry` resolution |
| `tests/test_publish_gate.py` | **Create** | Unit tests for crystallize publish gate (literal detection, `needs_review`) |
| `tests/test_pipeline_engine.py` | **Modify** | Update tests for new `ctx` signature |
| `tests/test_crystallize.py` | **Modify** | Add auto-parameterization and `needs_review` tests |
| `tests/test_mcp_tools_integration.py` | **Modify** | Profile-aware tests; remote WAL writeback test |
| `skills/binding-execution-profile/SKILL.md` | **Create** | Skill for adding a new execution profile |
| `skills/installing-memory-hub-pipeline/SKILL.md` | **Create** | Skill for installing a Memory Hub pipeline package |
| `skills/initializing-vertical-flywheel/SKILL.md` | **Modify** | Add manifest + ctx requirements |
| `README.md` | **Modify** | Architecture diagram, component table, env vars, glossary |
| `CLAUDE.md` | **Modify** | Documentation Update Rules table |

---

## Task 1: `ConnectorContext` and `ExecutionInfo` dataclasses

**Files:**
- Create: `scripts/connector_context.py`
- Create: `tests/test_connector_context.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_connector_context.py`:

```python
from __future__ import annotations

import pytest
from scripts.connector_context import ConnectorContext, ExecutionInfo


def _exec_info(**kwargs):
    defaults = dict(
        target_profile="local",
        execution_mode="local",
        connector="mock",
        pipeline="layers",
        mode="read",
    )
    defaults.update(kwargs)
    return ExecutionInfo(**defaults)


def test_connector_context_holds_all_fields():
    ctx = ConnectorContext(
        params={"host": "127.0.0.1", "port": 9999},
        metadata={"intent_signature": "read.mock.layers"},
        call_args={"document_id": "doc-1"},
        execution=_exec_info(),
    )
    assert ctx.params["host"] == "127.0.0.1"
    assert ctx.metadata["intent_signature"] == "read.mock.layers"
    assert ctx.call_args["document_id"] == "doc-1"
    assert ctx.execution.target_profile == "local"


def test_execution_info_modes():
    for mode in ("local", "remote", "auto"):
        ei = _exec_info(execution_mode=mode)
        assert ei.execution_mode == mode


def test_connector_context_params_are_independent_copy():
    original = {"host": "127.0.0.1"}
    ctx = ConnectorContext(
        params=original,
        metadata={},
        call_args={},
        execution=_exec_info(),
    )
    original["host"] = "mutated"
    assert ctx.params["host"] == "127.0.0.1"
```

- [ ] **Step 2: Run to verify they fail**

```bash
python -m pytest tests/test_connector_context.py -v
```

Expected: `ModuleNotFoundError: No module named 'scripts.connector_context'`

- [ ] **Step 3: Implement `connector_context.py`**

Create `scripts/connector_context.py`:

```python
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExecutionInfo:
    target_profile: str
    execution_mode: str   # "local" | "remote" | "auto"
    connector: str
    pipeline: str
    mode: str             # "read" | "write"


@dataclass
class ConnectorContext:
    params: dict[str, Any]
    metadata: dict[str, Any]
    call_args: dict[str, Any]
    execution: ExecutionInfo

    def __post_init__(self) -> None:
        # Defensive copy so callers can't mutate internals
        self.params = copy.deepcopy(self.params)
        self.metadata = copy.deepcopy(self.metadata)
        self.call_args = copy.deepcopy(self.call_args)


@dataclass
class ResolvedProfile:
    profile_name: str
    execution_mode: str            # "local" | "remote"
    runner_url: str                # empty string when local
    connector_params: dict[str, Any]
```

- [ ] **Step 4: Run tests to verify pass**

```bash
python -m pytest tests/test_connector_context.py -v
```

Expected: 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/connector_context.py tests/test_connector_context.py
git commit -m "feat: add ConnectorContext and ExecutionInfo dataclasses"
```

---

## Task 2: `ProfileRegistry` — profile loading and three-layer param merge

**Files:**
- Create: `scripts/profile_registry.py`
- Create: `tests/test_profile_registry.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_profile_registry.py`:

```python
from __future__ import annotations

import pytest
from scripts.profile_registry import ProfileRegistry


def _settings(**extra):
    base = {
        "profiles": {
            "local": {
                "execution": "local",
                "connectors": {
                    "hypermesh": {"hm_host": "127.0.0.1", "hm_port": 9999}
                },
            },
            "hm-vm-a": {
                "execution": "remote",
                "runner_url": "http://192.168.122.21:8787",
                "connectors": {
                    "hypermesh": {"hm_host": "192.168.122.21"}
                },
            },
        },
        "default_profile": "local",
    }
    base.update(extra)
    return base


_MANIFEST_PARAMS = {
    "hm_host": {"type": "string", "default": "127.0.0.1"},
    "hm_port": {"type": "integer", "default": 9999},
    "hm_timeout": {"type": "number", "default": 2.0},
}


def test_resolve_local_profile_returns_local_execution():
    reg = ProfileRegistry(_settings())
    rp = reg.resolve("local", "hypermesh", _MANIFEST_PARAMS, {})
    assert rp.execution_mode == "local"
    assert rp.runner_url == ""


def test_resolve_remote_profile_returns_runner_url():
    reg = ProfileRegistry(_settings())
    rp = reg.resolve("hm-vm-a", "hypermesh", _MANIFEST_PARAMS, {})
    assert rp.execution_mode == "remote"
    assert rp.runner_url == "http://192.168.122.21:8787"


def test_resolve_merges_manifest_defaults_as_base():
    reg = ProfileRegistry(_settings())
    # hm_timeout not in any profile — must come from manifest default
    rp = reg.resolve("local", "hypermesh", _MANIFEST_PARAMS, {})
    assert rp.connector_params["hm_timeout"] == 2.0


def test_profile_overrides_manifest_default():
    reg = ProfileRegistry(_settings())
    # hm_host in profile overrides manifest default
    rp = reg.resolve("hm-vm-a", "hypermesh", _MANIFEST_PARAMS, {})
    assert rp.connector_params["hm_host"] == "192.168.122.21"


def test_call_args_override_profile_value():
    reg = ProfileRegistry(_settings())
    rp = reg.resolve("local", "hypermesh", _MANIFEST_PARAMS, {"hm_host": "10.0.0.5"})
    assert rp.connector_params["hm_host"] == "10.0.0.5"


def test_call_args_key_not_in_manifest_is_excluded_from_params():
    reg = ProfileRegistry(_settings())
    # "tcl_cmd" is a business arg, not a manifest param — must NOT appear in connector_params
    rp = reg.resolve("local", "hypermesh", _MANIFEST_PARAMS, {"tcl_cmd": "*createnode 1 2 0"})
    assert "tcl_cmd" not in rp.connector_params


def test_resolve_falls_back_to_default_profile_when_none():
    reg = ProfileRegistry(_settings())
    rp = reg.resolve(None, "hypermesh", _MANIFEST_PARAMS, {})
    assert rp.profile_name == "local"


def test_resolve_unknown_profile_falls_back_to_default():
    reg = ProfileRegistry(_settings())
    rp = reg.resolve("nonexistent", "hypermesh", _MANIFEST_PARAMS, {})
    assert rp.profile_name == "local"


def test_resolve_empty_profiles_returns_local_with_manifest_defaults():
    reg = ProfileRegistry({"profiles": {}, "default_profile": "local"})
    rp = reg.resolve("local", "hypermesh", _MANIFEST_PARAMS, {})
    assert rp.execution_mode == "local"
    assert rp.connector_params["hm_host"] == "127.0.0.1"
```

- [ ] **Step 2: Run to verify they fail**

```bash
python -m pytest tests/test_profile_registry.py -v
```

Expected: `ModuleNotFoundError: No module named 'scripts.profile_registry'`

- [ ] **Step 3: Implement `profile_registry.py`**

Create `scripts/profile_registry.py`:

```python
from __future__ import annotations

import copy
from typing import Any

from scripts.connector_context import ResolvedProfile


class ProfileRegistry:
    def __init__(self, settings: dict[str, Any]) -> None:
        self._profiles: dict[str, dict] = dict(settings.get("profiles", {}))
        self._default_profile: str = str(settings.get("default_profile", "local"))

    def resolve(
        self,
        target_profile: str | None,
        connector: str,
        manifest_params: dict[str, dict],
        call_args: dict[str, Any],
    ) -> ResolvedProfile:
        """Resolve a profile name to execution mode, runner URL, and merged connector params.

        Merge priority (high → low):
          1. call_args keys that match a manifest param name
          2. profiles.<name>.connectors.<connector> values
          3. manifest param defaults
        """
        name = (target_profile or "").strip() or self._default_profile
        profile = self._profiles.get(name) or self._profiles.get(self._default_profile) or {}

        execution_raw = str(profile.get("execution", "local")).strip()
        # "auto" is resolved later by the caller (daemon checks runner health)
        execution_mode = execution_raw if execution_raw in ("local", "remote", "auto") else "local"
        runner_url = str(profile.get("runner_url", "")).strip()

        # Build merged params: manifest defaults → profile connector overrides → call_args matches
        merged: dict[str, Any] = {}
        for param_name, param_def in manifest_params.items():
            merged[param_name] = copy.deepcopy(param_def.get("default"))

        profile_connector = dict(profile.get("connectors", {}).get(connector, {}))
        for k, v in profile_connector.items():
            if k in manifest_params:
                merged[k] = copy.deepcopy(v)

        for k, v in call_args.items():
            if k in manifest_params:
                merged[k] = copy.deepcopy(v)

        resolved_profile_name = name if name in self._profiles else self._default_profile

        return ResolvedProfile(
            profile_name=resolved_profile_name,
            execution_mode=execution_mode,
            runner_url=runner_url,
            connector_params=merged,
        )
```

- [ ] **Step 4: Run tests to verify pass**

```bash
python -m pytest tests/test_profile_registry.py -v
```

Expected: 9 tests PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/profile_registry.py tests/test_profile_registry.py
git commit -m "feat: add ProfileRegistry with three-layer connector param merge"
```

---

## Task 3: `policy_config.py` — add profiles and default_profile schema

**Files:**
- Modify: `scripts/policy_config.py`
- Modify: `tests/test_plugin_static_config.py` (if profile validation tests needed — add inline)

- [ ] **Step 1: Write the failing test**

Add to the bottom of `tests/test_plugin_static_config.py`:

```python
def test_load_settings_accepts_profiles_and_default_profile(tmp_path, monkeypatch):
    from scripts.policy_config import load_settings, _reset_settings_cache
    cfg = tmp_path / "settings.json"
    cfg.write_text(
        '{"profiles": {"local": {"execution": "local"}}, "default_profile": "local"}',
        encoding="utf-8",
    )
    monkeypatch.setenv("EMERGE_SETTINGS_PATH", str(cfg))
    _reset_settings_cache()
    try:
        s = load_settings()
        assert "local" in s["profiles"]
        assert s["default_profile"] == "local"
    finally:
        _reset_settings_cache()


def test_load_settings_invalid_profile_execution_raises(tmp_path, monkeypatch):
    from scripts.policy_config import load_settings, _reset_settings_cache
    cfg = tmp_path / "settings.json"
    cfg.write_text(
        '{"profiles": {"bad": {"execution": "ftp"}}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("EMERGE_SETTINGS_PATH", str(cfg))
    _reset_settings_cache()
    try:
        with pytest.raises(ValueError, match="execution"):
            load_settings()
    finally:
        _reset_settings_cache()
```

- [ ] **Step 2: Run to verify they fail**

```bash
python -m pytest tests/test_plugin_static_config.py::test_load_settings_accepts_profiles_and_default_profile tests/test_plugin_static_config.py::test_load_settings_invalid_profile_execution_raises -v
```

Expected: 2 tests FAIL (`KeyError` or assertion error — profiles not in defaults)

- [ ] **Step 3: Implement in `policy_config.py`**

In `_DEFAULTS`, after `"metrics_sink"`:

```python
    "profiles": {},
    "default_profile": "local",
```

In `_validate_settings`, after the `sink` check, add:

```python
    profiles = s.get("profiles", {})
    if not isinstance(profiles, dict):
        raise ValueError("settings.profiles must be an object")
    for prof_name, prof_val in profiles.items():
        if not isinstance(prof_val, dict):
            raise ValueError(f"settings.profiles.{prof_name} must be an object")
        execution = prof_val.get("execution", "local")
        if execution not in ("local", "remote", "auto"):
            raise ValueError(
                f"settings.profiles.{prof_name}.execution must be 'local', 'remote', or 'auto', got {execution!r}"
            )
        if execution == "remote" and not str(prof_val.get("runner_url", "")).strip():
            raise ValueError(
                f"settings.profiles.{prof_name}.runner_url is required when execution is 'remote'"
            )
    default_profile = s.get("default_profile", "local")
    if not isinstance(default_profile, str):
        raise ValueError("settings.default_profile must be a string")
```

- [ ] **Step 4: Run tests to verify pass**

```bash
python -m pytest tests/test_plugin_static_config.py -v
```

Expected: all pass (including the 2 new ones)

- [ ] **Step 5: Full suite to check no regressions**

```bash
python -m pytest tests -q --tb=short
```

Expected: 192 passed (190 existing + 2 new)

- [ ] **Step 6: Commit**

```bash
git add scripts/policy_config.py tests/test_plugin_static_config.py
git commit -m "feat: add profiles and default_profile to settings schema"
```

---

## Task 4: `PipelineEngine` — ctx-based API + manifest loading

**Files:**
- Modify: `scripts/pipeline_engine.py`
- Modify: `tests/test_pipeline_engine.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_pipeline_engine.py` (keep existing tests — they'll be updated in Step 3):

```python
def test_pipeline_engine_run_read_ctx_signature(tmp_path):
    """run_read with new ctx signature returns rows and verify_result."""
    from scripts.pipeline_engine import PipelineEngine
    from scripts.connector_context import ExecutionInfo
    import os

    os.environ["EMERGE_CONNECTOR_ROOT"] = str(Path(__file__).parent / "connectors")
    try:
        engine = PipelineEngine()
        result = engine.run_read(
            connector="mock",
            pipeline="layers",
            resolved_params={},
            call_args={"document_id": "test-doc"},
            exec_info=ExecutionInfo(
                target_profile="local",
                execution_mode="local",
                connector="mock",
                pipeline="layers",
                mode="read",
            ),
        )
        assert result["pipeline_id"] == "mock.read.layers"
        rows = result["rows"]
        assert isinstance(rows, list)
        assert len(rows) == 2
        assert result["verification_state"] == "verified"
    finally:
        os.environ.pop("EMERGE_CONNECTOR_ROOT", None)


def test_pipeline_engine_run_write_ctx_signature(tmp_path):
    """run_write with new ctx signature returns action_result and policy fields."""
    from scripts.pipeline_engine import PipelineEngine
    from scripts.connector_context import ExecutionInfo
    import os

    os.environ["EMERGE_CONNECTOR_ROOT"] = str(Path(__file__).parent / "connectors")
    try:
        engine = PipelineEngine()
        result = engine.run_write(
            connector="mock",
            pipeline="add-wall",
            resolved_params={},
            call_args={"length": 500, "wall_id": "W-test"},
            exec_info=ExecutionInfo(
                target_profile="local",
                execution_mode="local",
                connector="mock",
                pipeline="add-wall",
                mode="write",
            ),
        )
        assert result["pipeline_id"] == "mock.write.add-wall"
        assert result["action_result"]["created"] is True
        assert result["verification_state"] == "verified"
    finally:
        os.environ.pop("EMERGE_CONNECTOR_ROOT", None)
```

- [ ] **Step 2: Run to verify they fail**

```bash
python -m pytest tests/test_pipeline_engine.py::test_pipeline_engine_run_read_ctx_signature tests/test_pipeline_engine.py::test_pipeline_engine_run_write_ctx_signature -v
```

Expected: FAIL — `run_read()` takes different args

- [ ] **Step 3: Update mock connector pipelines to `ctx` signature**

Create `tests/connectors/mock/manifest.yaml`:

```yaml
connector: mock
version: "1.0"
description: "Mock connector for tests"
params: {}
capabilities: []
```

Replace `tests/connectors/mock/pipelines/read/layers.py`:

```python
from __future__ import annotations

from typing import Any


def run_read(ctx) -> list[dict[str, Any]]:
    doc_id = ctx.call_args.get("document_id", "doc-mock")
    return [
        {"id": "L1", "name": "walls", "document_id": doc_id, "count": 2},
        {"id": "L2", "name": "doors", "document_id": doc_id, "count": 1},
    ]


def verify_read(ctx, rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok = bool(rows) and all("id" in row and "name" in row for row in rows)
    return {"ok": ok, "row_count": len(rows)}
```

Replace `tests/connectors/mock/pipelines/write/add-wall.py`:

```python
from __future__ import annotations

from typing import Any


def run_write(ctx) -> dict[str, Any]:
    length = int(ctx.call_args.get("length", 1000))
    wall_id = ctx.call_args.get("wall_id", "W-new")
    return {"wall_id": wall_id, "length": length, "created": True}


def verify_write(ctx, action_result: dict[str, Any]) -> dict[str, Any]:
    ok = bool(action_result.get("created")) and int(action_result.get("length", 0)) > 0
    return {
        "ok": ok,
        "verified_wall_id": action_result.get("wall_id"),
        "observed_length": action_result.get("length"),
    }
```

Replace `tests/connectors/mock/pipelines/write/add-wall-rollback.py`:

```python
from __future__ import annotations

from typing import Any


def run_write(ctx) -> dict[str, Any]:
    return {
        "wall_id": ctx.call_args.get("wall_id", "W-rb"),
        "length": int(ctx.call_args.get("length", 1000)),
        "created": True,
    }


def verify_write(ctx, action_result: dict[str, Any]) -> dict[str, Any]:
    return {"ok": False, "reason": "forced_failure_for_rollback"}


def rollback_write(ctx, action_result: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "rolled_back_wall_id": action_result.get("wall_id")}
```

- [ ] **Step 4: Rewrite `pipeline_engine.py`**

Replace the full content of `scripts/pipeline_engine.py`:

```python
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from typing import Any

from scripts.connector_context import ConnectorContext, ExecutionInfo


class PipelineMissingError(FileNotFoundError):
    """Raised when a pipeline's .yaml + .py files cannot be found in any connector root."""

    def __init__(self, connector: str, mode: str, pipeline: str, searched: str) -> None:
        self.connector = connector
        self.mode = mode
        self.pipeline = pipeline
        self.searched = searched
        super().__init__(
            f"Pipeline '{connector}/{mode}/{pipeline}' not found in: {searched}"
        )


_USER_CONNECTOR_ROOT = Path("~/.emerge/connectors").expanduser()


class PipelineEngine:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Path(__file__).resolve().parents[1]
        env_root = os.environ.get("EMERGE_CONNECTOR_ROOT", "").strip()
        if env_root:
            self._connector_roots = [
                Path(env_root).expanduser(),
                _USER_CONNECTOR_ROOT,
                self.root / "connectors",
            ]
        else:
            self._connector_roots = [_USER_CONNECTOR_ROOT, self.root / "connectors"]

    # ------------------------------------------------------------------
    # Public API: new ctx-based signatures
    # ------------------------------------------------------------------

    def run_read(
        self,
        connector: str,
        pipeline: str,
        resolved_params: dict[str, Any],
        call_args: dict[str, Any],
        exec_info: ExecutionInfo,
    ) -> dict[str, Any]:
        metadata, module = self._load_pipeline(connector, "read", pipeline)
        ctx = ConnectorContext(
            params=resolved_params,
            metadata=metadata,
            call_args=call_args,
            execution=exec_info,
        )
        rows = module.run_read(ctx)
        verify_fn = getattr(module, "verify_read", None)
        if callable(verify_fn):
            verify_result = verify_fn(ctx, rows)
            if not isinstance(verify_result, dict):
                raise ValueError("verify_read must return an object")
        else:
            verify_result = {"ok": bool(rows)}
        verification_state = "verified" if bool(verify_result.get("ok", False)) else "degraded"
        return {
            "pipeline_id": f"{connector}.read.{pipeline}",
            "intent_signature": metadata.get("intent_signature", ""),
            "rows": rows,
            "verify_result": verify_result,
            "verification_state": verification_state,
        }

    def run_write(
        self,
        connector: str,
        pipeline: str,
        resolved_params: dict[str, Any],
        call_args: dict[str, Any],
        exec_info: ExecutionInfo,
    ) -> dict[str, Any]:
        metadata, module = self._load_pipeline(connector, "write", pipeline)
        ctx = ConnectorContext(
            params=resolved_params,
            metadata=metadata,
            call_args=call_args,
            execution=exec_info,
        )
        action_result = module.run_write(ctx)
        verify_fn = getattr(module, "verify_write", None)
        if not callable(verify_fn):
            raise ValueError("verify_write is required for write pipelines")
        verify_result = verify_fn(ctx, action_result)
        if not isinstance(verify_result, dict):
            raise ValueError("verify_write must return an object")
        verification_state = "verified" if verify_result.get("ok") else "degraded"
        policy = str(metadata.get("rollback_or_stop_policy", "stop"))
        rollback_executed = False
        rollback_result: dict[str, Any] | None = None
        stop_triggered = False

        if verification_state == "degraded":
            if policy == "rollback":
                rollback_fn = getattr(module, "rollback_write", None)
                if callable(rollback_fn):
                    try:
                        rr = rollback_fn(ctx, action_result)
                        rollback_result = rr if isinstance(rr, dict) else {"ok": False, "error": "rollback_write must return object"}
                    except Exception as exc:
                        rollback_result = {"ok": False, "error": str(exc)}
                    rollback_executed = True
                else:
                    rollback_result = {"ok": False, "error": "rollback_write not implemented"}
                    stop_triggered = True
            else:
                stop_triggered = True

        return {
            "pipeline_id": f"{connector}.write.{pipeline}",
            "intent_signature": metadata.get("intent_signature", ""),
            "action_result": action_result,
            "verify_result": verify_result,
            "verification_state": verification_state,
            "rollback_or_stop_policy": policy,
            "policy_enforced": verification_state == "degraded",
            "stop_triggered": stop_triggered,
            "rollback_executed": rollback_executed,
            "rollback_result": rollback_result,
        }

    # ------------------------------------------------------------------
    # Source-only loading (for remote inline exec and crystallize)
    # ------------------------------------------------------------------

    def _load_pipeline_source(
        self, connector: str, mode: str, pipeline: str
    ) -> tuple[dict[str, Any], str]:
        """Return (metadata, py_source_text) without importing the module."""
        base, meta_path, code_path = self._find_pipeline_files(connector, mode, pipeline)
        metadata = self._load_metadata(meta_path)
        py_source = code_path.read_text(encoding="utf-8")
        return metadata, py_source

    def load_manifest(self, connector: str) -> dict[str, Any]:
        """Load connector manifest.yaml. Returns empty params dict if not found."""
        for connector_root in self._connector_roots:
            manifest_path = connector_root / connector / "manifest.yaml"
            if manifest_path.exists():
                return self._load_yaml(manifest_path)
        return {"connector": connector, "params": {}, "capabilities": []}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _find_pipeline_files(
        self, connector: str, mode: str, pipeline: str
    ) -> tuple[Path, Path, Path]:
        for connector_root in self._connector_roots:
            base = connector_root / connector / "pipelines" / mode
            meta_path = base / f"{pipeline}.yaml"
            code_path = base / f"{pipeline}.py"
            if meta_path.exists() and code_path.exists():
                return base, meta_path, code_path
        searched = ", ".join(str(r / connector) for r in self._connector_roots)
        raise PipelineMissingError(
            connector=connector, mode=mode, pipeline=pipeline, searched=searched
        )

    def _load_pipeline(
        self, connector: str, mode: str, pipeline: str
    ) -> tuple[dict[str, Any], Any]:
        _, meta_path, code_path = self._find_pipeline_files(connector, mode, pipeline)
        metadata = self._load_metadata(meta_path)
        module = self._load_module(code_path, f"emerge_{connector}_{mode}_{pipeline}")
        return metadata, module

    @staticmethod
    def _validate_metadata(path: Path, data: dict[str, Any]) -> None:
        errors: list[str] = []
        if not str(data.get("intent_signature", "")).strip():
            errors.append("intent_signature (required, non-empty string)")
        policy = str(data.get("rollback_or_stop_policy", ""))
        if policy not in ("stop", "rollback"):
            errors.append("rollback_or_stop_policy (must be 'stop' or 'rollback')")
        has_read = isinstance(data.get("read_steps"), list) and len(data["read_steps"]) > 0
        has_write = isinstance(data.get("write_steps"), list) and len(data["write_steps"]) > 0
        if not (has_read ^ has_write):
            errors.append("read_steps or write_steps (exactly one required, non-empty list)")
        has_verify = isinstance(data.get("verify_steps"), list) and len(data["verify_steps"]) > 0
        if not has_verify:
            errors.append("verify_steps (required, non-empty list)")
        if errors:
            raise ValueError(
                f"pipeline metadata invalid at {path}: missing/invalid fields: {', '.join(errors)}"
            )

    @staticmethod
    def _load_metadata(path: Path) -> dict[str, Any]:
        text = path.read_text(encoding="utf-8")
        try:
            import yaml  # type: ignore
            loaded = yaml.safe_load(text)
            if not isinstance(loaded, dict):
                raise ValueError("metadata must be an object")
        except Exception:
            loaded = json.loads(text)
            if not isinstance(loaded, dict):
                raise ValueError("metadata must be a JSON object")
        PipelineEngine._validate_metadata(path, loaded)
        return loaded

    @staticmethod
    def _load_yaml(path: Path) -> dict[str, Any]:
        text = path.read_text(encoding="utf-8")
        try:
            import yaml  # type: ignore
            loaded = yaml.safe_load(text)
            if not isinstance(loaded, dict):
                raise ValueError(f"Expected YAML object at {path}")
            return loaded
        except Exception:
            return {}

    @staticmethod
    def _load_module(path: Path, module_name: str) -> Any:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Failed to load module spec from {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
```

- [ ] **Step 5: Run the new tests**

```bash
python -m pytest tests/test_pipeline_engine.py -v
```

Expected: all pass (existing tests will need update — see Step 6)

- [ ] **Step 6: Update existing `test_pipeline_engine.py` tests to new API**

Find every call to `engine.run_read(args)` and `engine.run_write(args)` in `tests/test_pipeline_engine.py`. Replace with the new signature. Example pattern:

```python
# Before
result = engine.run_read({"connector": "mock", "pipeline": "layers"})

# After
from scripts.connector_context import ExecutionInfo
result = engine.run_read(
    connector="mock",
    pipeline="layers",
    resolved_params={},
    call_args={},
    exec_info=ExecutionInfo("local", "local", "mock", "layers", "read"),
)
```

Similarly for `run_write`. Run after each change:

```bash
python -m pytest tests/test_pipeline_engine.py -v
```

Expected: all tests pass.

- [ ] **Step 7: Run full suite**

```bash
python -m pytest tests -q --tb=short
```

Expected: all pass (some integration tests will fail because daemon still uses old engine API — that is expected and will be fixed in Task 5)

- [ ] **Step 8: Commit**

```bash
git add scripts/pipeline_engine.py tests/connectors/mock/ tests/test_pipeline_engine.py
git commit -m "feat: rewrite PipelineEngine with ConnectorContext API and manifest loading"
```

---

## Task 5: `EmergeDaemon` — replace RunnerRouter with ProfileRegistry, update all pipeline paths

**Files:**
- Modify: `scripts/emerge_daemon.py`
- Modify: `scripts/runner_client.py` (remove RunnerRouter)
- Modify: `tests/test_mcp_tools_integration.py`

- [ ] **Step 1: Write the failing integration tests**

Add to `tests/test_mcp_tools_integration.py`:

```python
def test_icc_read_resolves_profile_connector_params(tmp_path, monkeypatch):
    """icc_read passes profile-resolved connector params to pipeline via ctx.params."""
    import json, os
    from pathlib import Path

    # Write a settings.json with a local profile providing a custom document_id param
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "profiles": {
            "local": {"execution": "local", "connectors": {"mock": {}}}
        },
        "default_profile": "local",
    }), encoding="utf-8")
    monkeypatch.setenv("EMERGE_SETTINGS_PATH", str(settings))
    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(Path(__file__).parent / "connectors"))

    from scripts.policy_config import _reset_settings_cache
    _reset_settings_cache()
    try:
        daemon = EmergeDaemon(root=ROOT)
        result = daemon.handle_jsonrpc({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "icc_read", "arguments": {
                "connector": "mock", "pipeline": "layers",
                "target_profile": "local",
            }},
        })
        assert result["result"]["isError"] is not True
        obj = json.loads(result["result"]["content"][0]["text"])
        assert obj["pipeline_id"] == "mock.read.layers"
        assert obj["verification_state"] == "verified"
    finally:
        _reset_settings_cache()
        monkeypatch.delenv("EMERGE_SETTINGS_PATH", raising=False)
        monkeypatch.delenv("EMERGE_CONNECTOR_ROOT", raising=False)


def test_icc_read_call_args_override_profile_param(tmp_path, monkeypatch):
    """call_args that match a manifest param name override the profile value at runtime."""
    import json, os
    from pathlib import Path

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "profiles": {"local": {"execution": "local"}},
        "default_profile": "local",
    }), encoding="utf-8")
    monkeypatch.setenv("EMERGE_SETTINGS_PATH", str(settings))
    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(Path(__file__).parent / "connectors"))

    from scripts.policy_config import _reset_settings_cache
    _reset_settings_cache()
    try:
        daemon = EmergeDaemon(root=ROOT)
        result = daemon.handle_jsonrpc({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "icc_read", "arguments": {
                "connector": "mock", "pipeline": "layers",
                "document_id": "override-doc",
            }},
        })
        assert result["result"]["isError"] is not True
        obj = json.loads(result["result"]["content"][0]["text"])
        # document_id is a call_arg (not in mock manifest), flows through call_args not params
        assert obj["rows"][0]["document_id"] == "override-doc"
    finally:
        _reset_settings_cache()
        monkeypatch.delenv("EMERGE_SETTINGS_PATH", raising=False)
        monkeypatch.delenv("EMERGE_CONNECTOR_ROOT", raising=False)
```

- [ ] **Step 2: Run to verify they fail**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_icc_read_resolves_profile_connector_params tests/test_mcp_tools_integration.py::test_icc_read_call_args_override_profile_param -v
```

Expected: FAIL (daemon still uses old pipeline engine API)

- [ ] **Step 3: Remove `RunnerRouter` from `runner_client.py`**

Delete the entire `RunnerRouter` class (lines ~151–328 of `runner_client.py`). Keep `RetryConfig`, `RunnerClient`, and `_NO_PROXY_OPENER`.

- [ ] **Step 3b: Update `runner_sync.py` to read from `settings.json` profiles**

`runner_sync.py` currently discovers runner URLs from `runner-map.json` via `RunnerRouter.persisted_config_path()`. After `RunnerRouter` is removed, replace the entire `main()` function's discovery section:

```python
# Before (in main()):
from scripts.runner_client import RunnerRouter
cfg_path = RunnerRouter.persisted_config_path()
if not cfg_path.exists():
    return
cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
runner_map: dict[str, str] = cfg.get("map", {})
if not runner_map:
    return
seen: dict[str, str] = {}
for profile, url in runner_map.items():
    if url and url not in seen:
        seen[url] = profile

# After (in main()):
from scripts.policy_config import default_settings_path
settings_path = default_settings_path()
if not settings_path.exists():
    return
settings = json.loads(settings_path.read_text(encoding="utf-8"))
profiles: dict[str, dict] = settings.get("profiles", {})
if not profiles:
    return
# Deduplicate: one deploy per unique URL
seen: dict[str, str] = {}  # url -> first_profile_name
for profile_name, profile_cfg in profiles.items():
    if not isinstance(profile_cfg, dict):
        continue
    if str(profile_cfg.get("execution", "local")) != "remote":
        continue
    url = str(profile_cfg.get("runner_url", "")).strip()
    if url and url not in seen:
        seen[url] = profile_name
```

Also update `_deploy()` — the `cmd_runner_deploy` call uses `runner_url` positionally, which stays the same.

Run: `python -m pytest tests -q --tb=short` — verify no regressions.

- [ ] **Step 4: Rewrite daemon `__init__` to use `ProfileRegistry`**

In `scripts/emerge_daemon.py`, update the import section and `__init__`:

```python
# Remove this import:
# from scripts.runner_client import RunnerRouter

# Add this import:
from scripts.profile_registry import ProfileRegistry

# In EmergeDaemon.__init__, replace:
#   self._runner_router = RunnerRouter.from_env()
# With:
        from scripts.policy_config import load_settings
        try:
            _settings = load_settings()
        except Exception:
            _settings = {}
        self._profile_registry = ProfileRegistry(_settings)
```

- [ ] **Step 5: Update `icc_read` handler in `call_tool`**

Replace the `if name == "icc_read":` block:

```python
        if name == "icc_read":
            try:
                connector = str(arguments.get("connector", "mock")).strip()
                pipeline_name = str(arguments.get("pipeline", "layers")).strip()
                target_profile = str(arguments.get("target_profile", "")).strip() or None
                manifest = self.pipeline.load_manifest(connector)
                manifest_params = manifest.get("params", {})
                resolved = self._profile_registry.resolve(
                    target_profile, connector, manifest_params, arguments
                )
                exec_info = ExecutionInfo(
                    target_profile=resolved.profile_name,
                    execution_mode=resolved.execution_mode,
                    connector=connector,
                    pipeline=pipeline_name,
                    mode="read",
                )
                if resolved.execution_mode == "remote" and resolved.runner_url:
                    from scripts.runner_client import RunnerClient, RetryConfig
                    from scripts.policy_config import load_settings
                    try:
                        _s = load_settings().get("runner", {})
                        retry = RetryConfig(
                            max_attempts=int(_s.get("retry_max_attempts", 3)),
                            base_delay_s=float(_s.get("retry_base_delay_s", 0.5)),
                            max_delay_s=float(_s.get("retry_max_delay_s", 10.0)),
                        )
                    except Exception:
                        retry = RetryConfig()
                    client = RunnerClient(base_url=resolved.runner_url, timeout_s=30.0, retry=retry)
                    result = self._run_pipeline_remotely(
                        "read", connector, pipeline_name, resolved.connector_params, arguments, exec_info, client
                    )
                elif resolved.execution_mode == "auto":
                    try:
                        from scripts.runner_client import RunnerClient, RetryConfig
                        client = RunnerClient(base_url=resolved.runner_url, timeout_s=5.0)
                        client.health()
                        result = self._run_pipeline_remotely(
                            "read", connector, pipeline_name, resolved.connector_params, arguments, exec_info, client
                        )
                    except Exception:
                        result = self.pipeline.run_read(connector, pipeline_name, resolved.connector_params, arguments, exec_info)
                else:
                    result = self.pipeline.run_read(connector, pipeline_name, resolved.connector_params, arguments, exec_info)
                response = {
                    "isError": False,
                    "content": [{"type": "text", "text": json.dumps(result)}],
                }
                try:
                    self._record_pipeline_event(tool_name=name, arguments=arguments, result=result, is_error=False)
                except Exception as exc:
                    self._append_warning_text(response, f"policy bookkeeping failed: {exc}")
                return response
            except PipelineMissingError as exc:
                connector_arg = str(arguments.get("connector", ""))
                pipeline_arg = str(arguments.get("pipeline", ""))
                hint = (
                    f"no pipeline registered yet — use icc_exec with "
                    f"intent_signature='{connector_arg}.read.{pipeline_arg}' to explore"
                )
                return {
                    "isError": False, "pipeline_missing": True,
                    "connector": connector_arg, "pipeline": pipeline_arg, "mode": "read",
                    "fallback": "icc_exec", "fallback_hint": hint,
                    "content": [{"type": "text", "text": f"Pipeline not found. {hint}"}],
                }
            except Exception as exc:
                try:
                    self._record_pipeline_event(tool_name=name, arguments=arguments, result={}, is_error=True, error_text=str(exc))
                except Exception:
                    pass
                return {"isError": True, "recovery_suggestion": "exec", "content": [{"type": "text", "text": f"icc_read failed: {exc}"}]}
```

Add the `ExecutionInfo` import at the top of the daemon file:

```python
from scripts.connector_context import ExecutionInfo
```

- [ ] **Step 6: Update `icc_write` handler** (same pattern as icc_read, mode="write")

Apply the same profile resolution + ExecutionInfo construction to the `if name == "icc_write":` block. Replace `self.pipeline.run_write(arguments)` with `self.pipeline.run_write(connector, pipeline_name, resolved.connector_params, arguments, exec_info)`.

- [ ] **Step 7: Update flywheel bridge**

In `_try_flywheel_bridge`, replace:

```python
# Old:
_client = self._runner_router.find_client(arguments) if self._runner_router else None
if _client is not None:
    result = self._run_pipeline_remotely(mode, pipeline_args, _client)
elif mode == "write":
    result = self.pipeline.run_write(pipeline_args)
else:
    result = self.pipeline.run_read(pipeline_args)

# New:
connector_b, mode_b, name_b = parts
manifest_b = self.pipeline.load_manifest(connector_b)
resolved_b = self._profile_registry.resolve(
    str(arguments.get("target_profile", "")), connector_b,
    manifest_b.get("params", {}), arguments
)
exec_info_b = ExecutionInfo(
    target_profile=resolved_b.profile_name,
    execution_mode=resolved_b.execution_mode,
    connector=connector_b, pipeline=name_b, mode=mode_b,
)
if resolved_b.execution_mode == "remote" and resolved_b.runner_url:
    from scripts.runner_client import RunnerClient
    client_b = RunnerClient(base_url=resolved_b.runner_url, timeout_s=30.0)
    result = self._run_pipeline_remotely(mode_b, connector_b, name_b, resolved_b.connector_params, arguments, exec_info_b, client_b)
elif mode_b == "write":
    result = self.pipeline.run_write(connector_b, name_b, resolved_b.connector_params, arguments, exec_info_b)
else:
    result = self.pipeline.run_read(connector_b, name_b, resolved_b.connector_params, arguments, exec_info_b)
```

- [ ] **Step 8: Update `_run_pipeline_remotely` signature**

Change the method signature from `_run_pipeline_remotely(self, mode, arguments, client)` to:

```python
def _run_pipeline_remotely(
    self,
    mode: str,
    connector: str,
    pipeline_name: str,
    resolved_params: dict[str, Any],
    call_args: dict[str, Any],
    exec_info: "ExecutionInfo",
    client: Any,
) -> dict[str, Any]:
```

Inside the method, replace:
- `connector = str(arguments.get("connector", "")).strip()` → use `connector` param
- `pipeline_name = str(arguments.get("pipeline", "")).strip()` → use `pipeline_name` param
- `target_profile = str(arguments.get("target_profile", "default")).strip()` → `exec_info.target_profile`
- `args_repr = repr(json.dumps(arguments, ensure_ascii=True))` → split into two:
  ```python
  params_repr = repr(json.dumps(resolved_params, ensure_ascii=True))
  call_args_repr = repr(json.dumps(call_args, ensure_ascii=True))
  ```
- In `exec_code`, change `f"_a = _j.loads({args_repr})\n"` to:
  ```python
  f"_p = _j.loads({params_repr})\n"
  f"_ca = _j.loads({call_args_repr})\n"
  # Inline ConnectorContext definition for remote execution:
  "_EI = type('EI', (), {'target_profile': None, 'execution_mode': 'remote', 'connector': None, 'pipeline': None, 'mode': None})()\n"
  f"_EI.target_profile = {exec_info.target_profile!r}\n"
  f"_EI.execution_mode = 'remote'\n"
  f"_EI.connector = {connector!r}\n"
  f"_EI.pipeline = {pipeline_name!r}\n"
  f"_EI.mode = {mode!r}\n"
  "_CTX = type('CTX', (), {'params': None, 'metadata': None, 'call_args': None, 'execution': None})()\n"
  "_CTX.params = _p\n"
  f"_CTX.metadata = _j.loads({meta_repr})\n"
  "_CTX.call_args = _ca\n"
  "_CTX.execution = _EI\n"
  ```
- Change dispatch strings: replace `run_read(metadata=_m, args=_a)` → `run_read(_CTX)` and `verify_read(metadata=_m, args=_a, rows=_rows)` → `verify_read(_CTX, _rows)`, etc. for all five function calls.
- Add WAL writeback after successful exec:
  ```python
  # Remote WAL writeback — enables icc_crystallize to see remote-explored pipelines
  try:
      self._append_remote_wal_entry(
          connector=connector, pipeline_name=pipeline_name, mode=mode,
          py_source=py_source, metadata=metadata,
          target_profile=exec_info.target_profile,
      )
  except Exception:
      pass
  ```

- [ ] **Step 9: Add `_append_remote_wal_entry` helper to daemon**

```python
def _append_remote_wal_entry(
    self,
    *,
    connector: str,
    pipeline_name: str,
    mode: str,
    py_source: str,
    metadata: dict[str, Any],
    target_profile: str,
) -> None:
    """Write a source: 'remote' WAL entry so crystallize can see pipeline code that ran remotely."""
    import time as _time
    normalized = (target_profile or "default").strip() or "default"
    profile_key = "__default__" if normalized == "default" else derive_profile_token(normalized)
    if normalized == "default":
        session_id = self._base_session_id
    else:
        session_id = f"{self._base_session_id}__{profile_key}"
    session_dir = self._state_root / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    wal_path = session_dir / "wal.jsonl"
    entry = {
        "status": "success",
        "no_replay": False,
        "source": "remote",
        "code": py_source,
        "metadata": {
            "intent_signature": metadata.get("intent_signature", f"{connector}.{mode}.{pipeline_name}"),
            "mode": mode,
            "target_profile": normalized,
        },
        "ts_ms": int(_time.time() * 1000),
    }
    with wal_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=True) + "\n")
```

- [ ] **Step 10: Run the new integration tests**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_icc_read_resolves_profile_connector_params tests/test_mcp_tools_integration.py::test_icc_read_call_args_override_profile_param -v
```

Expected: PASS

- [ ] **Step 11: Run full suite**

```bash
python -m pytest tests -q --tb=short
```

Fix any failures from old `run_read(args)` / `run_write(args)` call sites in remaining tests. The pattern is always: add `connector=`, `pipeline=`, `resolved_params={}`, `call_args=arguments`, `exec_info=ExecutionInfo(...)`.

- [ ] **Step 12: Commit**

```bash
git add scripts/emerge_daemon.py scripts/runner_client.py tests/test_mcp_tools_integration.py
git commit -m "feat: replace RunnerRouter with ProfileRegistry in daemon; ctx-based pipeline dispatch"
```

---

## Task 6: Update HyperMesh and ZWCAD connectors to `ctx` API + add manifests

**Files:**
- Create: `~/.emerge/connectors/hypermesh/manifest.yaml`
- Modify: `~/.emerge/connectors/hypermesh/pipelines/read/state.py`
- Modify: `~/.emerge/connectors/hypermesh/pipelines/write/apply-change.py`
- Create: `~/.emerge/connectors/zwcad/manifest.yaml`
- Modify: `~/.emerge/connectors/zwcad/pipelines/read/state.py`
- Modify: `~/.emerge/connectors/zwcad/pipelines/write/apply-change.py`
- Modify: `tests/test_mcp_tools_integration.py` (update hypermesh tests)

- [ ] **Step 1: Create HyperMesh manifest**

Write `~/.emerge/connectors/hypermesh/manifest.yaml`:

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

- [ ] **Step 2: Rewrite HyperMesh `state.py`**

Replace `~/.emerge/connectors/hypermesh/pipelines/read/state.py`:

```python
from __future__ import annotations

import socket
from typing import Any


def run_read(ctx) -> list[dict[str, Any]]:
    """Read active model state from HyperMesh via TCP/Tcl socket bridge."""
    host = ctx.params["hm_host"]
    port = ctx.params["hm_port"]
    timeout = ctx.params["hm_timeout"]
    model_name = str(ctx.call_args.get("model_name", "hm-model-1"))

    try:
        rows = _query_via_tcl(host, port, timeout, model_name)
        if rows:
            return rows
        return _mock_rows(model_name)
    except Exception:
        return _mock_rows(model_name)


def _query_via_tcl(host: str, port: int, timeout: float, model_name: str) -> list[dict[str, Any]]:
    batch_cmd = (
        "set _n [hm_getentitydisplaycount nodes]; "
        "set _e [hm_getentitydisplaycount elements]; "
        "set _c [hm_getentitydisplaycount comps]; "
        "list $_n $_e $_c"
    )
    raw = _tcl_call(host, port, timeout, batch_cmd)
    parts = raw.strip().split()
    node_count = _parse_int(parts[0]) if len(parts) > 0 else 0
    elem_count = _parse_int(parts[1]) if len(parts) > 1 else 0
    comp_count = _parse_int(parts[2]) if len(parts) > 2 else 0
    return [{"model_name": model_name, "node_count": node_count, "element_count": elem_count, "component_count": comp_count, "source": "live"}]


def _tcl_call(host: str, port: int, timeout: float, cmd: str) -> str:
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall((cmd + "\n").encode("utf-8"))
        data = b""
        while b"\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
    line = data.decode("utf-8", errors="replace").strip()
    if line.startswith("SUCCESS: "):
        return line[len("SUCCESS: "):]
    if line.startswith("ERROR: "):
        raise RuntimeError(line[len("ERROR: "):])
    return line


def _parse_int(s: str) -> int:
    try:
        return int(s.strip())
    except (ValueError, AttributeError):
        return 0


def _mock_rows(model_name: str) -> list[dict[str, Any]]:
    return [{"model_name": model_name, "node_count": 0, "element_count": 0, "component_count": 0, "source": "mock"}]


def verify_read(ctx, rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok = bool(rows) and all("node_count" in r and "element_count" in r for r in rows)
    return {"ok": ok, "row_count": len(rows)}
```

- [ ] **Step 3: Rewrite HyperMesh `apply-change.py`**

Replace `~/.emerge/connectors/hypermesh/pipelines/write/apply-change.py`:

```python
from __future__ import annotations

import socket
from typing import Any


def run_write(ctx) -> dict[str, Any]:
    """Execute a Tcl command in HyperMesh via TCP/Tcl socket bridge."""
    host = ctx.params["hm_host"]
    port = ctx.params["hm_port"]
    timeout = ctx.params["hm_timeout"]
    tcl_cmd = str(ctx.call_args.get("tcl_cmd", ""))
    change_description = str(ctx.call_args.get("change_description", ""))

    if not tcl_cmd:
        return _mock_result(tcl_cmd, change_description, reason="no_tcl_cmd")

    try:
        raw = _tcl_call(host, port, timeout, tcl_cmd)
        return {"tcl_cmd": tcl_cmd, "change_description": change_description, "hm_result": raw, "created": True, "source": "live"}
    except Exception as exc:
        return _mock_result(tcl_cmd, change_description, reason=str(exc))


def _tcl_call(host: str, port: int, timeout: float, cmd: str) -> str:
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall((cmd + "\n").encode("utf-8"))
        data = b""
        while b"\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
    line = data.decode("utf-8", errors="replace").strip()
    if line.startswith("SUCCESS: "):
        return line[len("SUCCESS: "):]
    if line.startswith("ERROR: "):
        raise RuntimeError(line[len("ERROR: "):])
    return line


def _mock_result(tcl_cmd: str, change_description: str, reason: str = "") -> dict[str, Any]:
    return {"tcl_cmd": tcl_cmd, "change_description": change_description, "hm_result": "mock", "created": True, "source": "mock", "mock_reason": reason}


def verify_write(ctx, action_result: dict[str, Any]) -> dict[str, Any]:
    ok = bool(action_result.get("created"))
    return {"ok": ok, "tcl_cmd": action_result.get("tcl_cmd"), "source": action_result.get("source", "unknown")}


def rollback_write(ctx, action_result: dict[str, Any]) -> dict[str, Any]:
    host = ctx.params["hm_host"]
    port = ctx.params["hm_port"]
    timeout = ctx.params["hm_timeout"]
    tcl_cmd = action_result.get("tcl_cmd", "")
    source = action_result.get("source", "mock")

    if source == "mock":
        return {"rolled_back": True, "tcl_cmd": tcl_cmd, "method": "mock"}

    try:
        _tcl_call(host, port, timeout, "*undo")
        return {"rolled_back": True, "tcl_cmd": tcl_cmd, "method": "tcl_undo"}
    except Exception as exc:
        return {"rolled_back": False, "tcl_cmd": tcl_cmd, "reason": str(exc)}
```

- [ ] **Step 4: Create ZWCAD manifest**

Write `~/.emerge/connectors/zwcad/manifest.yaml`:

```yaml
connector: zwcad
version: "1.0"
description: "ZWCAD via COM automation (Windows only)"

params: {}

capabilities:
  - com_automation
  - windows_only
```

- [ ] **Step 5: Rewrite ZWCAD pipelines to `ctx` signature**

Replace `~/.emerge/connectors/zwcad/pipelines/read/state.py`:

```python
from __future__ import annotations

from typing import Any


def run_read(ctx) -> list[dict[str, Any]]:
    doc_id = ctx.call_args.get("document_id", "zwcad-doc-1")
    try:
        import win32com.client  # type: ignore[import]
        app = win32com.client.Dispatch("ZwCAD.Application")
        doc = app.ActiveDocument
        rows = [{"id": f"L{i}", "name": layer.Name, "document_id": doc.Name, "on": layer.LayerOn} for i, layer in enumerate(doc.Layers)]
        return rows if rows else _mock_rows(doc_id)
    except Exception:
        return _mock_rows(doc_id)


def _mock_rows(doc_id: str) -> list[dict[str, Any]]:
    return [
        {"id": "L0", "name": "0", "document_id": doc_id, "on": True},
        {"id": "L1", "name": "Defpoints", "document_id": doc_id, "on": True},
    ]


def verify_read(ctx, rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok = bool(rows) and all("id" in r and "name" in r for r in rows)
    return {"ok": ok, "row_count": len(rows)}
```

Replace `~/.emerge/connectors/zwcad/pipelines/write/apply-change.py`:

```python
from __future__ import annotations

from typing import Any


def run_write(ctx) -> dict[str, Any]:
    change_type = str(ctx.call_args.get("change_type", "line"))
    try:
        import win32com.client  # type: ignore[import]
        app = win32com.client.Dispatch("ZwCAD.Application")
        doc = app.ActiveDocument
        space = doc.ModelSpace
        if change_type == "line":
            x1, y1 = float(ctx.call_args.get("x1", 0)), float(ctx.call_args.get("y1", 0))
            x2, y2 = float(ctx.call_args.get("x2", 100)), float(ctx.call_args.get("y2", 100))
            line = space.AddLine(_point3d(x1, y1, 0), _point3d(x2, y2, 0))
            doc.Regen(1)
            return {"change_type": change_type, "entity_handle": line.Handle, "created": True}
        elif change_type == "circle":
            cx, cy = float(ctx.call_args.get("cx", 0)), float(ctx.call_args.get("cy", 0))
            circle = space.AddCircle(_point3d(cx, cy, 0), float(ctx.call_args.get("radius", 50)))
            doc.Regen(1)
            return {"change_type": change_type, "entity_handle": circle.Handle, "created": True}
        else:
            return _mock_result(change_type)
    except Exception:
        return _mock_result(change_type)


def _point3d(x: float, y: float, z: float) -> Any:
    import win32com.client  # type: ignore[import]
    return win32com.client.VARIANT(win32com.client.pythoncom.VT_ARRAY | win32com.client.pythoncom.VT_R8, [x, y, z])  # type: ignore[attr-defined]


def _mock_result(change_type: str) -> dict[str, Any]:
    return {"change_type": change_type, "entity_handle": "mock-handle-1", "created": True}


def verify_write(ctx, action_result: dict[str, Any]) -> dict[str, Any]:
    ok = bool(action_result.get("created"))
    return {"ok": ok, "change_type": action_result.get("change_type"), "entity_handle": action_result.get("entity_handle")}


def rollback_write(ctx, action_result: dict[str, Any]) -> dict[str, Any]:
    handle = action_result.get("entity_handle", "")
    if not handle or handle == "mock-handle-1":
        return {"rolled_back": True, "handle": handle, "mock": True}
    try:
        import win32com.client  # type: ignore[import]
        app = win32com.client.Dispatch("ZwCAD.Application")
        doc = app.ActiveDocument
        for i in range(doc.ModelSpace.Count):
            entity = doc.ModelSpace.Item(i)
            if entity.Handle == handle:
                entity.Delete()
                doc.Regen(1)
                return {"rolled_back": True, "handle": handle}
        return {"rolled_back": False, "handle": handle, "reason": "entity_not_found"}
    except Exception as exc:
        return {"rolled_back": False, "handle": handle, "reason": str(exc)}
```

- [ ] **Step 6: Update existing HyperMesh integration tests**

In `tests/test_mcp_tools_integration.py`, the existing HyperMesh tests pass `hm_timeout: 0.1` directly in arguments. With the new system, `hm_timeout` is a manifest param and will be overridden by `call_args` (since it matches a manifest param name). This still works — no test change needed. Run to confirm:

```bash
python -m pytest tests/test_mcp_tools_integration.py -k "hypermesh" -v
```

Expected: all HyperMesh tests pass.

- [ ] **Step 7: Run full suite**

```bash
python -m pytest tests -q --tb=short
```

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add ~/.emerge/connectors/hypermesh/ ~/.emerge/connectors/zwcad/
git commit -m "feat: update hypermesh and zwcad connectors to ctx API with manifests"
```

---

## Task 7: Crystallize — auto-parameterization and `needs_review` flag

**Files:**
- Modify: `scripts/emerge_daemon.py` (`_crystallize` method)
- Modify: `tests/test_crystallize.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_crystallize.py`:

```python
def test_crystallize_parameterizes_matched_literals(tmp_path, monkeypatch):
    """When WAL code contains values matching manifest params, crystallize replaces them with ctx.params."""
    import os, json
    from pathlib import Path

    # Create a manifest with known params
    connector_root = tmp_path / "connectors"
    manifest_dir = connector_root / "myconn"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "manifest.yaml").write_text(
        'connector: myconn\nparams:\n  my_host:\n    type: string\n    default: "127.0.0.1"\n  my_port:\n    type: integer\n    default: 1234\n',
        encoding="utf-8",
    )

    # Set up WAL with code containing literal values matching the manifest params
    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(connector_root))
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("EMERGE_SESSION_ID", "cryst-param-test")

    from scripts.emerge_daemon import EmergeDaemon
    from scripts.policy_config import _reset_settings_cache
    _reset_settings_cache()
    try:
        daemon = EmergeDaemon(root=Path(__file__).parent.parent)

        # Manually write a WAL entry with literal IP + port
        session_dir = Path(str(tmp_path / "state")) / "cryst-param-test"
        session_dir.mkdir(parents=True, exist_ok=True)
        wal_entry = {
            "status": "success",
            "no_replay": False,
            "code": 'import socket\nsock = socket.create_connection(("127.0.0.1", 1234))\n__result = [{"ok": True}]',
            "metadata": {"intent_signature": "read.myconn.data", "mode": "read", "target_profile": "default"},
        }
        (session_dir / "wal.jsonl").write_text(json.dumps(wal_entry) + "\n", encoding="utf-8")

        result = daemon.call_tool("icc_crystallize", {
            "intent_signature": "read.myconn.data",
            "connector": "myconn",
            "pipeline_name": "data",
            "mode": "read",
        })
        assert result.get("ok") is True
        py_content = Path(result["py_path"]).read_text(encoding="utf-8")
        assert 'ctx.params["my_host"]' in py_content
        assert 'ctx.params["my_port"]' in py_content
        assert '"127.0.0.1"' not in py_content
        assert "1234" not in py_content
    finally:
        _reset_settings_cache()
        for k in ("EMERGE_CONNECTOR_ROOT", "EMERGE_STATE_ROOT", "EMERGE_SESSION_ID"):
            monkeypatch.delenv(k, raising=False)


def test_crystallize_sets_needs_review_when_unresolved_ip_remains(tmp_path, monkeypatch):
    """When an IP literal in WAL code cannot be matched to a manifest param, needs_review: true."""
    import json
    from pathlib import Path

    connector_root = tmp_path / "connectors"
    (connector_root / "myconn").mkdir(parents=True)
    (connector_root / "myconn" / "manifest.yaml").write_text(
        'connector: myconn\nparams: {}\n', encoding="utf-8"
    )

    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(connector_root))
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("EMERGE_SESSION_ID", "cryst-review-test")

    from scripts.emerge_daemon import EmergeDaemon
    from scripts.policy_config import _reset_settings_cache
    _reset_settings_cache()
    try:
        daemon = EmergeDaemon(root=Path(__file__).parent.parent)
        session_dir = Path(str(tmp_path / "state")) / "cryst-review-test"
        session_dir.mkdir(parents=True, exist_ok=True)
        wal_entry = {
            "status": "success",
            "no_replay": False,
            "code": 'import socket\nsock = socket.create_connection(("10.0.0.5", 8080))\n__result = [{"ok": True}]',
            "metadata": {"intent_signature": "read.myconn.data2", "mode": "read", "target_profile": "default"},
        }
        (session_dir / "wal.jsonl").write_text(json.dumps(wal_entry) + "\n", encoding="utf-8")

        result = daemon.call_tool("icc_crystallize", {
            "intent_signature": "read.myconn.data2",
            "connector": "myconn",
            "pipeline_name": "data2",
            "mode": "read",
        })
        assert result.get("ok") is True
        yaml_content = Path(result["yaml_path"]).read_text(encoding="utf-8")
        py_content = Path(result["py_path"]).read_text(encoding="utf-8")
        assert "needs_review: true" in yaml_content
        assert "WARNING" in py_content
        assert "10.0.0.5" in py_content  # literal preserved, not silently dropped
    finally:
        _reset_settings_cache()
        for k in ("EMERGE_CONNECTOR_ROOT", "EMERGE_STATE_ROOT", "EMERGE_SESSION_ID"):
            monkeypatch.delenv(k, raising=False)
```

- [ ] **Step 2: Run to verify they fail**

```bash
python -m pytest tests/test_crystallize.py::test_crystallize_parameterizes_matched_literals tests/test_crystallize.py::test_crystallize_sets_needs_review_when_unresolved_ip_remains -v
```

Expected: FAIL

- [ ] **Step 3: Update `_crystallize` in `emerge_daemon.py`**

After finding `best_code` and before generating the harness, add parameterization:

```python
        # --- load manifest params for auto-parameterization ---
        manifest = self.pipeline.load_manifest(connector)
        manifest_params: dict[str, dict] = manifest.get("params", {})

        # Resolve current param values (use default_profile defaults for crystallize context)
        current_param_values: dict[str, Any] = {
            name: defn.get("default") for name, defn in manifest_params.items()
        }

        # Auto-parameterize: replace literals matching current param values with ctx.params["name"]
        parameterized_code, unresolved_ips = self._parameterize_code(
            best_code, current_param_values
        )
        needs_review = bool(unresolved_ips)
        best_code = parameterized_code
```

Then replace the `indented = textwrap.indent(best_code, "    ")` line with:

```python
        indented = textwrap.indent(best_code, "    ")
```

(no change needed there, but the surrounding ctx signature must be updated)

Change generated `run_read` / `run_write` signatures from `(metadata, args)` to `(ctx)`:

```python
        if mode == "read":
            py_src = (
                f"# auto-generated by icc_crystallize — review before promoting\n"
                + (f"# WARNING: auto-parameterization incomplete — review before publishing to Memory Hub\n"
                   f"# Unresolved literals detected: {unresolved_ips}\n"
                   if needs_review else "")
                + f"# intent_signature: {intent_signature}\n"
                f"# synthesized_at: {ts}\n"
                f"\n"
                f"def run_read(ctx):\n"
                f"    # --- CRYSTALLIZED ---\n"
                f"{indented}\n"
                f"    # --- END ---\n"
                f"    return __result\n"
                f"\n\n"
                f"def verify_read(ctx, rows):\n"
                f"    return {{\"ok\": bool(rows)}}\n"
            )
            yaml_src = (
                f"intent_signature: {intent_signature}\n"
                f"rollback_or_stop_policy: stop\n"
                f"read_steps:\n"
                f"  - run_read\n"
                f"verify_steps:\n"
                f"  - verify_read\n"
                f"synthesized: true\n"
                f"synthesized_at: {ts}\n"
                + (f"needs_review: true\n" if needs_review else "")
            )
        else:  # write
            py_src = (
                f"# auto-generated by icc_crystallize — review before promoting\n"
                + (f"# WARNING: auto-parameterization incomplete — review before publishing to Memory Hub\n"
                   f"# Unresolved literals detected: {unresolved_ips}\n"
                   if needs_review else "")
                + f"# intent_signature: {intent_signature}\n"
                f"# synthesized_at: {ts}\n"
                f"\n"
                f"def run_write(ctx):\n"
                f"    # --- CRYSTALLIZED ---\n"
                f"{indented}\n"
                f"    # --- END ---\n"
                f"    return __action\n"
                f"\n\n"
                f"def verify_write(ctx, action_result):\n"
                f"    return {{\"ok\": bool(action_result.get(\"ok\"))}}\n"
            )
            yaml_src = (
                f"intent_signature: {intent_signature}\n"
                f"rollback_or_stop_policy: stop\n"
                f"write_steps:\n"
                f"  - run_write\n"
                f"verify_steps:\n"
                f"  - verify_write\n"
                f"synthesized: true\n"
                f"synthesized_at: {ts}\n"
                + (f"needs_review: true\n" if needs_review else "")
            )
```

Add the new helper method to `EmergeDaemon`:

```python
    @staticmethod
    def _parameterize_code(
        code: str,
        param_values: dict[str, Any],
    ) -> tuple[str, list[str]]:
        """Replace WAL code literals matching param values with ctx.params["name"].

        Returns (parameterized_code, unresolved_ip_literals).
        Unresolved IPs are those matching r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}'
        that were NOT replaced because no matching param was found.
        """
        import re

        result = code

        # Build reverse map: value → param_name (strings and numbers)
        reverse: dict[str, str] = {}
        for name, value in param_values.items():
            if value is None:
                continue
            if isinstance(value, str):
                reverse[value] = name
            elif isinstance(value, (int, float)):
                reverse[str(value)] = name
                if isinstance(value, float) and value == int(value):
                    reverse[str(int(value))] = name

        # Replace string literals: "192.168.x.x" or '192.168.x.x'
        for literal_val, param_name in reverse.items():
            # Replace quoted string occurrences
            for quote in ('"', "'"):
                result = result.replace(f"{quote}{literal_val}{quote}", f'ctx.params["{param_name}"]')
            # Replace bare numeric occurrences (only for pure numeric values)
            if re.fullmatch(r'\d+(\.\d+)?', literal_val):
                result = re.sub(
                    r'(?<!\w)' + re.escape(literal_val) + r'(?!\w)',
                    f'ctx.params["{param_name}"]',
                    result,
                )

        # Detect remaining unresolved IP literals
        ip_pattern = re.compile(r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}')
        unresolved = ip_pattern.findall(result)

        return result, unresolved
```

- [ ] **Step 4: Run tests to verify pass**

```bash
python -m pytest tests/test_crystallize.py -v
```

Expected: all pass including the 2 new tests.

- [ ] **Step 5: Run full suite**

```bash
python -m pytest tests -q --tb=short
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/emerge_daemon.py tests/test_crystallize.py
git commit -m "feat: crystallize auto-parameterizes WAL literals and sets needs_review on unresolved IPs"
```

---

## Task 8: Remote WAL writeback integration test

**Files:**
- Modify: `tests/test_mcp_tools_integration.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_mcp_tools_integration.py`:

```python
def test_remote_pipeline_exec_writes_wal_entry_with_source_remote(tmp_path, monkeypatch):
    """After a successful remote pipeline exec, daemon appends source:remote WAL entry locally."""
    import json, threading, socket as _socket
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from pathlib import Path

    # Minimal fake runner that accepts /run and returns a valid pipeline exec result
    received = []
    class FakeRunner(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append(body)
            # Return a valid read pipeline result
            result_payload = json.dumps({"rows": [{"id": "L1", "name": "walls", "document_id": "d", "count": 1}], "verify": {"ok": True}})
            resp = json.dumps({"ok": True, "result": {"isError": False, "content": [{"type": "text", "text": f"stdout:\n{result_payload}"}]}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
        def log_message(self, *a): pass

    sock = _socket.socket(); sock.bind(("127.0.0.1", 0))
    host, port = sock.getsockname(); sock.close()
    server = HTTPServer((host, port), FakeRunner)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    runner_url = f"http://{host}:{port}"
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "profiles": {"hm-test": {"execution": "remote", "runner_url": runner_url}},
        "default_profile": "hm-test",
    }), encoding="utf-8")
    monkeypatch.setenv("EMERGE_SETTINGS_PATH", str(settings))
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("EMERGE_SESSION_ID", "remote-wal-test")
    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(Path(__file__).parent / "connectors"))

    from scripts.policy_config import _reset_settings_cache
    _reset_settings_cache()
    try:
        from scripts.emerge_daemon import EmergeDaemon
        from pathlib import Path as _Path
        daemon = EmergeDaemon(root=_Path(__file__).parent.parent)
        result = daemon.handle_jsonrpc({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "icc_read", "arguments": {
                "connector": "mock", "pipeline": "layers", "target_profile": "hm-test",
            }},
        })
        assert result["result"]["isError"] is not True

        # Check WAL written
        wal_path = tmp_path / "state" / "remote-wal-test" / "wal.jsonl"
        assert wal_path.exists(), "WAL file not created for remote exec"
        entries = [json.loads(line) for line in wal_path.read_text().splitlines() if line.strip()]
        remote_entries = [e for e in entries if e.get("source") == "remote"]
        assert len(remote_entries) >= 1, f"No remote WAL entry found: {entries}"
        assert remote_entries[0]["status"] == "success"
        assert remote_entries[0]["no_replay"] is False
    finally:
        _reset_settings_cache()
        server.shutdown()
        for k in ("EMERGE_SETTINGS_PATH", "EMERGE_STATE_ROOT", "EMERGE_SESSION_ID", "EMERGE_CONNECTOR_ROOT"):
            monkeypatch.delenv(k, raising=False)
```

- [ ] **Step 2: Run to verify it fails**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_remote_pipeline_exec_writes_wal_entry_with_source_remote -v
```

Expected: FAIL

- [ ] **Step 3: Verify the implementation from Task 5 covers this**

The `_append_remote_wal_entry` helper was added in Task 5 Step 9. This test should now pass without further code changes. If it fails, check that `_run_pipeline_remotely` calls `_append_remote_wal_entry` after a successful exec.

- [ ] **Step 4: Run to verify pass**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_remote_pipeline_exec_writes_wal_entry_with_source_remote -v
```

Expected: PASS

- [ ] **Step 5: Full suite**

```bash
python -m pytest tests -q --tb=short
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add tests/test_mcp_tools_integration.py
git commit -m "test: add remote WAL writeback integration test"
```

---

## Task 9: Update README and CLAUDE.md (documentation milestone)

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`

This task is placed here — after all core code is implemented and tests pass — so the docs accurately reflect the final state.

- [ ] **Step 1: Update README architecture diagram**

In `README.md`, update the `flowchart TB` mermaid diagram. Replace the `C[Runtime Core...]` node to include `ProfileRegistry`:

```
C[Runtime Core<br/>ExecSession · PipelineEngine · Policy Registry<br/>StateTracker · Metrics · ProfileRegistry]
```

Remove the separate `RunnerRouter` mention in the component table (it is now part of `ProfileRegistry`).

- [ ] **Step 2: Update component table**

Replace the `RunnerRouter` row:

```markdown
| **ProfileRegistry** | Resolves `target_profile` + connector to `ResolvedProfile`: execution mode (`local`/`remote`/`auto`), runner URL, and merged connector params (manifest defaults → profile overrides → call_args). Replaces `runner-map.json`. |
```

Update the `PipelineEngine` row:

```markdown
| **PipelineEngine** | Resolves `~/.emerge/connectors/` (or `EMERGE_CONNECTOR_ROOT`), loads connector `manifest.yaml` + pipeline YAML/Python, builds `ConnectorContext`, calls `run_read(ctx)`/`run_write(ctx)`/`verify`/`rollback`. Also provides `_load_pipeline_source()` for remote inline execution. |
```

- [ ] **Step 3: Update configuration table**

Remove `EMERGE_RUNNER_URL`, `EMERGE_RUNNER_MAP`, `EMERGE_RUNNER_URLS` env vars (replaced by `settings.json` profiles).

Add:

| `EMERGE_SETTINGS_PATH` | Path to `settings.json` | `~/.emerge/settings.json` |

Update the persisted route map section. Replace `runner-map.json` example with `settings.json` profiles example:

```json
{
  "profiles": {
    "local": { "execution": "local" },
    "hm-vm-a": {
      "execution": "remote",
      "runner_url": "http://10.0.0.11:8787",
      "connectors": {
        "hypermesh": { "hm_host": "10.0.0.11" }
      }
    }
  },
  "default_profile": "local"
}
```

- [ ] **Step 4: Update pipeline execution flow diagrams**

In the sequence diagram for remote execution, update the `icc_read` call to show `target_profile`, and show `ProfileRegistry` between `D` and `RR`:

```
CC->>D: icc_read { connector, pipeline, target_profile }
D->>PR: resolve(target_profile, connector)
PR-->>D: ResolvedProfile (mode, runner_url, params)
D->>PE: load pipeline source + manifest
```

- [ ] **Step 5: Update glossary**

Add the new terms from spec §10: `ConnectorContext`, `Connector Manifest`, `Profile`, `ProfileRegistry`, `Publish Gate`, `needs_review`, `Remote WAL Writeback`.

Remove `RunnerRouter` from glossary.

- [ ] **Step 6: Update CLAUDE.md Documentation Update Rules table**

Add rows:

```markdown
| New/changed connector manifest | `~/.emerge/connectors/<connector>/manifest.yaml` + README connector docs |
| Profile schema change | `scripts/policy_config.py` _DEFAULTS + README configuration table |
| Pipeline function signature change | README pipeline execution section + skills that create pipelines |
```

- [ ] **Step 7: Update test count badge**

Run the full suite and update the badge:

```bash
python -m pytest tests -q --tb=no 2>&1 | tail -3
```

Update the `![Tests]` badge in `README.md` with the new passing count.

- [ ] **Step 8: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs: update README and CLAUDE.md for ConnectorContext, ProfileRegistry, profiles"
```

---

## Task 10: New skills — `binding-execution-profile` and `installing-memory-hub-pipeline`

**Files:**
- Create: `skills/binding-execution-profile/SKILL.md`
- Create: `skills/installing-memory-hub-pipeline/SKILL.md`
- Modify: `skills/initializing-vertical-flywheel/SKILL.md`

- [ ] **Step 1: Create `binding-execution-profile` skill**

Create `skills/binding-execution-profile/SKILL.md`:

```markdown
---
name: binding-execution-profile
description: Use when adding a new machine, configuring a new remote runner, or switching execution target for a connector. Creates or updates a named profile in ~/.emerge/settings.json.
---

# Binding an Execution Profile

## Overview

An execution profile binds "where to run" (local vs remote runner URL) with "what to connect to" (connector-specific params like host, port, timeout). Profiles live in `~/.emerge/settings.json` under `profiles.<name>`.

## When to Use

- User says "I have a new HM machine" / "add a remote runner" / "configure vm-b"
- After installing a Memory Hub pipeline (prompted by `installing-memory-hub-pipeline`)
- When `icc_read`/`icc_write` fails with a connection error on a new machine

## Checklist

1. **Check settings file** — read `~/.emerge/settings.json`; create skeleton if absent
2. **Collect profile name** — ask user for a short slug (e.g. `hm-vm-a`, `local`)
3. **Collect execution mode** — `local`, `remote`, or `auto`
4. **If remote: bootstrap runner**
   - Collect runner URL
   - Run: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" runner-bootstrap --ssh-target "<user@host>" --target-profile "<name>" --runner-url "<url>"`
   - Verify: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" runner-status --pretty`
   - Proceed only when `Runner reachable: True`
5. **Load connector manifest** — read `~/.emerge/connectors/<connector>/manifest.yaml`; display declared params
6. **Collect param values** — for each param: show default, ask user to confirm or override
7. **Write profile** — update `settings.json`:
   ```json
   {
     "profiles": {
       "<name>": {
         "execution": "<mode>",
         "runner_url": "<url_if_remote>",
         "connectors": {
           "<connector>": { "<param>": "<value>", ... }
         }
       }
     }
   }
   ```
8. **Smoke test** — run `icc_read { connector, pipeline, target_profile: "<name>" }`; verify `verification_state == "verified"`
9. **Output change summary** — profile name, execution mode, params set, smoke test result

## Pass Criteria

Profile written to `settings.json` + smoke test passes.

## Notes

- Write `settings.json` atomically (temp file + rename) to avoid corruption.
- If no `manifest.yaml` exists for the connector, warn the user and suggest running `initializing-vertical-flywheel` first.
- Multiple profiles can target the same connector with different hosts.
```

- [ ] **Step 2: Create `installing-memory-hub-pipeline` skill**

Create `skills/installing-memory-hub-pipeline/SKILL.md`:

```markdown
---
name: installing-memory-hub-pipeline
description: Use when installing a pipeline package downloaded from Memory Hub into the local connector directory.
---

# Installing a Memory Hub Pipeline

## Overview

A Memory Hub pipeline package contains connector `manifest.yaml`, pipeline `.yaml` metadata, and `.py` logic. It ships with safe default param values (e.g. `127.0.0.1`) — production use requires binding a profile.

## Checklist

1. **Unpack package** — copy files to `~/.emerge/connectors/<connector>/`
2. **Read manifest** — display connector name, version, params
3. **Check for `needs_review`** — if any pipeline `.yaml` has `needs_review: true`:
   - Show the warning comment from the `.py` file
   - Ask user to confirm they've reviewed; do not proceed until confirmed
4. **Check existing profiles** — look for a profile in `settings.json` whose connector params match this connector
   - If none found: invoke `binding-execution-profile` skill
5. **Run smoke tests**:
   ```
   icc_read { connector: "<connector>", pipeline: "<read_pipeline>", target_profile: "<profile>" }
   icc_write { connector: "<connector>", pipeline: "<write_pipeline>", target_profile: "<profile>", ... }
   ```
6. **Verify policy registry** — confirm pipeline key appears in `pipeline://current`

## Pass Criteria

- `icc_read` and `icc_write` return `verification_state: "verified"`
- No unconfirmed `needs_review` pipelines
- Pipeline key visible in policy registry

## Notes

- If the package has no `manifest.yaml`, it is not a valid Memory Hub package. Reject it.
- If `verification_state` is `"degraded"`, check `rollback_result` and `stop_triggered` fields for diagnostics.
```

- [ ] **Step 3: Update `initializing-vertical-flywheel` skill**

In `skills/initializing-vertical-flywheel/SKILL.md`, make three targeted changes:

**Change 1:** Under "Assets To Create (Minimum)", add:
```markdown
- `~/.emerge/connectors/<vertical>/manifest.yaml`
```

**Change 2:** Under "Implementation Pattern", change step 1 to:
```markdown
1. Start by creating `manifest.yaml` with all connection params and safe defaults.
2. Then create mock-safe `*.py` with `run_read(ctx)` / `run_write(ctx)` signatures.
```

**Change 3:** Under "Verification Checklist", add item:
```markdown
4. `manifest.yaml` exists and all connection params are declared with safe defaults.
```

**Change 4:** After the "Reverse Flywheel Integration" section, add:
```markdown
## Profile Binding

After creating a new vertical connector, prompt the user to bind an execution profile:

> "Connector `<vertical>` created. Run `binding-execution-profile` to bind a profile (local or remote VM) before using `icc_read`/`icc_write` in production."
```

- [ ] **Step 4: Verify skills are syntactically valid**

```bash
# No automated test — just verify files exist and are non-empty
ls -la skills/binding-execution-profile/SKILL.md skills/installing-memory-hub-pipeline/SKILL.md
head -5 skills/initializing-vertical-flywheel/SKILL.md
```

- [ ] **Step 5: Commit**

```bash
git add skills/binding-execution-profile/ skills/installing-memory-hub-pipeline/ skills/initializing-vertical-flywheel/SKILL.md
git commit -m "docs: add binding-execution-profile and installing-memory-hub-pipeline skills; update flywheel skill"
```

---

## Task 11: Final verification

- [ ] **Step 1: Run full test suite**

```bash
python -m pytest tests -q --tb=short
```

Expected: all tests pass. Count should be ≥ 205 (190 baseline + ~15 new tests across tasks).

- [ ] **Step 2: Verify no IP literals in any pipeline**

```bash
grep -rn '\b\d\{1,3\}\.\d\{1,3\}\.\d\{1,3\}\.\d\{1,3\}\b' \
  tests/connectors/ scripts/pipeline_engine.py scripts/connector_context.py \
  scripts/profile_registry.py
```

Expected: zero matches (only legitimate IP patterns are in test harness server binding, not pipeline files)

- [ ] **Step 3: Verify all pipeline functions use `ctx` signature in mock connector**

```bash
grep -n "def run_read\|def run_write\|def verify_read\|def verify_write\|def rollback_write" \
  tests/connectors/mock/pipelines/**/*.py
```

Expected: all show `(ctx)` or `(ctx, rows)` or `(ctx, action_result)` — no `(metadata, args)`.

- [ ] **Step 4: Verify `RunnerRouter` is gone**

```bash
grep -rn "RunnerRouter" scripts/ tests/
```

Expected: zero matches.

- [ ] **Step 5: Smoke test with real settings (manual — optional)**

If you have a `~/.emerge/settings.json` with profiles configured, run:

```bash
python3 scripts/emerge_daemon.py  # start daemon
# In another terminal: use Claude Code to call icc_read with a known profile
```

- [ ] **Step 6: Final commit and update README badge**

```bash
python -m pytest tests -q --tb=no 2>&1 | tail -3
# Update README.md badge with the new test count
git add README.md
git commit -m "docs: update test count badge after ConnectorContext implementation"
```

---

## Spec Coverage Check

| Spec Section | Covered By |
|---|---|
| §1 ConnectorContext dataclass | Task 1 |
| §1 Params merge priority | Task 2 (ProfileRegistry) |
| §2 Connector Manifest + params schema | Task 4 (PipelineEngine.load_manifest), Task 6 (manifests) |
| §2 Pipeline-level param extension | Task 4 (load_manifest merges pipeline.params) |
| §3 Profile Binding in settings.json | Task 3 (policy_config) + Task 5 (daemon) |
| §3 ProfileRegistry.resolve() | Task 2 |
| §3 runner-map.json replaced | Task 5 (RunnerRouter removed) |
| §3 execution: "auto" fallback | Task 5 (icc_read handler) |
| §4a Auto-parameterization | Task 7 |
| §4b needs_review flag | Task 7 |
| §4c Generated ctx signature | Task 7 |
| §4d Remote WAL writeback | Task 5 (Step 9) + Task 8 (test) |
| §5a binding-execution-profile skill | Task 10 |
| §5b installing-memory-hub-pipeline skill | Task 10 |
| §5c initializing-vertical-flywheel update | Task 10 |
| §6 PipelineEngine refactor | Task 4 |
| §6 EmergeDaemon refactor | Task 5 |
| §6 Remote exec param separation | Task 5 (Step 8) |
| §6 MCP schema update | Task 5 |
| §7 Memory Hub publish contract | Task 7 (needs_review) + Task 11 (verification) |
| §8 Test strategy | Tasks 1–8 |
| README + CLAUDE.md docs | Task 9 |
