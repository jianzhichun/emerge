# Emerge Plugin Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement all four audit-identified capability gaps: core engine hardening (A), MCP surface expansion (B), L1.5 composition routing (C), and observability (D).

**Architecture:** Tasks run in dependency order — D1 (settings) first because A2 (retry) and all later tasks read from it; then A (hardening); then B (MCP surface); then C (L1.5 routing using B's registry reads); then D2 (metrics, wired into all emit points). Each task is RED→GREEN (failing test first, then implementation).

**Tech Stack:** Python 3.11+, stdlib only (no new dependencies). MCP JSON-RPC 2.0 protocol. Existing: `scripts/repl_daemon.py`, `scripts/runner_client.py`, `scripts/pipeline_engine.py`, `scripts/policy_config.py`, `scripts/state_tracker.py`, `hooks/pre_compact.py`.

---

## File Map

| File | Status | Responsibility |
|------|--------|---------------|
| `scripts/policy_config.py` | Modify | Add `load_settings()` singleton + `default_settings_path()` |
| `scripts/metrics.py` | Create | `MetricsSink` protocol, `LocalJSONLSink`, `NullSink`, `get_sink()` |
| `scripts/runner_client.py` | Modify | Add `RetryConfig` dataclass + retry loop in `call_tool()` |
| `scripts/pipeline_engine.py` | Modify | Add `_validate_metadata()` called from `_load_metadata()` |
| `hooks/pre_compact.py` | Rewrite | Real implementation: load tracker → format_recovery_token → emit JSON |
| `scripts/repl_daemon.py` | Modify | Add resources/list+read, prompts/list+get, icc_reconcile tool, L1.5 `_try_l15_promote()`, metrics emit |
| `.claude-plugin/plugin.json` | Rewrite | Add capabilities, permissions, version 0.2.0 |
| `tests/test_settings.py` | Create | Settings load/override/validation tests |
| `tests/test_metrics.py` | Create | MetricsSink emit + LocalJSONLSink file content tests |
| `tests/test_pipeline_engine.py` | Modify | Schema validation error tests |
| `tests/test_hook_scripts_output.py` | Modify | PreCompact real output shape tests |
| `tests/test_mcp_tools_integration.py` | Modify | Resources, prompts, icc_reconcile, L1.5 promotion tests |
| `tests/test_runner_retry.py` | Create | RetryConfig backoff + HTTP 5xx retry tests |

---

## Task 1: Settings singleton (D1 foundation)

**Files:**
- Modify: `scripts/policy_config.py`
- Create: `tests/test_settings.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_settings.py
from __future__ import annotations
import json, os
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
import sys
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _write_settings(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_load_settings_returns_defaults_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setenv("EMERGE_SETTINGS_PATH", str(tmp_path / "nonexistent.json"))
    from scripts.policy_config import load_settings, _reset_settings_cache
    _reset_settings_cache()
    s = load_settings()
    assert s["policy"]["promote_min_attempts"] == 20
    assert s["runner"]["timeout_s"] == 30
    assert s["metrics_sink"] == "local_jsonl"


def test_load_settings_file_overrides_defaults(tmp_path, monkeypatch):
    cfg = tmp_path / "settings.json"
    _write_settings(cfg, {"policy": {"promote_min_attempts": 50}})
    monkeypatch.setenv("EMERGE_SETTINGS_PATH", str(cfg))
    from scripts.policy_config import load_settings, _reset_settings_cache
    _reset_settings_cache()
    s = load_settings()
    assert s["policy"]["promote_min_attempts"] == 50
    # non-overridden key keeps default
    assert s["policy"]["promote_min_success_rate"] == 0.95


def test_load_settings_env_path_takes_priority(tmp_path, monkeypatch):
    cfg = tmp_path / "custom.json"
    _write_settings(cfg, {"metrics_sink": "null"})
    monkeypatch.setenv("EMERGE_SETTINGS_PATH", str(cfg))
    from scripts.policy_config import load_settings, _reset_settings_cache
    _reset_settings_cache()
    s = load_settings()
    assert s["metrics_sink"] == "null"


def test_load_settings_rejects_invalid_policy_value(tmp_path, monkeypatch):
    cfg = tmp_path / "bad.json"
    _write_settings(cfg, {"policy": {"promote_min_attempts": "not-a-number"}})
    monkeypatch.setenv("EMERGE_SETTINGS_PATH", str(cfg))
    from scripts.policy_config import load_settings, _reset_settings_cache
    _reset_settings_cache()
    with pytest.raises(ValueError, match="promote_min_attempts"):
        load_settings()
```

- [ ] **Step 2: Run to verify RED**

```
pytest tests/test_settings.py -v
```
Expected: 4 failures — `ImportError: cannot import name 'load_settings'`

- [ ] **Step 3: Implement settings in `scripts/policy_config.py`**

Add after existing constants (keep all existing code):

```python
import json as _json
import os as _os

_SETTINGS_CACHE: dict | None = None

_DEFAULTS: dict = {
    "policy": {
        "promote_min_attempts": PROMOTE_MIN_ATTEMPTS,
        "promote_min_success_rate": PROMOTE_MIN_SUCCESS_RATE,
        "promote_min_verify_rate": PROMOTE_MIN_VERIFY_RATE,
        "promote_max_human_fix_rate": PROMOTE_MAX_HUMAN_FIX_RATE,
        "rollback_consecutive_failures": ROLLBACK_CONSECUTIVE_FAILURES,
        "stable_min_attempts": STABLE_MIN_ATTEMPTS,
        "stable_min_success_rate": STABLE_MIN_SUCCESS_RATE,
        "stable_min_verify_rate": STABLE_MIN_VERIFY_RATE,
        "window_size": WINDOW_SIZE,
    },
    "connector_root": "~/.emerge/connectors",
    "runner": {
        "timeout_s": 30,
        "retry_max_attempts": 3,
        "retry_base_delay_s": 0.5,
        "retry_max_delay_s": 10.0,
    },
    "metrics_sink": "local_jsonl",
}

_POLICY_INT_KEYS = {
    "promote_min_attempts", "rollback_consecutive_failures",
    "stable_min_attempts", "window_size",
}
_POLICY_FLOAT_KEYS = {
    "promote_min_success_rate", "promote_min_verify_rate",
    "promote_max_human_fix_rate", "stable_min_success_rate", "stable_min_verify_rate",
}


def default_settings_path() -> Path:
    raw = _os.environ.get("EMERGE_SETTINGS_PATH", "").strip()
    if raw:
        return Path(raw).expanduser()
    return default_emerge_home() / "settings.json"


def _reset_settings_cache() -> None:
    global _SETTINGS_CACHE
    _SETTINGS_CACHE = None


def _deep_merge(base: dict, override: dict) -> dict:
    result = {**base}
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _validate_settings(s: dict) -> None:
    policy = s.get("policy", {})
    for key in _POLICY_INT_KEYS:
        if key in policy and not isinstance(policy[key], int):
            raise ValueError(f"settings.policy.{key} must be an integer, got {policy[key]!r}")
    for key in _POLICY_FLOAT_KEYS:
        if key in policy and not isinstance(policy[key], (int, float)):
            raise ValueError(f"settings.policy.{key} must be a number, got {policy[key]!r}")
    runner = s.get("runner", {})
    if "timeout_s" in runner and not isinstance(runner["timeout_s"], (int, float)):
        raise ValueError(f"settings.runner.timeout_s must be a number")
    sink = s.get("metrics_sink", "local_jsonl")
    if sink not in ("local_jsonl", "null"):
        raise ValueError(f"settings.metrics_sink must be 'local_jsonl' or 'null', got {sink!r}")


def load_settings() -> dict:
    global _SETTINGS_CACHE
    if _SETTINGS_CACHE is not None:
        return _SETTINGS_CACHE
    path = default_settings_path()
    if path.exists():
        raw = _json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"settings file must be a JSON object: {path}")
        merged = _deep_merge(_DEFAULTS, raw)
    else:
        merged = _deep_merge(_DEFAULTS, {})
    _validate_settings(merged)
    _SETTINGS_CACHE = merged
    return _SETTINGS_CACHE
```

- [ ] **Step 4: Run to verify GREEN**

```
pytest tests/test_settings.py -v
```
Expected: 4 passed.

- [ ] **Step 5: Run full suite to check no regressions**

```
pytest -q
```
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/policy_config.py tests/test_settings.py
git commit -m "feat: add load_settings() singleton with env/file/default merge and validation"
```

---

## Task 2: Metrics sink (D2)

**Files:**
- Create: `scripts/metrics.py`
- Create: `tests/test_metrics.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_metrics.py
from __future__ import annotations
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_local_jsonl_sink_appends_event(tmp_path):
    from scripts.metrics import LocalJSONLSink
    sink = LocalJSONLSink(path=tmp_path / "metrics.jsonl")
    sink.emit("pipeline.read", {"pipeline_id": "mock.read.layers", "ok": True})
    lines = (tmp_path / "metrics.jsonl").read_text().strip().split("\n")
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["event_type"] == "pipeline.read"
    assert event["pipeline_id"] == "mock.read.layers"
    assert "ts_ms" in event


def test_local_jsonl_sink_appends_multiple(tmp_path):
    from scripts.metrics import LocalJSONLSink
    sink = LocalJSONLSink(path=tmp_path / "m.jsonl")
    sink.emit("exec.call", {"target_profile": "default"})
    sink.emit("runner.retry", {"attempt": 1})
    lines = (tmp_path / "m.jsonl").read_text().strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[1])["event_type"] == "runner.retry"


def test_null_sink_does_not_write(tmp_path):
    from scripts.metrics import NullSink
    sink = NullSink()
    sink.emit("anything", {"x": 1})  # must not raise


def test_get_sink_returns_local_jsonl_by_default(tmp_path):
    from scripts.metrics import get_sink, LocalJSONLSink
    sink = get_sink({"metrics_sink": "local_jsonl"}, default_path=tmp_path / "m.jsonl")
    assert isinstance(sink, LocalJSONLSink)


def test_get_sink_returns_null_sink(tmp_path):
    from scripts.metrics import get_sink, NullSink
    sink = get_sink({"metrics_sink": "null"}, default_path=tmp_path / "m.jsonl")
    assert isinstance(sink, NullSink)
```

- [ ] **Step 2: Run to verify RED**

```
pytest tests/test_metrics.py -v
```
Expected: 5 failures — `ModuleNotFoundError: No module named 'scripts.metrics'`

- [ ] **Step 3: Create `scripts/metrics.py`**

```python
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


class NullSink:
    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        pass


class LocalJSONLSink:
    def __init__(self, path: Path) -> None:
        self._path = path

    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        event = {"ts_ms": int(time.time() * 1000), "event_type": event_type, **payload}
        line = json.dumps(event, ensure_ascii=True) + "\n"
        fd, tmp = tempfile.mkstemp(prefix=".metrics-", suffix=".jsonl", dir=str(self._path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                if self._path.exists():
                    f.write(self._path.read_text(encoding="utf-8"))
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
            tmp = ""
        finally:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)


def get_sink(
    settings: dict[str, Any],
    *,
    default_path: Path | None = None,
) -> "LocalJSONLSink | NullSink":
    kind = str(settings.get("metrics_sink", "local_jsonl"))
    if kind == "null":
        return NullSink()
    path = default_path or (Path.home() / ".emerge" / "metrics.jsonl")
    return LocalJSONLSink(path=path)
```

- [ ] **Step 4: Run to verify GREEN**

```
pytest tests/test_metrics.py -v
```
Expected: 5 passed.

- [ ] **Step 5: Run full suite**

```
pytest -q
```
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/metrics.py tests/test_metrics.py
git commit -m "feat: add MetricsSink with LocalJSONLSink and NullSink"
```

---

## Task 3: Runner retry/backoff (A2)

**Files:**
- Modify: `scripts/runner_client.py`
- Create: `tests/test_runner_retry.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_runner_retry.py
from __future__ import annotations
import json, time, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _make_flaky_server(fail_count: int, port: int) -> tuple[HTTPServer, list[int]]:
    """Server that returns 503 for first `fail_count` requests then succeeds."""
    call_log: list[int] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            call_log.append(len(call_log) + 1)
            if len(call_log) <= fail_count:
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b'{"ok":false,"error":"temporary"}')
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true,"result":{"isError":false,"content":[{"type":"text","text":"ok"}]}}')

        def log_message(self, *a): pass

    srv = HTTPServer(("127.0.0.1", port), Handler)
    return srv, call_log


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_call_tool_retries_on_5xx_and_succeeds():
    from scripts.runner_client import RunnerClient, RetryConfig
    port = _free_port()
    srv, log = _make_flaky_server(fail_count=2, port=port)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        client = RunnerClient(
            base_url=f"http://127.0.0.1:{port}",
            timeout_s=5.0,
            retry=RetryConfig(max_attempts=4, base_delay_s=0.01, max_delay_s=0.05),
        )
        result = client.call_tool("icc_exec", {"code": "x=1"})
        assert result["isError"] is False
        assert len(log) == 3  # 2 failures + 1 success
    finally:
        srv.shutdown()


def test_call_tool_raises_after_max_attempts():
    from scripts.runner_client import RunnerClient, RetryConfig
    import pytest
    port = _free_port()
    srv, log = _make_flaky_server(fail_count=99, port=port)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        client = RunnerClient(
            base_url=f"http://127.0.0.1:{port}",
            timeout_s=5.0,
            retry=RetryConfig(max_attempts=2, base_delay_s=0.01, max_delay_s=0.05),
        )
        with pytest.raises(RuntimeError, match="runner http 503"):
            client.call_tool("icc_exec", {"code": "x=1"})
        assert len(log) == 2
    finally:
        srv.shutdown()


def test_retry_config_defaults():
    from scripts.runner_client import RetryConfig
    r = RetryConfig()
    assert r.max_attempts == 3
    assert r.base_delay_s == 0.5
    assert r.max_delay_s == 10.0
```

- [ ] **Step 2: Run to verify RED**

```
pytest tests/test_runner_retry.py -v
```
Expected: failures — `cannot import name 'RetryConfig'`

- [ ] **Step 3: Modify `scripts/runner_client.py`**

Add after the imports at the top:

```python
import random
import time as _time
```

Add `RetryConfig` dataclass before `RunnerClient`:

```python
@dataclass
class RetryConfig:
    max_attempts: int = 3
    base_delay_s: float = 0.5
    max_delay_s: float = 10.0
```

Add `retry` field to `RunnerClient` dataclass:

```python
@dataclass
class RunnerClient:
    base_url: str
    timeout_s: float = 30.0
    retry: "RetryConfig | None" = None
```

Replace the `call_tool` method body with:

```python
    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        retry = self.retry or RetryConfig(max_attempts=1)
        last_exc: Exception | None = None
        for attempt in range(max(1, retry.max_attempts)):
            if attempt > 0:
                delay = min(retry.base_delay_s * (2 ** (attempt - 1)), retry.max_delay_s)
                _time.sleep(delay * random.random())
            try:
                return self._call_tool_once(tool_name, arguments)
            except RuntimeError as exc:
                msg = str(exc)
                # retry on connection errors and 5xx; not on 4xx
                if "runner http 4" in msg:
                    raise
                last_exc = exc
        assert last_exc is not None
        raise last_exc

    def _call_tool_once(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        payload = {"tool_name": tool_name, "arguments": arguments}
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        req = urllib.request.Request(
            url=f"{self.base_url}/run",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with _NO_PROXY_OPENER.open(req, timeout=self.timeout_s) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"runner http {exc.code}: {detail}") from exc
        except Exception as exc:
            raise RuntimeError(f"runner unreachable: {exc}") from exc
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise RuntimeError("runner response must be an object")
        if not bool(data.get("ok", False)):
            err = str(data.get("error", "unknown runner error"))
            raise RuntimeError(err)
        result = data.get("result", {})
        if not isinstance(result, dict):
            raise RuntimeError("runner result must be an object")
        return result
```

- [ ] **Step 4: Wire retry config from settings in `RunnerRouter.from_env()`**

In `runner_client.py`, in `RunnerRouter.from_env()`, after loading `timeout_s`, add:

```python
        from scripts.policy_config import load_settings
        try:
            _s = load_settings()
            _r = _s.get("runner", {})
            retry_cfg = RetryConfig(
                max_attempts=int(_r.get("retry_max_attempts", 3)),
                base_delay_s=float(_r.get("retry_base_delay_s", 0.5)),
                max_delay_s=float(_r.get("retry_max_delay_s", 10.0)),
            )
        except Exception:
            retry_cfg = RetryConfig()
```

Then pass `retry=retry_cfg` when constructing `RunnerClient` objects (all 3 places: `default_client`, `mapped_clients`, `pooled_clients`).

- [ ] **Step 5: Run to verify GREEN**

```
pytest tests/test_runner_retry.py -v
```
Expected: 3 passed.

- [ ] **Step 6: Run full suite**

```
pytest -q
```
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add scripts/runner_client.py tests/test_runner_retry.py
git commit -m "feat: add RetryConfig with exponential backoff + full jitter to RunnerClient"
```

---

## Task 4: Pipeline metadata validation (A3)

**Files:**
- Modify: `scripts/pipeline_engine.py`
- Modify: `tests/test_pipeline_engine.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_pipeline_engine.py`:

```python
def test_load_metadata_rejects_missing_intent_signature(tmp_path):
    from scripts.pipeline_engine import PipelineEngine
    bad = tmp_path / "bad.yaml"
    bad.write_text('{"rollback_or_stop_policy": "stop", "read_steps": ["x"], "verify_steps": ["y"]}')
    engine = PipelineEngine()
    with pytest.raises(ValueError, match="intent_signature"):
        engine._load_metadata(bad)


def test_load_metadata_rejects_invalid_policy(tmp_path):
    from scripts.pipeline_engine import PipelineEngine
    bad = tmp_path / "bad.yaml"
    bad.write_text('{"intent_signature": "s", "rollback_or_stop_policy": "unknown", "read_steps": ["x"], "verify_steps": ["y"]}')
    engine = PipelineEngine()
    with pytest.raises(ValueError, match="rollback_or_stop_policy"):
        engine._load_metadata(bad)


def test_load_metadata_rejects_missing_steps(tmp_path):
    from scripts.pipeline_engine import PipelineEngine
    bad = tmp_path / "bad.yaml"
    bad.write_text('{"intent_signature": "s", "rollback_or_stop_policy": "stop", "verify_steps": ["y"]}')
    engine = PipelineEngine()
    with pytest.raises(ValueError, match="read_steps.*write_steps"):
        engine._load_metadata(bad)


def test_load_metadata_rejects_missing_verify_steps(tmp_path):
    from scripts.pipeline_engine import PipelineEngine
    bad = tmp_path / "bad.yaml"
    bad.write_text('{"intent_signature": "s", "rollback_or_stop_policy": "stop", "read_steps": ["x"]}')
    engine = PipelineEngine()
    with pytest.raises(ValueError, match="verify_steps"):
        engine._load_metadata(bad)


def test_load_metadata_accepts_valid_metadata(tmp_path):
    from scripts.pipeline_engine import PipelineEngine
    good = tmp_path / "good.yaml"
    good.write_text('{"intent_signature": "read.mock.test", "rollback_or_stop_policy": "stop", "read_steps": ["x"], "verify_steps": ["y"]}')
    engine = PipelineEngine()
    data = engine._load_metadata(good)
    assert data["intent_signature"] == "read.mock.test"
```

(Add `import pytest` at top of test file if not present.)

- [ ] **Step 2: Run to verify RED**

```
pytest tests/test_pipeline_engine.py::test_load_metadata_rejects_missing_intent_signature tests/test_pipeline_engine.py::test_load_metadata_rejects_invalid_policy tests/test_pipeline_engine.py::test_load_metadata_rejects_missing_steps tests/test_pipeline_engine.py::test_load_metadata_rejects_missing_verify_steps tests/test_pipeline_engine.py::test_load_metadata_accepts_valid_metadata -v
```
Expected: 5 failures — no validation exists yet.

- [ ] **Step 3: Add `_validate_metadata` to `scripts/pipeline_engine.py`**

Add this static method to `PipelineEngine`:

```python
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
        if not has_read and not has_write:
            errors.append("read_steps or write_steps (at least one required, non-empty list)")
        has_verify = isinstance(data.get("verify_steps"), list) and len(data["verify_steps"]) > 0
        if not has_verify:
            errors.append("verify_steps (required, non-empty list)")
        if errors:
            raise ValueError(
                f"pipeline metadata invalid at {path}: missing/invalid fields: {', '.join(errors)}"
            )
```

In `_load_metadata`, call it after parsing:

```python
    @staticmethod
    def _load_metadata(path: Path) -> dict[str, Any]:
        text = path.read_text(encoding="utf-8")
        try:
            import yaml
            loaded = yaml.safe_load(text)
            if not isinstance(loaded, dict):
                raise ValueError("metadata must be an object")
        except Exception:
            loaded = json.loads(text)
            if not isinstance(loaded, dict):
                raise ValueError("metadata must be a JSON object")
        PipelineEngine._validate_metadata(path, loaded)
        return loaded
```

- [ ] **Step 4: Run to verify GREEN**

```
pytest tests/test_pipeline_engine.py -v
```
Expected: all pass.

- [ ] **Step 5: Run full suite**

```
pytest -q
```
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/pipeline_engine.py tests/test_pipeline_engine.py
git commit -m "feat: validate pipeline metadata schema on load — reject missing/invalid fields"
```

---

## Task 5: PreCompact hook — real implementation (A1)

**Files:**
- Rewrite: `hooks/pre_compact.py`
- Modify: `tests/test_hook_scripts_output.py`

- [ ] **Step 1: Update test to assert real output shape**

Replace the existing `test_post_tool_use_and_pre_compact_contract` test in `tests/test_hook_scripts_output.py`. Find and replace the pre_compact assertion block:

```python
def test_pre_compact_emits_recovery_token(tmp_path: Path):
    import subprocess, json, os
    # First seed some state via post_tool_use
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_DATA"] = str(tmp_path)
    subprocess.run(
        ["python3", str(ROOT / "hooks" / "post_tool_use.py")],
        input=json.dumps({
            "tool_name": "mcp__plugin_emerge__icc_write",
            "tool_result": {"verification_state": "verified"},
            "delta_message": "Wrote layer to ZWCAD",
        }),
        capture_output=True, text=True, env=env, check=True,
    )
    # Now run pre_compact with the seeded state
    proc = subprocess.run(
        ["python3", str(ROOT / "hooks" / "pre_compact.py")],
        input="{}",
        capture_output=True, text=True, env=env, check=True,
    )
    out = json.loads(proc.stdout.strip())
    assert out["hookSpecificOutput"]["hookEventName"] == "PreCompact"
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "L1_5_TOKEN" in ctx
    token_text = ctx.rsplit("L1_5_TOKEN\n", 1)[1].strip()
    token = json.loads(token_text)
    assert token["schema_version"] == "l15.v1"
    assert isinstance(token["deltas"], list)
    assert len(ctx) <= 900  # budget enforced
```

Also update the old test that asserted `text.startswith("Keep only Goal")` — remove that assertion or replace the test body:

```python
def test_post_tool_use_and_pre_compact_contract(tmp_path: Path):
    # post_tool_use part (keep existing assertions)
    p_out = _run(
        "post_tool_use.py",
        {
            "tool_name": "mcp__plugin_emerge__icc_read",
            "tool_result": {"verification_state": "verified"},
            "delta_message": "Read layer snapshot",
        },
        tmp_path,
    )
    p_json = json.loads(p_out)
    assert p_json["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "additionalContext" in p_json["hookSpecificOutput"]
    token = _extract_l15_token(p_json["hookSpecificOutput"]["additionalContext"])
    assert token["schema_version"] == "l15.v1"
    assert token["deltas"]
    # pre_compact now emits JSON, not plain text
    import subprocess, os
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_DATA"] = str(tmp_path)
    proc = subprocess.run(
        ["python3", str(ROOT / "hooks" / "pre_compact.py")],
        input="{}",
        capture_output=True, text=True, env=env, check=True,
    )
    out = json.loads(proc.stdout.strip())
    assert out["hookSpecificOutput"]["hookEventName"] == "PreCompact"
```

- [ ] **Step 2: Run to verify RED**

```
pytest tests/test_hook_scripts_output.py::test_pre_compact_emits_recovery_token -v
```
Expected: FAIL — pre_compact outputs plain text, not JSON.

- [ ] **Step 3: Rewrite `hooks/pre_compact.py`**

```python
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.policy_config import default_hook_state_root  # noqa: E402
from scripts.state_tracker import load_tracker  # noqa: E402

_BUDGET_CHARS = 800


def main() -> None:
    payload_text = sys.stdin.read().strip()
    try:
        payload = json.loads(payload_text) if payload_text else {}
    except Exception:
        payload = {}

    state_path = Path(
        os.environ.get("CLAUDE_PLUGIN_DATA", str(default_hook_state_root()))
    ) / "state.json"
    tracker = load_tracker(state_path)

    token = tracker.format_recovery_token(budget_chars=_BUDGET_CHARS)
    token_json = json.dumps(token, ensure_ascii=True, separators=(",", ":"))
    context_text = (
        f"Goal\n{token.get('goal') or 'Not set.'}\n\n"
        f"Open Risks\n"
        + ("\n".join(f"- {r}" for r in token.get("open_risks", [])) or "- None.")
        + f"\n\nL1_5_TOKEN\n{token_json}"
    )

    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreCompact",
            "additionalContext": context_text,
        }
    }
    print(json.dumps(out))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run to verify GREEN**

```
pytest tests/test_hook_scripts_output.py -v
```
Expected: all pass.

- [ ] **Step 5: Run full suite**

```
pytest -q
```
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add hooks/pre_compact.py tests/test_hook_scripts_output.py
git commit -m "feat: implement PreCompact hook — emit recovery token with budget-capped L1.5 state"
```

---

## Task 6: MCP Resources (B1)

**Files:**
- Modify: `scripts/repl_daemon.py`
- Modify: `tests/test_mcp_tools_integration.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_mcp_tools_integration.py`:

```python
def test_resources_list_returns_static_and_pipeline_uris():
    daemon = ReplDaemon(root=ROOT)
    resp = daemon.handle_jsonrpc({"jsonrpc": "2.0", "id": 50, "method": "resources/list", "params": {}})
    uris = [r["uri"] for r in resp["result"]["resources"]]
    assert "policy://current" in uris
    assert "runner://status" in uris
    assert "state://deltas" in uris
    # at least one pipeline:// from mock connector
    assert any(u.startswith("pipeline://") for u in uris)


def test_resources_read_policy_current(tmp_path):
    os.environ["REPL_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["REPL_SESSION_ID"] = "res-test"
    try:
        daemon = ReplDaemon(root=ROOT)
        # Seed a pipeline entry
        daemon.call_tool("icc_read", {"connector": "mock", "pipeline": "layers"})
        resp = daemon.handle_jsonrpc({"jsonrpc": "2.0", "id": 51, "method": "resources/read",
                                      "params": {"uri": "policy://current"}})
        resource = resp["result"]["resource"]
        assert resource["uri"] == "policy://current"
        assert resource["mimeType"] == "application/json"
        data = json.loads(resource["text"])
        assert "pipelines" in data
    finally:
        os.environ.pop("REPL_STATE_ROOT", None)
        os.environ.pop("REPL_SESSION_ID", None)


def test_resources_read_pipeline_uri():
    daemon = ReplDaemon(root=ROOT)
    resp = daemon.handle_jsonrpc({"jsonrpc": "2.0", "id": 52, "method": "resources/read",
                                  "params": {"uri": "pipeline://mock/read/layers"}})
    resource = resp["result"]["resource"]
    assert resource["uri"] == "pipeline://mock/read/layers"
    data = json.loads(resource["text"])
    assert "intent_signature" in data


def test_resources_read_unknown_uri_returns_error():
    daemon = ReplDaemon(root=ROOT)
    resp = daemon.handle_jsonrpc({"jsonrpc": "2.0", "id": 53, "method": "resources/read",
                                  "params": {"uri": "unknown://foo"}})
    assert "error" in resp or resp["result"].get("isError")
```

- [ ] **Step 2: Run to verify RED**

```
pytest tests/test_mcp_tools_integration.py::test_resources_list_returns_static_and_pipeline_uris tests/test_mcp_tools_integration.py::test_resources_read_policy_current tests/test_mcp_tools_integration.py::test_resources_read_pipeline_uri tests/test_mcp_tools_integration.py::test_resources_read_unknown_uri_returns_error -v
```
Expected: 4 failures — `Method not found: resources/list`

- [ ] **Step 3: Add resource handlers to `ReplDaemon.handle_jsonrpc()`**

In `scripts/repl_daemon.py`, add these two methods to `ReplDaemon`:

```python
    def _list_resources(self) -> list[dict[str, Any]]:
        static = [
            {"uri": "policy://current", "name": "Pipeline policy registry", "mimeType": "application/json"},
            {"uri": "runner://status", "name": "Runner health summary", "mimeType": "application/json"},
            {"uri": "state://deltas", "name": "State tracker deltas", "mimeType": "application/json"},
        ]
        # Dynamic pipeline:// URIs
        for connector_root in self.pipeline._connector_roots:
            if not connector_root.exists():
                continue
            for meta in connector_root.glob("*/pipelines/*/*.yaml"):
                parts = meta.relative_to(connector_root).parts
                if len(parts) == 4:
                    connector, _, mode, name_yaml = parts
                    name = name_yaml[:-5]
                    uri = f"pipeline://{connector}/{mode}/{name}"
                    static.append({"uri": uri, "name": f"{connector} {mode} pipeline: {name}", "mimeType": "application/json"})
        return static

    def _read_resource(self, uri: str) -> dict[str, Any]:
        if uri == "policy://current":
            session_dir = self._state_root / self._base_session_id
            path = session_dir / "pipelines-registry.json"
            data = self._load_json_object(path, root_key="pipelines")
            return {"uri": uri, "mimeType": "application/json", "text": json.dumps(data)}
        if uri == "runner://status":
            from scripts.runner_client import RunnerRouter
            router = RunnerRouter.from_env()
            summary = router.health_summary() if router else {"configured": False, "any_reachable": False}
            return {"uri": uri, "mimeType": "application/json", "text": json.dumps(summary)}
        if uri == "state://deltas":
            from scripts.policy_config import default_hook_state_root
            from scripts.state_tracker import load_tracker
            import os as _os
            state_path = Path(_os.environ.get("CLAUDE_PLUGIN_DATA", str(default_hook_state_root()))) / "state.json"
            tracker = load_tracker(state_path)
            return {"uri": uri, "mimeType": "application/json", "text": json.dumps(tracker.to_dict())}
        if uri.startswith("pipeline://"):
            rest = uri[len("pipeline://"):]
            parts = rest.split("/", 2)
            if len(parts) == 3:
                connector, mode, name = parts
                for connector_root in self.pipeline._connector_roots:
                    meta = connector_root / connector / "pipelines" / mode / f"{name}.yaml"
                    if meta.exists():
                        from scripts.pipeline_engine import PipelineEngine
                        data = PipelineEngine._load_metadata(meta)
                        return {"uri": uri, "mimeType": "application/json", "text": json.dumps(data)}
        raise KeyError(f"Resource not found: {uri}")
```

In `handle_jsonrpc`, add before the final return:

```python
        if method == "resources/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"resources": self._list_resources()}}

        if method == "resources/read":
            uri = params.get("uri", "")
            try:
                resource = self._read_resource(uri)
                return {"jsonrpc": "2.0", "id": req_id, "result": {"resource": resource}}
            except KeyError as exc:
                return {"jsonrpc": "2.0", "id": req_id,
                        "error": {"code": -32602, "message": str(exc)}}
```

- [ ] **Step 4: Run to verify GREEN**

```
pytest tests/test_mcp_tools_integration.py::test_resources_list_returns_static_and_pipeline_uris tests/test_mcp_tools_integration.py::test_resources_read_policy_current tests/test_mcp_tools_integration.py::test_resources_read_pipeline_uri tests/test_mcp_tools_integration.py::test_resources_read_unknown_uri_returns_error -v
```
Expected: 4 passed.

- [ ] **Step 5: Run full suite**

```
pytest -q
```
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/repl_daemon.py tests/test_mcp_tools_integration.py
git commit -m "feat: add MCP resources/list and resources/read — policy, pipeline, runner, state URIs"
```

---

## Task 7: MCP Prompts (B2)

**Files:**
- Modify: `scripts/repl_daemon.py`
- Modify: `tests/test_mcp_tools_integration.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_mcp_tools_integration.py`:

```python
def test_prompts_list_returns_icc_explore_and_icc_promote():
    daemon = ReplDaemon(root=ROOT)
    resp = daemon.handle_jsonrpc({"jsonrpc": "2.0", "id": 60, "method": "prompts/list", "params": {}})
    names = [p["name"] for p in resp["result"]["prompts"]]
    assert "icc_explore" in names
    assert "icc_promote" in names


def test_prompts_get_icc_explore():
    daemon = ReplDaemon(root=ROOT)
    resp = daemon.handle_jsonrpc({"jsonrpc": "2.0", "id": 61, "method": "prompts/get",
                                  "params": {"name": "icc_explore", "arguments": {"vertical": "zwcad", "goal": "list layers"}}})
    result = resp["result"]
    assert result["name"] == "icc_explore"
    assert isinstance(result["messages"], list) and result["messages"]
    assert "zwcad" in result["messages"][0]["content"]


def test_prompts_get_icc_promote():
    daemon = ReplDaemon(root=ROOT)
    resp = daemon.handle_jsonrpc({"jsonrpc": "2.0", "id": 62, "method": "prompts/get",
                                  "params": {"name": "icc_promote",
                                             "arguments": {"intent_signature": "zwcad.read.state",
                                                           "script_ref": "connectors/zwcad/read.py",
                                                           "connector": "zwcad"}}})
    result = resp["result"]
    assert result["name"] == "icc_promote"
    assert "zwcad" in result["messages"][0]["content"]


def test_prompts_get_unknown_returns_error():
    daemon = ReplDaemon(root=ROOT)
    resp = daemon.handle_jsonrpc({"jsonrpc": "2.0", "id": 63, "method": "prompts/get",
                                  "params": {"name": "nonexistent"}})
    assert "error" in resp
```

- [ ] **Step 2: Run to verify RED**

```
pytest tests/test_mcp_tools_integration.py::test_prompts_list_returns_icc_explore_and_icc_promote tests/test_mcp_tools_integration.py::test_prompts_get_icc_explore tests/test_mcp_tools_integration.py::test_prompts_get_icc_promote tests/test_mcp_tools_integration.py::test_prompts_get_unknown_returns_error -v
```
Expected: 4 failures.

- [ ] **Step 3: Add prompt handlers to `ReplDaemon`**

Add to `ReplDaemon`:

```python
    _PROMPTS = [
        {
            "name": "icc_explore",
            "description": "Explore a new vertical using icc_exec with policy tracking",
            "arguments": [
                {"name": "vertical", "description": "Name of the vertical (e.g. zwcad)", "required": True},
                {"name": "goal", "description": "What to explore", "required": False},
            ],
        },
        {
            "name": "icc_promote",
            "description": "Promote an exec history into a formalized pipeline",
            "arguments": [
                {"name": "intent_signature", "description": "Intent signature of the exec (e.g. zwcad.read.state)", "required": True},
                {"name": "script_ref", "description": "Path to the script that was executed", "required": True},
                {"name": "connector", "description": "Target connector name", "required": True},
            ],
        },
    ]

    def _get_prompt(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "icc_explore":
            vertical = str(arguments.get("vertical", "<vertical>"))
            goal = str(arguments.get("goal", "explore the vertical"))
            content = (
                f"Use icc_exec to explore the {vertical} vertical. Goal: {goal}.\n"
                f"Include intent_signature='<intent>' and script_ref='~/.emerge/connectors/{vertical}/pipelines/read/state.py' "
                f"in each icc_exec call so the policy flywheel can track progress.\n"
                f"When the exec is stable and consistent, use icc_read with connector='{vertical}' to verify the pipeline works."
            )
            return {"name": name, "messages": [{"role": "user", "content": content}]}
        if name == "icc_promote":
            sig = str(arguments.get("intent_signature", ""))
            ref = str(arguments.get("script_ref", ""))
            connector = str(arguments.get("connector", ""))
            content = (
                f"Promote the exec pattern '{sig}' (script: {ref}) to a formal {connector} pipeline.\n"
                f"1. Create ~/.emerge/connectors/{connector}/pipelines/read/<name>.yaml and <name>.py\n"
                f"2. Implement run_read() and verify_read() in the .py file\n"
                f"3. Call icc_read with connector='{connector}' to verify it works\n"
                f"4. The intent_signature in the yaml must match '{sig}'"
            )
            return {"name": name, "messages": [{"role": "user", "content": content}]}
        raise KeyError(f"Prompt not found: {name}")
```

In `handle_jsonrpc`, add:

```python
        if method == "prompts/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"prompts": self._PROMPTS}}

        if method == "prompts/get":
            pname = params.get("name", "")
            pargs = params.get("arguments") or {}
            try:
                prompt = self._get_prompt(pname, pargs)
                return {"jsonrpc": "2.0", "id": req_id, "result": prompt}
            except KeyError as exc:
                return {"jsonrpc": "2.0", "id": req_id,
                        "error": {"code": -32602, "message": str(exc)}}
```

- [ ] **Step 4: Run to verify GREEN**

```
pytest tests/test_mcp_tools_integration.py::test_prompts_list_returns_icc_explore_and_icc_promote tests/test_mcp_tools_integration.py::test_prompts_get_icc_explore tests/test_mcp_tools_integration.py::test_prompts_get_icc_promote tests/test_mcp_tools_integration.py::test_prompts_get_unknown_returns_error -v
```
Expected: 4 passed.

- [ ] **Step 5: Run full suite**

```
pytest -q
```
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/repl_daemon.py tests/test_mcp_tools_integration.py
git commit -m "feat: add MCP prompts/list and prompts/get — icc_explore and icc_promote templates"
```

---

## Task 8: icc_reconcile tool + plugin.json (B3+B4)

**Files:**
- Modify: `scripts/repl_daemon.py`
- Rewrite: `.claude-plugin/plugin.json`
- Modify: `tests/test_mcp_tools_integration.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_mcp_tools_integration.py`:

```python
def test_icc_reconcile_confirms_delta(tmp_path):
    import os
    os.environ["CLAUDE_PLUGIN_DATA"] = str(tmp_path)
    try:
        from scripts.policy_config import default_hook_state_root
        from scripts.state_tracker import StateTracker, save_tracker
        state_path = tmp_path / "state.json"
        tracker = StateTracker()
        delta_id = tracker.add_delta("test delta", level="core_critical", verification_state="verified", provisional=True)
        save_tracker(state_path, tracker)

        daemon = ReplDaemon(root=ROOT)
        resp = daemon.handle_jsonrpc({
            "jsonrpc": "2.0", "id": 70, "method": "tools/call",
            "params": {"name": "icc_reconcile", "arguments": {"delta_id": delta_id, "outcome": "confirm"}}
        })
        assert resp["result"]["isError"] is False
        body = json.loads(resp["result"]["content"][0]["text"])
        assert body["delta_id"] == delta_id
        assert body["outcome"] == "confirm"
        assert body["verification_state"] in ("verified", "degraded")
    finally:
        os.environ.pop("CLAUDE_PLUGIN_DATA", None)


def test_icc_reconcile_not_in_tools_list():
    daemon = ReplDaemon(root=ROOT)
    resp = daemon.handle_jsonrpc({"jsonrpc": "2.0", "id": 71, "method": "tools/list", "params": {}})
    names = [t["name"] for t in resp["result"]["tools"]]
    assert "icc_reconcile" not in names
```

- [ ] **Step 2: Run to verify RED**

```
pytest tests/test_mcp_tools_integration.py::test_icc_reconcile_confirms_delta tests/test_mcp_tools_integration.py::test_icc_reconcile_not_in_tools_list -v
```
Expected: 2 failures.

- [ ] **Step 3: Add `icc_reconcile` to `call_tool()` in `repl_daemon.py`**

In `call_tool()`, add after the `icc_write` block (before the final `return {"isError": True...}`):

```python
        if name == "icc_reconcile":
            try:
                delta_id = str(arguments.get("delta_id", "")).strip()
                outcome = str(arguments.get("outcome", "")).strip()
                if outcome not in ("confirm", "correct", "retract"):
                    raise ValueError(f"outcome must be confirm/correct/retract, got {outcome!r}")
                if not delta_id:
                    raise ValueError("delta_id is required")
                from scripts.policy_config import default_hook_state_root
                from scripts.state_tracker import load_tracker, save_tracker
                import os as _os2
                state_path = Path(
                    _os2.environ.get("CLAUDE_PLUGIN_DATA", str(default_hook_state_root()))
                ) / "state.json"
                tracker = load_tracker(state_path)
                tracker.reconcile_delta(delta_id, outcome)
                save_tracker(state_path, tracker)
                result_body = {
                    "delta_id": delta_id,
                    "outcome": outcome,
                    "verification_state": tracker.state.get("verification_state", "verified"),
                    "goal": tracker.state.get("goal", ""),
                }
                return {"isError": False, "content": [{"type": "text", "text": json.dumps(result_body)}]}
            except Exception as exc:
                return {"isError": True, "content": [{"type": "text", "text": f"icc_reconcile failed: {exc}"}]}
```

- [ ] **Step 4: Rewrite `.claude-plugin/plugin.json`**

```json
{
  "name": "emerge",
  "version": "0.2.0",
  "description": "Generic RWB flywheel plugin for Claude Code",
  "author": { "name": "Emerge" },
  "capabilities": {
    "tools": ["icc_exec", "icc_read", "icc_write", "icc_reconcile"],
    "resources": ["policy://current", "pipeline://*", "runner://status", "state://deltas"],
    "prompts": ["icc_explore", "icc_promote"]
  },
  "permissions": {
    "filesystem": ["~/.emerge/"],
    "network": ["localhost", "192.168.122.0/24"]
  }
}
```

- [ ] **Step 5: Run to verify GREEN**

```
pytest tests/test_mcp_tools_integration.py::test_icc_reconcile_confirms_delta tests/test_mcp_tools_integration.py::test_icc_reconcile_not_in_tools_list -v
```
Expected: 2 passed.

- [ ] **Step 6: Run full suite**

```
pytest -q
```
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add scripts/repl_daemon.py .claude-plugin/plugin.json tests/test_mcp_tools_integration.py
git commit -m "feat: add icc_reconcile tool and update plugin.json to v0.2.0 with capabilities"
```

---

## Task 9: L1.5 composition routing (C)

**Files:**
- Modify: `scripts/repl_daemon.py`
- Modify: `tests/test_mcp_tools_integration.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_mcp_tools_integration.py`:

```python
def test_l15_exec_routes_to_pipeline_when_stable(tmp_path):
    """When L1.5 candidate is stable AND pipeline is canary/stable, icc_exec is redirected."""
    os.environ["REPL_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["REPL_SESSION_ID"] = "l15-promote-test"
    try:
        daemon = ReplDaemon(root=ROOT)
        session_dir = tmp_path / "state" / "l15-promote-test"
        session_dir.mkdir(parents=True, exist_ok=True)

        # Seed L1.5 candidate as stable
        candidates = {
            "candidates": {
                "l15::mock.read.layers::zwcad.plan.read::connectors/zwcad/read.py": {
                    "status": "stable",
                    "attempts": 40, "successes": 40, "verify_passes": 40,
                    "human_fixes": 0, "degraded_count": 0, "consecutive_failures": 0,
                    "recent_outcomes": [1] * 20, "total_calls": 40, "last_ts_ms": 0,
                    "source": "l15_composed", "pipeline_id": "mock.read.layers",
                    "intent_signature": "zwcad.plan.read",
                    "script_ref": "connectors/zwcad/read.py",
                }
            }
        }
        (session_dir / "candidates.json").write_text(json.dumps(candidates))

        # Seed pipeline registry as canary
        pipelines = {
            "pipelines": {
                "pipeline::mock.read.layers": {
                    "status": "canary", "rollout_pct": 20,
                    "success_rate": 1.0, "verify_rate": 1.0,
                }
            }
        }
        (session_dir / "pipelines-registry.json").write_text(json.dumps(pipelines))

        # Call icc_exec with L1.5 args — should be promoted to icc_read
        out = daemon.call_tool("icc_exec", {
            "code": "x = 1",
            "intent_signature": "zwcad.plan.read",
            "script_ref": "connectors/zwcad/read.py",
            "base_pipeline_id": "mock.read.layers",
        })
        assert out["isError"] is False
        body = json.loads(out["content"][0]["text"])
        assert body.get("l15_promoted") is True
        assert body.get("pipeline_id") == "mock.read.layers"
    finally:
        os.environ.pop("REPL_STATE_ROOT", None)
        os.environ.pop("REPL_SESSION_ID", None)


def test_l15_exec_does_not_promote_when_candidate_is_canary(tmp_path):
    """When L1.5 candidate is only canary, exec runs normally (no promotion)."""
    os.environ["REPL_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["REPL_SESSION_ID"] = "l15-canary-test"
    try:
        daemon = ReplDaemon(root=ROOT)
        session_dir = tmp_path / "state" / "l15-canary-test"
        session_dir.mkdir(parents=True, exist_ok=True)

        candidates = {
            "candidates": {
                "l15::mock.read.layers::zwcad.plan.read::connectors/zwcad/read.py": {
                    "status": "canary",
                    "attempts": 20, "successes": 20, "verify_passes": 20,
                    "human_fixes": 0, "consecutive_failures": 0,
                    "recent_outcomes": [1] * 20, "total_calls": 20, "last_ts_ms": 0,
                }
            }
        }
        (session_dir / "candidates.json").write_text(json.dumps(candidates))

        out = daemon.call_tool("icc_exec", {
            "code": "print('hello')",
            "intent_signature": "zwcad.plan.read",
            "script_ref": "connectors/zwcad/read.py",
            "base_pipeline_id": "mock.read.layers",
        })
        assert out["isError"] is False
        body_text = out["content"][0]["text"]
        # Normal exec output — no l15_promoted key
        assert "hello" in body_text or "l15_promoted" not in body_text
    finally:
        os.environ.pop("REPL_STATE_ROOT", None)
        os.environ.pop("REPL_SESSION_ID", None)
```

- [ ] **Step 2: Run to verify RED**

```
pytest tests/test_mcp_tools_integration.py::test_l15_exec_routes_to_pipeline_when_stable tests/test_mcp_tools_integration.py::test_l15_exec_does_not_promote_when_candidate_is_canary -v
```
Expected: first test fails (no promotion logic), second passes or fails.

- [ ] **Step 3: Add `_try_l15_promote()` to `ReplDaemon`**

Add method to `ReplDaemon`:

```python
    def _try_l15_promote(self, arguments: dict[str, Any]) -> dict[str, Any] | None:
        intent_signature = str(arguments.get("intent_signature", "")).strip()
        script_ref = str(arguments.get("script_ref", "")).strip()
        base_pipeline_id = str(arguments.get("base_pipeline_id", "")).strip()
        if not (intent_signature and script_ref and base_pipeline_id):
            return None

        key = self._l15_candidate_key(base_pipeline_id, intent_signature, script_ref)
        session_dir = self._state_root / self._base_session_id
        candidates_path = session_dir / "candidates.json"
        candidates = self._load_json_object(candidates_path, root_key="candidates")
        candidate = candidates["candidates"].get(key)
        if not isinstance(candidate, dict):
            return None
        if str(candidate.get("status", "explore")) != "stable":
            return None

        pipelines_path = session_dir / "pipelines-registry.json"
        pipelines = self._load_json_object(pipelines_path, root_key="pipelines")
        pipeline_entry = pipelines["pipelines"].get(f"pipeline::{base_pipeline_id}")
        if not isinstance(pipeline_entry, dict):
            return None
        if str(pipeline_entry.get("status", "explore")) not in ("canary", "stable"):
            return None

        parts = base_pipeline_id.split(".", 2)
        if len(parts) != 3:
            return None
        connector, mode, name = parts
        if mode == "write":
            result = self.pipeline.run_write({**arguments, "connector": connector, "pipeline": name})
        else:
            result = self.pipeline.run_read({**arguments, "connector": connector, "pipeline": name})
        result["l15_promoted"] = True
        return result
```

In `call_tool()`, at the very start of the `if name == "icc_exec":` block (before the `try:`), add promotion check:

```python
        if name == "icc_exec":
            # L1.5 promotion: if candidate is stable and pipeline is ready, redirect
            promoted = self._try_l15_promote(arguments)
            if promoted is not None:
                response = {"isError": False, "content": [{"type": "text", "text": json.dumps(promoted)}]}
                try:
                    self._record_pipeline_event(
                        tool_name="icc_read" if promoted.get("pipeline_id", "").split(".")[1] == "read" else "icc_write",
                        arguments=arguments,
                        result=promoted,
                        is_error=False,
                    )
                except Exception:
                    pass
                return response
            try:
                # ... existing exec code unchanged ...
```

- [ ] **Step 4: Run to verify GREEN**

```
pytest tests/test_mcp_tools_integration.py::test_l15_exec_routes_to_pipeline_when_stable tests/test_mcp_tools_integration.py::test_l15_exec_does_not_promote_when_candidate_is_canary -v
```
Expected: 2 passed.

- [ ] **Step 5: Run full suite**

```
pytest -q
```
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/repl_daemon.py tests/test_mcp_tools_integration.py
git commit -m "feat: add L1.5 promotion routing — stable candidate + canary/stable pipeline redirects icc_exec to pipeline"
```

---

## Task 10: Wire metrics emit points (D2)

**Files:**
- Modify: `scripts/repl_daemon.py`
- Modify: `scripts/runner_client.py`
- Modify: `tests/test_metrics.py`

- [ ] **Step 1: Write failing integration test**

Add to `tests/test_metrics.py`:

```python
def test_daemon_emits_pipeline_read_metric(tmp_path):
    import os, sys
    from pathlib import Path
    ROOT = Path(__file__).resolve().parents[1]
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    os.environ["REPL_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["REPL_SESSION_ID"] = "metric-test"
    metrics_path = tmp_path / "metrics.jsonl"
    os.environ["EMERGE_SETTINGS_PATH"] = str(tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text(
        '{"metrics_sink": "local_jsonl", "metrics_path": "' + str(metrics_path) + '"}'
    )
    try:
        from scripts.policy_config import _reset_settings_cache
        _reset_settings_cache()
        from scripts.repl_daemon import ReplDaemon
        daemon = ReplDaemon(root=ROOT)
        daemon._metrics_path = metrics_path  # inject path for test isolation
        daemon.call_tool("icc_read", {"connector": "mock", "pipeline": "layers"})
        assert metrics_path.exists()
        events = [json.loads(l) for l in metrics_path.read_text().strip().split("\n") if l]
        types = [e["event_type"] for e in events]
        assert "pipeline.read" in types
    finally:
        os.environ.pop("REPL_STATE_ROOT", None)
        os.environ.pop("REPL_SESSION_ID", None)
        os.environ.pop("EMERGE_SETTINGS_PATH", None)
        from scripts.policy_config import _reset_settings_cache
        _reset_settings_cache()
```

- [ ] **Step 2: Run to verify RED**

```
pytest tests/test_metrics.py::test_daemon_emits_pipeline_read_metric -v
```
Expected: FAIL — daemon doesn't emit metrics.

- [ ] **Step 3: Initialize MetricsSink in `ReplDaemon.__init__()`**

Add to `ReplDaemon.__init__()` after `self._runner_router = RunnerRouter.from_env()`:

```python
        from scripts.policy_config import load_settings, default_emerge_home
        from scripts.metrics import get_sink
        try:
            _settings = load_settings()
        except Exception:
            _settings = {}
        _metrics_path = getattr(self, "_metrics_path", None) or (default_emerge_home() / "metrics.jsonl")
        self._sink = get_sink(_settings, default_path=_metrics_path)
```

- [ ] **Step 4: Add emit calls**

In `_record_pipeline_event()`, after the `events_path.open(...)` block, add:

```python
        try:
            self._sink.emit(
                f"pipeline.{mode}",
                {
                    "pipeline_id": pipeline_id,
                    "target_profile": target_profile,
                    "is_error": is_error,
                    "verify_passed": verify_passed,
                    "session_id": self._base_session_id,
                },
            )
        except Exception:
            pass
```

In `_record_exec_event()`, after the `events_path.open(...)` block, add:

```python
        try:
            self._sink.emit(
                "exec.call",
                {
                    "intent_signature": intent_signature,
                    "target_profile": target_profile,
                    "is_error": is_error,
                    "session_id": self._base_session_id,
                },
            )
        except Exception:
            pass
```

In `_update_pipeline_registry()`, after `self._atomic_write_json(registry_path, registry)`, if `transitioned` is True, add:

```python
        if transitioned:
            try:
                self._sink.emit(
                    "policy.transition",
                    {
                        "candidate_key": candidate_key,
                        "new_status": status,
                        "reason": reason,
                        "attempts": attempts,
                    },
                )
            except Exception:
                pass
```

In `_try_l15_promote()`, after setting `result["l15_promoted"] = True`, add:

```python
        try:
            self._sink.emit("l15.promoted", {"key": key, "pipeline_id": base_pipeline_id})
        except Exception:
            pass
```

In `RunnerClient.call_tool()`, in the retry loop after sleeping (attempt > 0), add before `self._call_tool_once(...)`:

Note: RunnerClient doesn't have access to daemon's sink. Add `on_retry` callback instead:

```python
@dataclass
class RunnerClient:
    base_url: str
    timeout_s: float = 30.0
    retry: "RetryConfig | None" = None
    on_retry: "Any" = None  # Optional[Callable[[int, Exception], None]]
```

In the retry loop:

```python
            if attempt > 0:
                delay = min(retry.base_delay_s * (2 ** (attempt - 1)), retry.max_delay_s)
                _time.sleep(delay * random.random())
                if callable(self.on_retry):
                    try:
                        self.on_retry(attempt, last_exc)
                    except Exception:
                        pass
```

- [ ] **Step 5: Run to verify GREEN**

```
pytest tests/test_metrics.py -v
```
Expected: all pass.

- [ ] **Step 6: Run full suite**

```
pytest -q
```
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add scripts/repl_daemon.py scripts/runner_client.py tests/test_metrics.py
git commit -m "feat: wire metrics emit points — pipeline.read/write, exec.call, l15.promoted, policy.transition"
```

---

## Task 11: Final integration check

- [ ] **Step 1: Run full test suite**

```
pytest -q
```
Expected: all pass, 0 failures.

- [ ] **Step 2: Smoke test resources + prompts via daemon**

```bash
python3 -c "
from pathlib import Path
from scripts.repl_daemon import ReplDaemon
import json
d = ReplDaemon(root=Path('.'))
r = d.handle_jsonrpc({'jsonrpc':'2.0','id':1,'method':'resources/list','params':{}})
print('Resources:', [x['uri'] for x in r['result']['resources']])
p = d.handle_jsonrpc({'jsonrpc':'2.0','id':2,'method':'prompts/list','params':{}})
print('Prompts:', [x['name'] for x in p['result']['prompts']])
"
```
Expected: lists policy://, pipeline://, runner://, state://; icc_explore and icc_promote.

- [ ] **Step 3: Smoke test policy-status shows settings are loaded**

```bash
python3 scripts/repl_admin.py policy-status --pretty
```
Expected: policy section shows thresholds (same or from settings file if present).

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "chore: plugin completion v0.2.0 — A/B/C/D workstreams complete"
```
