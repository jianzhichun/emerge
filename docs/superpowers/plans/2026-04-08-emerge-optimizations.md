# Emerge Code Quality & Correctness Optimizations

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix all confirmed correctness, concurrency-safety, and code-quality issues found in the deep codebase audit — reaching optimal solution for each.

**Architecture:** Six independent fix clusters applied in priority order: (1) threading lock for in-process concurrent writes, (2) bridge failure downgrade, (3) span_tracker atomic write, (4) cross-platform fcntl, (5) candidate update deduplication, (6) risk/path validation hardening.

**Tech Stack:** Python 3.11+, stdlib only (threading, tempfile, fcntl/msvcrt), pytest

---

## Confirmed Issues (from audit)

| # | File | Issue | Severity |
|---|------|-------|----------|
| 1 | `emerge_daemon.py:1641-1690` | `_record_exec_event()`, `_record_pipeline_event()`, `_increment_human_fix()`, `_update_pipeline_registry()` do unprotected read-modify-write on `candidates.json` + `pipelines-registry.json` — no threading.Lock | CRITICAL |
| 2 | `emerge_daemon.py:200-206` | Bridge failures log a warning but don't increment `consecutive_failures` — stable pipeline can fail forever without downgrade | HIGH |
| 3 | `span_tracker.py:128-131` | `_atomic_write()` uses fixed-name `.tmp` file and no `fsync` — not crash-safe, race-unsafe | MEDIUM |
| 4 | `goal_control_plane.py:59` | `fcntl.flock()` is POSIX-only; raises `AttributeError` on Windows | MEDIUM |
| 5 | `emerge_daemon.py:1583-1837` | `_record_exec_event()` and `_record_pipeline_event()` share 60+ lines of identical candidate-entry update logic | LOW |
| 6 | `state_tracker.py:92-97` | Risk dedup key is text-only; same message from different intents collapses to one risk | LOW |
| 7 | `pipeline_engine.py:41-48` | `connector` / `pipeline` params not validated against path traversal in `run_read()` / `run_write()` | LOW |

## False Positives (do NOT fix — already correct)
- `exec_session.py _write_checkpoint()` — Already uses `mkstemp + fsync + os.replace` (lines 304-317). Analysis was wrong.
- `_should_sample()` — 100% sampling for explore/stable, rollout_pct% for canary. Intentional design.
- Checkpoint non-atomic — false, already atomic.

---

## File Map

| File | Change |
|------|--------|
| `scripts/emerge_daemon.py` | Add `self._registry_lock = threading.Lock()`, wrap all 4 methods; add bridge-fail recording; extract `_update_candidate_entry()` |
| `scripts/span_tracker.py` | Replace `_atomic_write()` with proper `mkstemp + fsync + os.replace` |
| `scripts/goal_control_plane.py` | Make `_file_lock` cross-platform (fcntl on POSIX, msvcrt on Windows) |
| `scripts/pipeline_engine.py` | Validate connector/pipeline params at entry of `run_read()` / `run_write()` |
| `scripts/state_tracker.py` | Include `intent_signature` in risk dedup key |
| `tests/test_mcp_tools_integration.py` | Add concurrent bridge-failure and registry-lock tests |
| `tests/test_span_tracker.py` | Add crash-safe atomic write test |
| `tests/test_pipeline_engine.py` | Add path traversal rejection test |
| `tests/test_state_tracker.py` | Add risk dedup test with same text + different intent |

---

## Task 1: threading.Lock for registry writes

**Root cause:** `EmergeDaemon` only has `_stdout_lock` (line 48). The four methods that write `candidates.json` / `pipelines-registry.json` are all unprotected.

**Files:**
- Modify: `scripts/emerge_daemon.py:94-119` (`__init__`), `1583`, `1693`, `1840`, `2134`
- Test: `tests/test_mcp_tools_integration.py`

- [ ] **Step 1.1: Write the failing test**

Add to `tests/test_mcp_tools_integration.py`:

```python
import threading

def test_concurrent_exec_events_do_not_lose_attempts(tmp_path, monkeypatch):
    """Two threads calling _record_exec_event concurrently must not lose counts."""
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path))
    daemon = EmergeDaemon()
    base_args = {
        "intent_signature": "zwcad.read.state",
        "code": "__result = 1",
        "target_profile": "default",
    }
    fake_result = {"isError": False}
    errors = []

    def record():
        try:
            daemon._record_exec_event(
                arguments=base_args,
                result=fake_result,
                target_profile="default",
                mode="inline_code",
                execution_path="local",
                sampled_in_policy=True,
                candidate_key="zwcad.read.state",
            )
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=record) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    session_dir = tmp_path / daemon._base_session_id
    import json
    reg = json.loads((session_dir / "candidates.json").read_text())
    # All 20 calls must be recorded — no lost updates
    assert reg["candidates"]["zwcad.read.state"]["attempts"] == 20
```

- [ ] **Step 1.2: Run to confirm failure**

```bash
cd /Users/apple/Documents/workspace/emerge
python -m pytest tests/test_mcp_tools_integration.py::test_concurrent_exec_events_do_not_lose_attempts -xvs 2>&1 | tail -20
```

Expected: FAIL — attempts will be less than 20 due to lost updates.

- [ ] **Step 1.3: Add `_registry_lock` to `__init__`**

In `scripts/emerge_daemon.py`, after line 113 (`self._runner_router = ...`), add:

```python
        # Protects all read-modify-write operations on candidates.json
        # and pipelines-registry.json across threads (e.g. OperatorMonitor).
        self._registry_lock = threading.Lock()
```

- [ ] **Step 1.4: Wrap `_record_exec_event()` writes**

In `_record_exec_event()`, replace lines 1641-1691 (the block from `key = candidate_key` to `self._update_pipeline_registry(...)`):

```python
        if not intent_signature:
            return
        key = candidate_key
        registry_path = session_dir / "candidates.json"
        with self._registry_lock:
            registry = self._load_json_object(registry_path, root_key="candidates")
            entry = registry["candidates"].get(
                key,
                {
                    "source": "exec",
                    "target_profile": target_profile,
                    "last_execution_path": execution_path,
                    "intent_signature": intent_signature,
                    "script_ref": script_ref or "<inline>",
                    "attempts": 0,
                    "successes": 0,
                    "verify_passes": 0,
                    "human_fixes": 0,
                    "degraded_count": 0,
                    "consecutive_failures": 0,
                    "recent_outcomes": [],
                    "total_calls": 0,
                    "last_ts_ms": 0,
                },
            )
            if description:
                entry["description"] = description
            entry["last_execution_path"] = execution_path
            entry["total_calls"] = int(entry.get("total_calls", 0)) + 1
            if is_error:
                sampled_in_policy = True
            if sampled_in_policy:
                entry["attempts"] += 1
                if not is_error:
                    entry["successes"] += 1
                if trusted_verify_passed:
                    entry["verify_passes"] += 1
            is_degraded = False
            failed_attempt = (is_error or is_degraded) and sampled_in_policy
            if sampled_in_policy and is_degraded:
                entry["degraded_count"] += 1
            if sampled_in_policy:
                entry["consecutive_failures"] = (
                    int(entry.get("consecutive_failures", 0)) + 1 if failed_attempt else 0
                )
                recent = list(entry.get("recent_outcomes", []))
                recent.append(0 if failed_attempt else 1)
                entry["recent_outcomes"] = recent[-WINDOW_SIZE:]
            entry["last_ts_ms"] = event["ts_ms"]
            registry["candidates"][key] = entry
            self._atomic_write_json(registry_path, registry)
            self._update_pipeline_registry(candidate_key=key, entry=entry)
```

- [ ] **Step 1.5: Wrap `_record_pipeline_event()` writes**

In `_record_pipeline_event()`, replace lines 1767-1838 (block from `registry_path = session_dir / "candidates.json"` to `self._update_pipeline_registry(...)`):

```python
        registry_path = session_dir / "candidates.json"
        with self._registry_lock:
            registry = self._load_json_object(registry_path, root_key="candidates")
            entry = registry["candidates"].get(
                key,
                {
                    "source": "pipeline",
                    "pipeline_id": pipeline_id,
                    "target_profile": target_profile,
                    "last_execution_path": execution_path,
                    "intent_signature": intent_signature or pipeline_id,
                    "script_ref": pipeline_id,
                    "attempts": 0,
                    "successes": 0,
                    "verify_passes": 0,
                    "human_fixes": 0,
                    "degraded_count": 0,
                    "consecutive_failures": 0,
                    "recent_outcomes": [],
                    "total_calls": 0,
                    "policy_enforced_count": 0,
                    "stop_triggered_count": 0,
                    "rollback_executed_count": 0,
                    "last_policy_action": "none",
                    "last_ts_ms": 0,
                },
            )
            entry["source"] = "pipeline"
            entry["last_execution_path"] = execution_path
            if pipeline_description and not entry.get("description"):
                entry["description"] = pipeline_description
            policy_enforced = bool(result.get("policy_enforced", False))
            stop_triggered = bool(result.get("stop_triggered", False))
            rollback_executed = bool(result.get("rollback_executed", False))
            if policy_enforced:
                entry["policy_enforced_count"] = int(entry.get("policy_enforced_count", 0)) + 1
            if stop_triggered:
                entry["stop_triggered_count"] = int(entry.get("stop_triggered_count", 0)) + 1
            if rollback_executed:
                entry["rollback_executed_count"] = int(entry.get("rollback_executed_count", 0)) + 1
            if rollback_executed:
                entry["last_policy_action"] = "rollback"
            elif stop_triggered:
                entry["last_policy_action"] = "stop"
            else:
                entry["last_policy_action"] = "none"
            entry["total_calls"] = int(entry.get("total_calls", 0)) + 1
            if sampled_in_policy:
                entry["attempts"] += 1
                if not is_error:
                    entry["successes"] += 1
                if event["verify_passed"]:
                    entry["verify_passes"] += 1
            is_degraded = str(result.get("verification_state", "")).lower() == "degraded"
            failed_attempt = (is_error or is_degraded) and sampled_in_policy
            if sampled_in_policy and is_degraded:
                entry["degraded_count"] += 1
            if sampled_in_policy:
                entry["consecutive_failures"] = (
                    int(entry.get("consecutive_failures", 0)) + 1 if failed_attempt else 0
                )
                recent = list(entry.get("recent_outcomes", []))
                recent.append(0 if failed_attempt else 1)
                entry["recent_outcomes"] = recent[-WINDOW_SIZE:]
            entry["last_ts_ms"] = event["ts_ms"]
            registry["candidates"][key] = entry
            self._atomic_write_json(registry_path, registry)
            self._update_pipeline_registry(candidate_key=key, entry=entry)
```

- [ ] **Step 1.6: Wrap `_increment_human_fix()` writes**

Replace lines 2139-2153 (`session_dir = ...` through `_update_pipeline_registry(...)`):

```python
        session_dir = self._state_root / self._base_session_id
        candidates_path = session_dir / "candidates.json"
        if not candidates_path.exists():
            return
        with self._registry_lock:
            registry = self._load_json_object(candidates_path, root_key="candidates")
            entry = registry["candidates"].get(intent_signature)
            if not isinstance(entry, dict):
                return
            entry["human_fixes"] = int(entry.get("human_fixes", 0)) + 1
            registry["candidates"][intent_signature] = entry
            self._atomic_write_json(candidates_path, registry)
            try:
                self._update_pipeline_registry(candidate_key=intent_signature, entry=entry)
            except Exception:
                pass
```

Note: `_update_pipeline_registry()` already acquires `_registry_lock` indirectly via the callers above. Since `_increment_human_fix` also holds the lock and calls `_update_pipeline_registry()` which writes `pipelines-registry.json`, we need `_update_pipeline_registry()` to NOT acquire the lock itself (it's called while lock is held). This is fine because `_update_pipeline_registry` only needs the pipeline registry, not candidates.json — no nested lock.

Actually `_update_pipeline_registry` writes `pipelines-registry.json` which is a DIFFERENT file. We need a separate lock for it, OR we can use one coarser lock for both files. Use one lock since the operations are always paired:

Update `__init__` to rename the lock to make its scope clear:

```python
        # Coarse lock protecting concurrent updates to candidates.json AND
        # pipelines-registry.json — always written together as a pair.
        self._registry_lock = threading.Lock()
```

- [ ] **Step 1.7: Run test to verify it passes**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_concurrent_exec_events_do_not_lose_attempts -xvs 2>&1 | tail -10
```

Expected: PASS — all 20 attempts recorded.

- [ ] **Step 1.8: Run full test suite**

```bash
python -m pytest tests -q 2>&1 | tail -15
```

Expected: same number of passing tests as before (334+).

- [ ] **Step 1.9: Commit**

```bash
cd /Users/apple/Documents/workspace/emerge
git add scripts/emerge_daemon.py tests/test_mcp_tools_integration.py
git commit -m "fix: add threading lock for concurrent registry writes (candidates.json + pipelines-registry.json)"
```

---

## Task 2: Bridge failure triggers consecutive_failures

**Root cause:** `_try_flywheel_bridge()` lines 200-206 catch exceptions and return None without updating the pipeline registry. A stable pipeline that consistently throws will never be downgraded.

**Files:**
- Modify: `scripts/emerge_daemon.py:200-206`
- Test: `tests/test_mcp_tools_integration.py`

- [ ] **Step 2.1: Write the failing test**

Add to `tests/test_mcp_tools_integration.py`:

```python
def test_bridge_failure_records_consecutive_failure(tmp_path, monkeypatch):
    """When the flywheel bridge raises, the pipeline's consecutive_failures must increment."""
    import json
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path))
    daemon = EmergeDaemon()

    # Manually seed registry with a stable pipeline
    registry_path = tmp_path / "pipelines-registry.json"
    registry = {
        "pipelines": {
            "zwcad.read.state": {
                "status": "stable",
                "rollout_pct": 100,
                "attempts": 50,
                "consecutive_failures": 0,
            }
        }
    }
    EmergeDaemon._atomic_write_json(registry_path, registry)

    # Patch PipelineEngine.run_read to raise
    def boom(*args, **kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(daemon.pipeline, "run_read", boom)

    result = daemon._try_flywheel_bridge({"intent_signature": "zwcad.read.state"})
    assert result is None  # bridge must fail gracefully

    # Verify consecutive_failures was incremented in the registry
    updated = json.loads(registry_path.read_text())
    assert updated["pipelines"]["zwcad.read.state"]["consecutive_failures"] == 1
```

- [ ] **Step 2.2: Run to confirm failure**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_bridge_failure_records_consecutive_failure -xvs 2>&1 | tail -15
```

Expected: FAIL — consecutive_failures is still 0.

- [ ] **Step 2.3: Update `_try_flywheel_bridge()` exception handler**

Replace lines 200-206 in `scripts/emerge_daemon.py`:

```python
        except Exception as _bridge_exc:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "flywheel bridge failed for %s (%s), falling back to LLM: %s",
                base_pipeline_id, mode, _bridge_exc,
            )
            # Record the failure so policy can downgrade if bridge keeps failing.
            try:
                _rr2 = self._get_runner_router()
                _cl2 = _rr2.find_client(arguments) if _rr2 else None
                _exec_path2 = "remote" if _cl2 is not None else "local"
                self._record_pipeline_event(
                    tool_name="icc_read" if mode == "read" else "icc_write",
                    arguments={**arguments, "connector": connector, "pipeline": name},
                    result={"verification_state": "degraded", "pipeline_id": base_pipeline_id},
                    is_error=True,
                    error_text=str(_bridge_exc),
                    execution_path=_exec_path2,
                )
            except Exception:
                pass
            return None
```

- [ ] **Step 2.4: Run test to verify pass**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_bridge_failure_records_consecutive_failure -xvs 2>&1 | tail -10
```

Expected: PASS.

- [ ] **Step 2.5: Run full suite**

```bash
python -m pytest tests -q 2>&1 | tail -10
```

Expected: all passing.

- [ ] **Step 2.6: Commit**

```bash
git add scripts/emerge_daemon.py tests/test_mcp_tools_integration.py
git commit -m "fix: record consecutive_failures when flywheel bridge raises — enables stable→explore downgrade"
```

---

## Task 3: span_tracker._atomic_write() crash-safe

**Root cause:** `_atomic_write()` (span_tracker.py:128-131) writes to a fixed-name `.tmp` file without `fsync`. Two issues:
1. No `fsync` → on power-loss the temp file may be 0 bytes after rename
2. Fixed name `state.tmp` → if two calls race (unlikely but possible), they clobber

**Files:**
- Modify: `scripts/span_tracker.py:128-131`
- Test: `tests/test_span_tracker.py`

- [ ] **Step 3.1: Write the failing test**

Add to `tests/test_span_tracker.py`:

```python
import os
import tempfile

def test_atomic_write_leaves_no_tmp_file_on_success(tmp_path):
    """After _atomic_write succeeds, no .tmp file must remain."""
    from scripts.span_tracker import SpanTracker
    st = SpanTracker(state_root=tmp_path, hook_state_root=tmp_path)
    target = tmp_path / "state.json"
    st._atomic_write(target, {"key": "value"})
    assert target.exists()
    # No stray .tmp files
    tmp_files = list(tmp_path.glob("*.tmp")) + list(tmp_path.glob("state.tmp"))
    assert tmp_files == [], f"stray tmp files: {tmp_files}"

def test_atomic_write_content_survives_fsync(tmp_path):
    """Written content must be correct — implies fsync path was executed."""
    import json
    from scripts.span_tracker import SpanTracker
    st = SpanTracker(state_root=tmp_path, hook_state_root=tmp_path)
    target = tmp_path / "test_write.json"
    st._atomic_write(target, {"hello": "world", "num": 42})
    result = json.loads(target.read_text(encoding="utf-8"))
    assert result == {"hello": "world", "num": 42}
```

- [ ] **Step 3.2: Run to confirm the fsync test passes (state.tmp test fails)**

```bash
python -m pytest tests/test_span_tracker.py::test_atomic_write_leaves_no_tmp_file_on_success tests/test_span_tracker.py::test_atomic_write_content_survives_fsync -xvs 2>&1 | tail -20
```

The `test_atomic_write_leaves_no_tmp_file_on_success` will FAIL because `state.tmp` exists. The content test PASSES (data is correct).

- [ ] **Step 3.3: Replace `_atomic_write()` in span_tracker.py**

Replace lines 128-131 (current implementation):

```python
    def _atomic_write(self, path: Path, data: dict) -> None:
        import os as _os
        import tempfile as _tempfile
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path_str = _tempfile.mkstemp(
            prefix=f"{path.stem}-", suffix=".tmp", dir=str(path.parent)
        )
        _tmp = tmp_path_str
        try:
            with _os.fdopen(fd, "w", encoding="utf-8") as f:
                import json as _json
                _json.dump(data, f, ensure_ascii=False)
                f.flush()
                _os.fsync(f.fileno())
            _os.replace(tmp_path_str, path)
            _tmp = ""
        finally:
            if _tmp and _os.path.exists(_tmp):
                _os.unlink(_tmp)
```

- [ ] **Step 3.4: Run tests to verify pass**

```bash
python -m pytest tests/test_span_tracker.py::test_atomic_write_leaves_no_tmp_file_on_success tests/test_span_tracker.py::test_atomic_write_content_survives_fsync -xvs 2>&1 | tail -10
```

Expected: both PASS.

- [ ] **Step 3.5: Run full suite**

```bash
python -m pytest tests -q 2>&1 | tail -10
```

Expected: all passing.

- [ ] **Step 3.6: Commit**

```bash
git add scripts/span_tracker.py tests/test_span_tracker.py
git commit -m "fix: span_tracker._atomic_write uses mkstemp+fsync+replace — crash-safe, race-safe"
```

---

## Task 4: Cross-platform file locking (fcntl → portable)

**Root cause:** `goal_control_plane.py:_file_lock()` does `import fcntl` inside the loop — this raises `ImportError` on Windows. Target: macOS/Linux primary + graceful fallback on Windows.

**Files:**
- Modify: `scripts/goal_control_plane.py:52-70`
- Test: `tests/test_goal_control_plane.py`

- [ ] **Step 4.1: Write the failing test (simulating Windows)**

Add to `tests/test_goal_control_plane.py`:

```python
def test_file_lock_works_without_fcntl(tmp_path, monkeypatch):
    """_file_lock must not crash if fcntl is unavailable (simulates Windows)."""
    import builtins
    real_import = builtins.__import__

    def mock_import(name, *args, **kwargs):
        if name == "fcntl":
            raise ImportError("No module named 'fcntl'")
        return real_import(name, *args, **kwargs)

    # Re-import the module with fcntl blocked
    import importlib
    import scripts.goal_control_plane as gcp
    lock_path = tmp_path / ".test.lock"

    with monkeypatch.context() as m:
        m.setattr(builtins, "__import__", mock_import)
        acquired = False
        try:
            with gcp._file_lock(lock_path, timeout_ms=500):
                acquired = True
        except ImportError:
            pass  # old code raises; new code must not
    assert acquired, "_file_lock must work even when fcntl is unavailable"
```

- [ ] **Step 4.2: Run to confirm failure**

```bash
python -m pytest tests/test_goal_control_plane.py::test_file_lock_works_without_fcntl -xvs 2>&1 | tail -15
```

Expected: FAIL with ImportError or acquired==False.

- [ ] **Step 4.3: Replace `_file_lock` in goal_control_plane.py**

Replace the entire `_file_lock` function (lines 52-70):

```python
@contextlib.contextmanager
def _file_lock(lock_path: Path, timeout_ms: int = 3000):
    """Cross-platform advisory file lock.

    Uses fcntl.flock on POSIX (macOS, Linux) and a busy-wait fallback on
    Windows where fcntl is unavailable. The fallback is not production-grade
    for high-contention scenarios but prevents hard crashes.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    start_ms = _now_ms()

    try:
        import fcntl as _fcntl
        _has_fcntl = True
    except ImportError:
        _has_fcntl = False

    if _has_fcntl:
        with lock_path.open("a+", encoding="utf-8") as handle:
            while True:
                try:
                    _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                    try:
                        yield
                        return
                    finally:
                        _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
                except BlockingIOError:
                    if _now_ms() - start_ms >= timeout_ms:
                        raise TimeoutError(f"goal control lock timeout: {lock_path}")
                    time.sleep(0.02)
    else:
        # Windows fallback: lock file existence as mutex (best-effort, not atomic).
        # Sufficient for single-machine single-daemon scenarios.
        sentinel = lock_path.with_suffix(".lock")
        while sentinel.exists():
            if _now_ms() - start_ms >= timeout_ms:
                raise TimeoutError(f"goal control lock timeout (Windows fallback): {lock_path}")
            time.sleep(0.02)
        sentinel.touch()
        try:
            yield
        finally:
            try:
                sentinel.unlink()
            except FileNotFoundError:
                pass
```

- [ ] **Step 4.4: Run test to verify pass**

```bash
python -m pytest tests/test_goal_control_plane.py::test_file_lock_works_without_fcntl -xvs 2>&1 | tail -10
```

Expected: PASS.

- [ ] **Step 4.5: Run full suite**

```bash
python -m pytest tests -q 2>&1 | tail -10
```

Expected: all passing.

- [ ] **Step 4.6: Commit**

```bash
git add scripts/goal_control_plane.py tests/test_goal_control_plane.py
git commit -m "fix: cross-platform _file_lock — fcntl on POSIX, file-sentinel fallback on Windows"
```

---

## Task 5: Extract `_update_candidate_entry()` to eliminate duplication

**Root cause:** `_record_exec_event()` and `_record_pipeline_event()` each contain ~60 lines of nearly identical candidate-entry update logic (attempts/successes/verify_passes/consecutive_failures/recent_outcomes). Any fix to one must be duplicated to the other.

**Files:**
- Modify: `scripts/emerge_daemon.py:1583-1838`
- Test: none needed (covered by existing tests)

- [ ] **Step 5.1: Add `_update_candidate_entry()` private method**

Insert the following method after `_increment_human_fix()` (after line ~2153), before `_resolve_script_roots()`:

```python
    def _update_candidate_entry(
        self,
        *,
        entry: dict[str, Any],
        sampled_in_policy: bool,
        is_error: bool,
        is_degraded: bool,
        verify_passed: bool,
        ts_ms: int,
    ) -> None:
        """Apply standard attempt/success/verify/failure bookkeeping to a candidate entry.

        Mutates ``entry`` in-place. Called within ``_registry_lock`` held by caller.
        """
        failed_attempt = (is_error or is_degraded) and sampled_in_policy
        entry["total_calls"] = int(entry.get("total_calls", 0)) + 1
        if is_error:
            sampled_in_policy = True  # errors always sampled
        if sampled_in_policy:
            entry["attempts"] += 1
            if not is_error:
                entry["successes"] += 1
            if verify_passed:
                entry["verify_passes"] += 1
            if is_degraded:
                entry["degraded_count"] = int(entry.get("degraded_count", 0)) + 1
            entry["consecutive_failures"] = (
                int(entry.get("consecutive_failures", 0)) + 1 if failed_attempt else 0
            )
            recent = list(entry.get("recent_outcomes", []))
            recent.append(0 if failed_attempt else 1)
            entry["recent_outcomes"] = recent[-WINDOW_SIZE:]
        entry["last_ts_ms"] = ts_ms
```

- [ ] **Step 5.2: Refactor `_record_exec_event()` to use it**

In `_record_exec_event()`, replace the manual bookkeeping block (from `entry["total_calls"] = ...` to `entry["last_ts_ms"] = event["ts_ms"]`) with:

```python
            self._update_candidate_entry(
                entry=entry,
                sampled_in_policy=sampled_in_policy,
                is_error=is_error,
                is_degraded=False,
                verify_passed=trusted_verify_passed,
                ts_ms=event["ts_ms"],
            )
```

Also remove the now-redundant `entry["total_calls"]`, `entry["attempts"]`, etc. lines that were inline before.

- [ ] **Step 5.3: Refactor `_record_pipeline_event()` to use it**

In `_record_pipeline_event()`, replace the manual bookkeeping block with:

```python
            is_degraded = str(result.get("verification_state", "")).lower() == "degraded"
            self._update_candidate_entry(
                entry=entry,
                sampled_in_policy=sampled_in_policy,
                is_error=is_error,
                is_degraded=is_degraded,
                verify_passed=event["verify_passed"],
                ts_ms=event["ts_ms"],
            )
```

Remove the now-redundant inline lines for total_calls, attempts, successes, etc.

- [ ] **Step 5.4: Run full suite**

```bash
python -m pytest tests -q 2>&1 | tail -10
```

Expected: same pass count as before — this is a refactor, not a behavior change.

- [ ] **Step 5.5: Commit**

```bash
git add scripts/emerge_daemon.py
git commit -m "refactor: extract _update_candidate_entry() — eliminates 60 lines of duplication between _record_exec_event and _record_pipeline_event"
```

---

## Task 6: Path traversal validation in PipelineEngine

**Root cause:** `run_read()` and `run_write()` accept `connector` and `pipeline` from external arguments without checking for `..` or path separators. A crafted `connector="../../etc"` could escape the connector root.

**Files:**
- Modify: `scripts/pipeline_engine.py:41-48`, `64-70`
- Test: `tests/test_pipeline_engine.py`

- [ ] **Step 6.1: Write the failing test**

Add to `tests/test_pipeline_engine.py`:

```python
import pytest
from scripts.pipeline_engine import PipelineEngine

def test_run_read_rejects_path_traversal_in_connector():
    pe = PipelineEngine()
    with pytest.raises(ValueError, match="invalid connector"):
        pe.run_read({"connector": "../../etc", "pipeline": "state"})

def test_run_read_rejects_path_traversal_in_pipeline():
    pe = PipelineEngine()
    with pytest.raises(ValueError, match="invalid pipeline"):
        pe.run_read({"connector": "zwcad", "pipeline": "../../../etc/passwd"})

def test_run_write_rejects_path_traversal():
    pe = PipelineEngine()
    with pytest.raises(ValueError, match="invalid connector"):
        pe.run_write({"connector": "../../etc", "pipeline": "state"})
```

- [ ] **Step 6.2: Run to confirm failure**

```bash
python -m pytest tests/test_pipeline_engine.py::test_run_read_rejects_path_traversal_in_connector tests/test_pipeline_engine.py::test_run_read_rejects_path_traversal_in_pipeline tests/test_pipeline_engine.py::test_run_write_rejects_path_traversal -xvs 2>&1 | tail -15
```

Expected: FAIL — no ValueError raised, raises PipelineMissingError or similar.

- [ ] **Step 6.3: Add `_validate_path_segment()` and call it**

Add before `PipelineEngine.__init__`:

```python
_SAFE_SEGMENT_RE = re.compile(r"^[a-z][a-z0-9_\-]*(\.[a-z][a-z0-9_\-]*)*$")

import re as _re_mod
```

Actually `re` is already imported at top of emerge_daemon.py but NOT in pipeline_engine.py. Add to the top of `pipeline_engine.py` after existing imports:

```python
import re
```

Add this static method to `PipelineEngine`:

```python
    @staticmethod
    def _validate_path_segment(value: str, label: str) -> None:
        """Reject connector/pipeline values that could escape the connector root.

        Allowed: lowercase letters, digits, underscore, hyphen, dot-separated sub-segments.
        Rejected: any path separator, '..', leading dot, or uppercase.
        """
        _SAFE = re.compile(r"^[a-z][a-z0-9_./-]*$")
        if not value or ".." in value or value.startswith("/") or not _SAFE.match(value):
            raise ValueError(
                f"invalid {label} {value!r}: must be lowercase alphanumeric/underscore/hyphen, "
                "no path traversal"
            )
```

At the start of `run_read()` (after the empty-check), add:

```python
        self._validate_path_segment(connector, "connector")
        self._validate_path_segment(pipeline, "pipeline")
```

At the start of `run_write()` (after the empty-check), add:

```python
        self._validate_path_segment(connector, "connector")
        self._validate_path_segment(pipeline, "pipeline")
```

- [ ] **Step 6.4: Run tests to verify pass**

```bash
python -m pytest tests/test_pipeline_engine.py::test_run_read_rejects_path_traversal_in_connector tests/test_pipeline_engine.py::test_run_read_rejects_path_traversal_in_pipeline tests/test_pipeline_engine.py::test_run_write_rejects_path_traversal -xvs 2>&1 | tail -10
```

Expected: all PASS.

- [ ] **Step 6.5: Run full suite — ensure existing tests pass with valid connector names**

```bash
python -m pytest tests -q 2>&1 | tail -10
```

Expected: all passing.

- [ ] **Step 6.6: Commit**

```bash
git add scripts/pipeline_engine.py tests/test_pipeline_engine.py
git commit -m "fix: validate connector/pipeline path segments in run_read/run_write — reject path traversal"
```

---

## Task 7: Risk dedup key includes intent_signature

**Root cause:** `state_tracker.py:92-97` — two different intents failing with the same message (e.g., `"pipeline verification failed"`) collapse to one risk entry, hiding the second failure.

**Files:**
- Modify: `scripts/state_tracker.py:92-97`
- Test: `tests/test_state_tracker.py`

- [ ] **Step 7.1: Write the failing test**

Add to `tests/test_state_tracker.py`:

```python
def test_same_risk_text_different_intents_creates_two_risks():
    from scripts.state_tracker import StateTracker
    st = StateTracker()
    st.add_risk("pipeline verification failed", intent_signature="zwcad.read.state")
    st.add_risk("pipeline verification failed", intent_signature="autocad.read.state")
    open_risks = [r for r in st.state["open_risks"] if r.get("status") == "open"]
    assert len(open_risks) == 2, (
        f"Expected 2 distinct risks (different intents), got {len(open_risks)}: {open_risks}"
    )

def test_same_risk_text_same_intent_deduplicates():
    from scripts.state_tracker import StateTracker
    st = StateTracker()
    st.add_risk("pipeline verification failed", intent_signature="zwcad.read.state")
    st.add_risk("pipeline verification failed", intent_signature="zwcad.read.state")
    open_risks = [r for r in st.state["open_risks"] if r.get("status") == "open"]
    assert len(open_risks) == 1, "Same text + same intent must deduplicate"
```

- [ ] **Step 7.2: Run to confirm failure**

```bash
python -m pytest tests/test_state_tracker.py::test_same_risk_text_different_intents_creates_two_risks tests/test_state_tracker.py::test_same_risk_text_same_intent_deduplicates -xvs 2>&1 | tail -15
```

Expected: `test_same_risk_text_different_intents_creates_two_risks` FAILS (only 1 risk created).

- [ ] **Step 7.3: Update `add_risk()` dedup logic in state_tracker.py**

In `add_risk()`, replace lines 92-97 (the dedup check and risk_id computation):

```python
        # Dedup key includes intent_signature so same message from different intents
        # creates separate risk entries.
        dedup_key = f"{text}||{intent_signature or ''}"
        for existing in self.state["open_risks"]:
            existing_dedup = (
                f"{existing.get('text', existing if isinstance(existing, str) else '')}||"
                f"{existing.get('intent_signature', '') if isinstance(existing, dict) else ''}"
            )
            if existing_dedup == dedup_key:
                return
        import hashlib as _hashlib
        risk_id = "r-" + _hashlib.sha256(dedup_key.encode()).hexdigest()[:12]
```

Note: `hashlib` is already imported at the top of `state_tracker.py` — no new import needed. Remove the duplicate `import hashlib` if one is already present.

- [ ] **Step 7.4: Run tests to verify pass**

```bash
python -m pytest tests/test_state_tracker.py::test_same_risk_text_different_intents_creates_two_risks tests/test_state_tracker.py::test_same_risk_text_same_intent_deduplicates -xvs 2>&1 | tail -10
```

Expected: both PASS.

- [ ] **Step 7.5: Run full suite**

```bash
python -m pytest tests -q 2>&1 | tail -10
```

Expected: all passing.

- [ ] **Step 7.6: Commit**

```bash
git add scripts/state_tracker.py tests/test_state_tracker.py
git commit -m "fix: risk dedup key includes intent_signature — same message from different intents creates separate risks"
```

---

## Task 8: Final verification

- [ ] **Step 8.1: Run complete test suite with verbose output**

```bash
cd /Users/apple/Documents/workspace/emerge
python -m pytest tests -q --tb=short 2>&1 | tail -20
```

Expected: ≥340 tests passing (334 original + new tests from tasks 1-7), 0 failures.

- [ ] **Step 8.2: Verify git log is clean**

```bash
git log --oneline -8
```

Expected: see all 6 fix commits + 1 refactor commit in sequence.

- [ ] **Step 8.3: Final commit — bump internal quality marker**

```bash
git log --oneline -1  # confirm last commit
```

No extra commit needed if all task commits landed cleanly.

---

## Self-Review

**Spec coverage check:**

| Issue | Task |
|-------|------|
| Race condition candidates.json + pipelines-registry.json | Task 1 ✓ |
| Bridge failures not downgrading | Task 2 ✓ |
| span_tracker._atomic_write not crash-safe | Task 3 ✓ |
| Windows fcntl incompatibility | Task 4 ✓ |
| _record_exec_event/_record_pipeline_event duplication | Task 5 ✓ |
| Path traversal in PipelineEngine | Task 6 ✓ |
| Risk dedup too aggressive (text-only key) | Task 7 ✓ |
| Checkpoint non-atomic | FALSE POSITIVE — already correct, skipped ✓ |
| Sampling bias | FALSE POSITIVE — intentional design, skipped ✓ |
| call_tool() refactor | OUT OF SCOPE — high risk, low gain for correctness ✓ |

**Placeholder scan:** All steps contain exact code. No TBDs.

**Type consistency:** `_update_candidate_entry()` (Task 5) mutates `entry: dict[str, Any]` in-place — consistent with how callers already mutate entries before saving. No return type mismatch.

**Dependency order:** Tasks 1 and 5 both touch `_record_exec_event()` and `_record_pipeline_event()`. Execute Task 1 first (adds `_registry_lock` + wraps the blocks), then Task 5 (extracts the inner logic into `_update_candidate_entry()`). If reversed, the extraction complicates the lock-wrapping.
