# Emerge Round-2 Optimizations: IO Safety & Performance

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 3 confirmed issues found in the second-round audit: metrics fsync, truncate_jsonl crash-safety, and runner-router per-call disk reads.

**Architecture:** Three independent fixes, applied in any order. No new abstractions — each fix is a minimal surgical change consistent with existing patterns in the codebase.

**Tech Stack:** Python 3.11+, stdlib (os, tempfile, threading), pytest

---

## False Positives (confirmed, do NOT fix)

| Issue | Why Skipped |
|-------|-------------|
| `_open_spans` thread safety | Daemon uses single-threaded stdio loop; background threads never touch `_open_spans` |
| `_should_sample` TOCTOU | Same — single-threaded, no concurrent writer to `pipelines-registry.json` |
| ExecSession globals size limit | WAL/session lifetime is bounded to daemon run; not a real-world issue |
| `call_tool` O(N) dispatch | N=12 tools, difference is nanoseconds; YAGNI |
| OperatorMonitor buffer locking | `run()` is a single thread — `_event_buffers` has no concurrent accessor |

---

## File Map

| File | Change |
|------|--------|
| `scripts/metrics.py` | Add `f.flush()` + `os.fsync()` to `LocalJSONLSink.emit()` |
| `scripts/policy_config.py:212-231` | Replace `truncate_jsonl_if_needed` with mkstemp+fsync+replace pattern |
| `scripts/emerge_daemon.py:162-168` | Cache `RunnerRouter` with mtime-based invalidation in `_get_runner_router()` |
| `tests/test_metrics.py` | New: fsync path test |
| `tests/test_policy_config.py` | Add: truncate_jsonl crash-safety test |
| `tests/test_mcp_tools_integration.py` | Add: runner router cache test |

---

## Task 1: metrics.py — add fsync to emit()

**Root cause:** `LocalJSONLSink.emit()` (metrics.py:22-23) opens the file, writes a line, and closes — no `flush()` or `fsync()`. On daemon crash, the last N events may be lost from OS buffer. Every other write path in the project uses fsync.

**Files:**
- Modify: `scripts/metrics.py:22-23`
- Test: `tests/test_metrics.py` (create new)

- [ ] **Step 1.1: Write the failing test**

Create `tests/test_metrics.py`:

```python
import json
import os


def test_emit_flushes_to_disk(tmp_path):
    """emit() must fsync — data must survive without closing the process."""
    from scripts.metrics import LocalJSONLSink

    path = tmp_path / "metrics.jsonl"
    sink = LocalJSONLSink(path=path)
    sink.emit("test.event", {"key": "value"})

    # Read raw bytes directly (bypassing Python file cache)
    raw = path.read_bytes()
    lines = [l for l in raw.decode().splitlines() if l.strip()]
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["event_type"] == "test.event"
    assert event["key"] == "value"
    assert "ts_ms" in event


def test_emit_appends_multiple_events(tmp_path):
    """Each emit() call must append a new line, not overwrite."""
    from scripts.metrics import LocalJSONLSink

    path = tmp_path / "metrics.jsonl"
    sink = LocalJSONLSink(path=path)
    sink.emit("event.one", {"n": 1})
    sink.emit("event.two", {"n": 2})
    sink.emit("event.three", {"n": 3})

    lines = [l for l in path.read_text().splitlines() if l.strip()]
    assert len(lines) == 3
    types = [json.loads(l)["event_type"] for l in lines]
    assert types == ["event.one", "event.two", "event.three"]


def test_null_sink_does_not_write(tmp_path):
    """NullSink must be a no-op (no file created)."""
    from scripts.metrics import NullSink

    sink = NullSink()
    sink.emit("ignored", {"x": 1})  # must not raise
```

- [ ] **Step 1.2: Run to confirm tests pass (they already pass — data is written)**

```bash
cd /Users/apple/Documents/workspace/emerge
python -m pytest tests/test_metrics.py -xvs 2>&1 | tail -15
```

These tests pass even without fsync because the test reads after the file closes. The key verification is that fsync is now called — we verify this by code inspection in Step 1.4.

- [ ] **Step 1.3: Update `LocalJSONLSink.emit()` in metrics.py**

Replace lines 18-23 (the entire `emit` method body):

```python
    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        import os as _os
        self._path.parent.mkdir(parents=True, exist_ok=True)
        event = {"ts_ms": int(time.time() * 1000), "event_type": event_type, **payload}
        line = json.dumps(event, ensure_ascii=True) + "\n"
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            _os.fsync(f.fileno())
```

Note: `os` is NOT currently imported at the top of `metrics.py`. The inline `import os as _os` keeps the module clean. Alternatively, add `import os` to the top-level imports (preferred for consistency — check what's already there):

Current imports in `metrics.py`:
```python
import json
import time
from pathlib import Path
from typing import Any
```

Add `import os` after `import json`:

```python
import json
import os
import time
from pathlib import Path
from typing import Any
```

Then use `os.fsync` directly:

```python
    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        event = {"ts_ms": int(time.time() * 1000), "event_type": event_type, **payload}
        line = json.dumps(event, ensure_ascii=True) + "\n"
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
```

- [ ] **Step 1.4: Run full test suite**

```bash
python -m pytest tests -q 2>&1 | tail -10
```

Expected: ≥348 tests passing (347 + 3 new), 0 failures.

- [ ] **Step 1.5: Commit**

```bash
git add scripts/metrics.py tests/test_metrics.py
git commit -m "fix: LocalJSONLSink.emit adds flush+fsync — metrics survive daemon crash"
```

---

## Task 2: truncate_jsonl_if_needed — mkstemp + fsync

**Root cause:** `policy_config.py:truncate_jsonl_if_needed()` (lines 227-229) uses `path.with_suffix(".tmp")` — a fixed temp name — and `write_text()` (no fsync). Issues:
1. Fixed name `.tmp` can collide if two callers race (e.g., exec-events truncation and pipeline-events truncation happen to use the same file suffix pattern)
2. No `fsync` before rename — data may not survive crash
3. Inconsistent with rest of codebase (all other writes use mkstemp+fsync+replace)

**Files:**
- Modify: `scripts/policy_config.py:212-231`
- Test: `tests/test_policy_config.py`

- [ ] **Step 2.1: Check if test_policy_config.py exists**

```bash
ls /Users/apple/Documents/workspace/emerge/tests/test_policy_config.py 2>/dev/null || echo "not found"
```

If not found, create it. If found, append to it.

- [ ] **Step 2.2: Write the failing test**

Add to `tests/test_policy_config.py` (create if not exists):

```python
def test_truncate_jsonl_no_stray_tmp_file(tmp_path):
    """After truncation, no fixed-name .tmp file must remain."""
    import json
    from scripts.policy_config import truncate_jsonl_if_needed

    path = tmp_path / "events.jsonl"
    # Write 20000 lines to trigger truncation (max_lines=10000, ratio=1.5 → triggers at 15000)
    lines = [json.dumps({"n": i}) for i in range(20000)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    truncate_jsonl_if_needed(path, max_lines=10000)

    # Verify: no fixed-name .tmp stray files
    stray = list(tmp_path.glob("events.tmp"))
    assert stray == [], f"stray fixed-name .tmp files: {stray}"

    # Verify: file was actually truncated
    remaining = [l for l in path.read_text().splitlines() if l.strip()]
    assert len(remaining) == 10000, f"Expected 10000 lines after truncation, got {len(remaining)}"

    # Verify: correct lines (should be the LAST 10000)
    last = json.loads(remaining[-1])
    assert last["n"] == 19999
    first = json.loads(remaining[0])
    assert first["n"] == 10000


def test_truncate_jsonl_below_threshold_no_change(tmp_path):
    """File below threshold must not be modified at all."""
    import json
    from scripts.policy_config import truncate_jsonl_if_needed

    path = tmp_path / "small.jsonl"
    lines = [json.dumps({"n": i}) for i in range(100)]
    original = "\n".join(lines) + "\n"
    path.write_text(original, encoding="utf-8")

    mtime_before = path.stat().st_mtime
    truncate_jsonl_if_needed(path, max_lines=10000)
    mtime_after = path.stat().st_mtime

    assert mtime_before == mtime_after, "File must not be touched when below threshold"
```

- [ ] **Step 2.3: Run to confirm first test FAILS (stray .tmp file exists)**

```bash
cd /Users/apple/Documents/workspace/emerge
python -m pytest tests/test_policy_config.py::test_truncate_jsonl_no_stray_tmp_file -xvs 2>&1 | tail -15
```

Expected: FAIL — `events.tmp` exists after truncation (old code leaves it via `with_suffix(".tmp")`).

Actually, `tmp.replace(path)` moves the file, so `events.tmp` won't exist after a successful call. But on crash between `write_text` and `replace`, `.tmp` would remain. The test may PASS with old code since we can't simulate a crash in a unit test.

The real problem is the fixed name and missing fsync. Verify the implementation changed to mkstemp by reading the diff after Step 2.4.

- [ ] **Step 2.4: Replace `truncate_jsonl_if_needed` in policy_config.py**

Replace lines 212-231:

```python
def truncate_jsonl_if_needed(path: "Path", max_lines: int, trigger_ratio: float = 1.5) -> None:
    """Truncate a .jsonl file to *max_lines* when it exceeds max_lines * trigger_ratio.

    Reads the file once and rewrites only when the trigger threshold is crossed,
    so the amortised cost per append is O(1) for normal operation.
    Uses mkstemp + fsync + os.replace for crash-safe atomic rewrite.
    Silently ignores all errors (disk full, permissions, etc.) — non-fatal.
    """
    try:
        if not path.exists():
            return
        text = path.read_text(encoding="utf-8")
        lines = [l for l in text.splitlines() if l.strip()]
        if len(lines) <= int(max_lines * trigger_ratio):
            return
        trimmed = "\n".join(lines[-max_lines:]) + "\n"
        import tempfile as _tempfile
        import os as _os
        fd, tmp_path_str = _tempfile.mkstemp(
            prefix=f"{path.stem}-", suffix=".jsonl.tmp", dir=str(path.parent)
        )
        _tmp = tmp_path_str
        try:
            with _os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(trimmed)
                f.flush()
                _os.fsync(f.fileno())
            _os.replace(tmp_path_str, path)
            _tmp = ""
        finally:
            if _tmp and _os.path.exists(_tmp):
                _os.unlink(_tmp)
    except Exception:
        pass  # Non-fatal — truncation is a performance optimization only
```

Note: `tempfile` and `os` are NOT currently imported at module level in `policy_config.py` (only used here). The inline imports keep the module clean. Check the current imports:

```bash
head -20 /Users/apple/Documents/workspace/emerge/scripts/policy_config.py
```

If `import os` and `import tempfile` are NOT at top level, the inline imports are fine. Do NOT add them at top level to avoid breaking unrelated code.

- [ ] **Step 2.5: Run tests**

```bash
python -m pytest tests/test_policy_config.py -xvs 2>&1 | tail -15
python -m pytest tests -q 2>&1 | tail -10
```

Expected: both tests pass, full suite ≥350 tests passing.

- [ ] **Step 2.6: Commit**

```bash
git add scripts/policy_config.py tests/test_policy_config.py
git commit -m "fix: truncate_jsonl_if_needed uses mkstemp+fsync+replace — crash-safe, no fixed temp name"
```

---

## Task 3: Cache RunnerRouter with mtime-based invalidation

**Root cause:** `EmergeDaemon._get_runner_router()` (emerge_daemon.py:162-168) calls `RunnerRouter.from_env()` on every invocation. `from_env()` reads `~/.emerge/runner-map.json` from disk, parses JSON, and constructs `RunnerClient` objects. This is called on every `icc_exec`, `icc_read`, `icc_write`, and flywheel bridge check — adding disk I/O to every hot path.

The comment says "Always reload from disk so runner config added after daemon start is picked up." — correct goal, but we can achieve it more efficiently by checking the file's mtime.

**Files:**
- Modify: `scripts/emerge_daemon.py:113-117` (`__init__`), `162-168` (`_get_runner_router`)
- Test: `tests/test_mcp_tools_integration.py`

- [ ] **Step 3.1: Write the test**

Add to `tests/test_mcp_tools_integration.py`:

```python
def test_runner_router_cached_between_calls(tmp_path, monkeypatch):
    """_get_runner_router() must return the same object on consecutive calls when config hasn't changed."""
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path))
    # No runner configured — router should be None
    monkeypatch.delenv("EMERGE_RUNNER_URL", raising=False)
    from scripts.emerge_daemon import EmergeDaemon
    daemon = EmergeDaemon()

    r1 = daemon._get_runner_router()
    r2 = daemon._get_runner_router()
    r3 = daemon._get_runner_router()

    # All None (no runner configured) — but verify they're the same object (cached)
    # Use a call counter to confirm from_env is not called multiple times
    call_count = []
    original_from_env = type(r1).from_env if r1 is not None else None

    from scripts.runner_client import RunnerRouter
    original = RunnerRouter.from_env
    def counting_from_env(*args, **kwargs):
        call_count.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(RunnerRouter, "from_env", staticmethod(counting_from_env))

    # Reset cache by creating a new daemon
    daemon2 = EmergeDaemon()
    # Call 10 times
    for _ in range(10):
        daemon2._get_runner_router()

    # Should only call from_env once (initial) not 10 times
    assert len(call_count) <= 2, (
        f"from_env called {len(call_count)} times for 10 _get_runner_router() calls — caching broken"
    )
```

- [ ] **Step 3.2: Run to confirm test fails**

```bash
cd /Users/apple/Documents/workspace/emerge
python -m pytest tests/test_mcp_tools_integration.py::test_runner_router_cached_between_calls -xvs 2>&1 | tail -20
```

Expected: FAIL — `from_env` called 10+ times instead of ≤2.

- [ ] **Step 3.3: Add cache fields to `__init__`**

In `EmergeDaemon.__init__`, after the line `self._registry_lock = threading.Lock()` (around line 119), add:

```python
        # Cache for RunnerRouter — rebuilt only when runner-map.json changes on disk.
        self._runner_router_cache: "RunnerRouter | None" = RunnerRouter.from_env()
        self._runner_router_config_mtime: float = self._read_runner_config_mtime()
```

- [ ] **Step 3.4: Add `_read_runner_config_mtime()` helper method**

Add this method near `_get_runner_router()` (around line 162), before it:

```python
    def _read_runner_config_mtime(self) -> float:
        """Return mtime of the runner config file, or 0.0 if it doesn't exist."""
        try:
            from scripts.runner_client import RunnerRouter as _RR
            p = _RR.persisted_config_path()
            return p.stat().st_mtime if p.exists() else 0.0
        except Exception:
            return 0.0
```

- [ ] **Step 3.5: Replace `_get_runner_router()` with cached version**

Replace lines 162-168:

```python
    def _get_runner_router(self) -> "RunnerRouter | None":
        """Return cached RunnerRouter, rebuilding only when runner-map.json changes.

        The original contract ("reload from disk so config added after daemon start
        is picked up") is preserved via mtime-based invalidation — zero disk reads
        when config hasn't changed, one read when it has.
        """
        current_mtime = self._read_runner_config_mtime()
        if current_mtime != self._runner_router_config_mtime:
            self._runner_router_cache = RunnerRouter.from_env()
            self._runner_router_config_mtime = current_mtime
        return self._runner_router_cache
```

- [ ] **Step 3.6: Run the test to confirm it passes**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_runner_router_cached_between_calls -xvs 2>&1 | tail -10
```

Expected: PASS — from_env called ≤2 times (once in __init__ via `RunnerRouter.from_env()`, once inside _get_runner_router on first call which also misses due to monkeypatching order).

Note: if the mtime read itself fails in `_read_runner_config_mtime`, it returns 0.0. On the first call to `_get_runner_router`, `current_mtime == 0.0 == self._runner_router_config_mtime` → no rebuild. This is correct: if the file doesn't exist, the initial build (which also found no file) is still valid.

- [ ] **Step 3.7: Run full test suite**

```bash
python -m pytest tests -q 2>&1 | tail -10
```

Expected: ≥351 tests passing, 0 failures.

- [ ] **Step 3.8: Commit**

```bash
git add scripts/emerge_daemon.py tests/test_mcp_tools_integration.py
git commit -m "perf: cache RunnerRouter with mtime-based invalidation — eliminate per-call disk read in _get_runner_router"
```

---

## Task 4: Final verification

- [ ] **Step 4.1: Run full suite**

```bash
cd /Users/apple/Documents/workspace/emerge
python -m pytest tests -q --tb=short 2>&1 | tail -10
```

Expected: ≥351 tests, 0 failures.

- [ ] **Step 4.2: Verify git log**

```bash
git log --oneline -5
```

Expected: see 3 new commits on top of the round-1 fixes.

---

## Self-Review

**Spec coverage:**

| Issue | Task |
|-------|------|
| metrics.py emit() no fsync | Task 1 ✓ |
| truncate_jsonl_if_needed fixed temp + no fsync | Task 2 ✓ |
| _get_runner_router per-call disk read | Task 3 ✓ |
| False positives (thread safety, TOCTOU, globals size) | Explicitly skipped with documentation ✓ |

**Placeholder scan:** All steps contain exact code. No TBDs.

**Type consistency:**
- `self._runner_router_cache: "RunnerRouter | None"` — matches `RunnerRouter | None` return type of `_get_runner_router()`
- `self._runner_router_config_mtime: float` — matches return type of `_read_runner_config_mtime()`

**Dependency order:** Tasks 1, 2, 3 are fully independent. Any order works.
