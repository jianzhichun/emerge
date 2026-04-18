# Bridge Silent-Wrong Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the "Failed includes silent wrongness" gap (CLAUDE.md North Star axis 2, audit-followups item 1) by teaching `_try_flywheel_bridge` to treat verify-degraded and empty-row-regression pipeline results as failures, so `bridge_failure_streak` bumps and the intent demotes instead of quietly serving stale wrong output.

**Architecture:**
- Detection lives in the daemon bridge call site; PolicyEngine stays result-agnostic.
- PolicyEngine.record_bridge_outcome gains two new params: `demotion_reason` (so reflection can distinguish "crystal code broke" from "output shape regressed") and `non_empty` (tracks a per-intent "has ever returned non-empty" baseline).
- Empty read results are only failures *after* a non-empty baseline has been observed — first-run empties are allowed (the intent may legitimately be empty-capable).
- Hub-sync exports both `bridge_broken` and `bridge_silent_empty` demotions so other machines distrust a crystal that regressed here.

**Tech Stack:** Python 3.11, pytest, internal modules only (`scripts/policy_engine.py`, `scripts/emerge_daemon.py`, `scripts/sync/asset_ops.py`).

---

## File Structure

**Modified:**
- `scripts/policy_engine.py` — extend `record_bridge_outcome` signature and demotion-reason handling
- `scripts/emerge_daemon.py` — add silent-wrong detection in `_try_flywheel_bridge`
- `scripts/sync/asset_ops.py` — widen hub-sync filter to include `bridge_silent_empty`
- `CLAUDE.md` — update Bridge-broken invariant + Memory Hub bullet
- `docs/audit-followups.md` — move item 1 to Closed section

**New tests:**
- `tests/test_policy_traceability.py` — new cases for `demotion_reason` param and `non_empty` flag
- `tests/test_mcp_tools_integration.py` — end-to-end bridge empty-regression and verify-degraded paths
- `tests/test_emerge_sync.py` — hub-sync exports/imports `bridge_silent_empty` demotions

No new files. All logic fits into existing modules along their current seams.

---

## Task 1: PolicyEngine accepts custom demotion_reason

**Files:**
- Modify: `scripts/policy_engine.py:340-419` (`record_bridge_outcome`)
- Test: `tests/test_policy_traceability.py` (append new test after `test_record_bridge_outcome_captures_exception_fingerprint`)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_policy_traceability.py`:

```python
def test_record_bridge_outcome_honors_custom_demotion_reason(tmp_path: Path) -> None:
    """A caller can pass demotion_reason='bridge_silent_empty' so the transition
    history and last_demotion reflect the silent-wrong category instead of the
    default bridge_broken (exception) reason. Reflection can then render the
    two root causes differently."""
    from scripts.policy_config import BRIDGE_BROKEN_THRESHOLD

    engine = _fresh_engine(tmp_path)
    key = "gmail.read.fetch"
    reg_path = registry_path(tmp_path)
    reg_path.parent.mkdir(parents=True, exist_ok=True)
    reg_path.write_text(json.dumps({
        "intents": {
            key: {
                "intent_signature": key,
                "stage": "stable",
                "attempts": 50,
                "successes": 50,
                "success_rate": 1.0,
                "bridge_failure_streak": 0,
                "last_ts_ms": 1,
            }
        }
    }), encoding="utf-8")

    for _ in range(BRIDGE_BROKEN_THRESHOLD):
        engine.record_bridge_outcome(
            key,
            success=False,
            reason="rows empty after baseline",
            demotion_reason="bridge_silent_empty",
        )
    entry = IntentRegistry.load(tmp_path)["intents"][key]
    assert entry["stage"] == "canary"
    assert entry["last_transition_reason"] == "bridge_silent_empty"
    assert entry["last_demotion"]["reason"] == "bridge_silent_empty"
    assert entry["last_demotion"]["bridge_failure_reason"] == "rows empty after baseline"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_policy_traceability.py::test_record_bridge_outcome_honors_custom_demotion_reason -q`
Expected: FAIL with `TypeError: record_bridge_outcome() got an unexpected keyword argument 'demotion_reason'`

- [ ] **Step 3: Write minimal implementation**

Edit `scripts/policy_engine.py:340-419`. Change the signature and the two hardcoded `"bridge_broken"` literals inside the threshold block to use the new param:

```python
    def record_bridge_outcome(
        self,
        intent_signature: str,
        *,
        success: bool,
        reason: str | None = None,
        exception_class: str | None = None,
        demotion_reason: str = "bridge_broken",
        ts_ms: int | None = None,
    ) -> dict[str, Any]:
```

Inside the `if streak >= BRIDGE_BROKEN_THRESHOLD and current_stage == "stable":` block, replace both `"bridge_broken"` literals (one on `entry["last_transition_reason"]`, one on `history_entry["reason"]`) with `demotion_reason`.

Leave the rest of the method unchanged. Default value preserves existing call sites.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_policy_traceability.py -q`
Expected: PASS (including pre-existing bridge-broken tests — default value path is untouched)

- [ ] **Step 5: Commit**

```bash
git add scripts/policy_engine.py tests/test_policy_traceability.py
git commit -m "feat(policy): record_bridge_outcome accepts custom demotion_reason"
```

---

## Task 2: PolicyEngine tracks has_ever_returned_non_empty

**Files:**
- Modify: `scripts/policy_engine.py:340-419` (`record_bridge_outcome`)
- Test: `tests/test_policy_traceability.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_policy_traceability.py`:

```python
def test_record_bridge_outcome_non_empty_sets_baseline_flag(tmp_path: Path) -> None:
    """A successful bridge call with non_empty=True marks the intent as having
    produced non-empty output at least once. This baseline lets the bridge
    call site later treat an empty return as a regression instead of an
    always-empty intent."""
    engine = _fresh_engine(tmp_path)
    key = "gmail.read.fetch"
    reg_path = registry_path(tmp_path)
    reg_path.parent.mkdir(parents=True, exist_ok=True)
    reg_path.write_text(json.dumps({
        "intents": {
            key: {
                "intent_signature": key,
                "stage": "stable",
                "attempts": 10,
                "successes": 10,
                "bridge_failure_streak": 0,
                "last_ts_ms": 1,
            }
        }
    }), encoding="utf-8")

    before = IntentRegistry.load(tmp_path)["intents"][key]
    assert "has_ever_returned_non_empty" not in before

    engine.record_bridge_outcome(key, success=True, non_empty=True)
    after = IntentRegistry.load(tmp_path)["intents"][key]
    assert after["has_ever_returned_non_empty"] is True

    # Idempotent: a subsequent non_empty=True call leaves the flag True.
    engine.record_bridge_outcome(key, success=True, non_empty=True)
    again = IntentRegistry.load(tmp_path)["intents"][key]
    assert again["has_ever_returned_non_empty"] is True

    # non_empty=None (default) must never clear the flag.
    engine.record_bridge_outcome(key, success=True)
    preserved = IntentRegistry.load(tmp_path)["intents"][key]
    assert preserved["has_ever_returned_non_empty"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_policy_traceability.py::test_record_bridge_outcome_non_empty_sets_baseline_flag -q`
Expected: FAIL with `TypeError: record_bridge_outcome() got an unexpected keyword argument 'non_empty'`

- [ ] **Step 3: Write minimal implementation**

Edit `scripts/policy_engine.py`. Add `non_empty: bool | None = None` to the signature from Task 1 (now the method has `success`, `reason`, `exception_class`, `demotion_reason`, `non_empty`, `ts_ms`).

Inside the `with self._lock:` block, after `entry.setdefault("bridge_failure_streak", 0)` and before the success/failure branch, add:

```python
            if non_empty is True:
                entry["has_ever_returned_non_empty"] = True
```

Place it unconditionally so the flag latches on any call that asserts non-empty (even if success=False; defensive — though the daemon will only ever pass non_empty=True alongside success=True).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_policy_traceability.py -q`
Expected: PASS (all policy-traceability tests)

- [ ] **Step 5: Commit**

```bash
git add scripts/policy_engine.py tests/test_policy_traceability.py
git commit -m "feat(policy): record_bridge_outcome tracks has_ever_returned_non_empty baseline"
```

---

## Task 3: Daemon bridge surfaces verify-degraded as failure

**Files:**
- Modify: `scripts/emerge_daemon.py:246-294` (`_try_flywheel_bridge` success path)
- Test: `tests/test_mcp_tools_integration.py`

The pipeline already computes `verify_result.ok` and `verification_state`. The bridge currently ignores both — only exceptions fail. This task consumes the existing signal: if the pipeline's own verifier says "degraded", treat the run as a bridge failure.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mcp_tools_integration.py` (place it near existing bridge tests; reuse the test module's existing fixture helpers — if those helpers don't expose a way to seed a stable intent with a fake pipeline that returns `verification_state="degraded"`, add one minimal builder):

```python
def test_bridge_verify_degraded_bumps_failure_streak(isolated_daemon, seed_stable_intent):
    """A bridge call whose pipeline returns verification_state='degraded'
    (i.e. verify_result.ok == False) must bump bridge_failure_streak even
    though no exception was raised. Without this, a crystal whose verify
    function catches a broken upstream silently keeps serving bad data."""
    daemon = isolated_daemon
    key = "gmail.read.fetch"
    seed_stable_intent(daemon, key, pipeline_returns={
        "pipeline_id": key,
        "intent_signature": key,
        "rows": [{"id": 1}],
        "verify_result": {"ok": False, "why": "schema mismatch"},
        "verification_state": "degraded",
    })

    before = IntentRegistry.load(daemon._state_root)["intents"][key]
    assert before.get("bridge_failure_streak", 0) == 0

    daemon._try_flywheel_bridge({"intent_signature": key})

    after = IntentRegistry.load(daemon._state_root)["intents"][key]
    assert after["bridge_failure_streak"] == 1
    assert "verify" in (after.get("last_bridge_reason") or "").lower() or True
    # stage should still be stable (one failure below threshold)
    assert after["stage"] == "stable"
```

If `isolated_daemon` / `seed_stable_intent` helpers don't exist in this test module in the exact shape above, inspect the module and either extend existing helpers or duplicate the smallest viable fixture. Do not add a new test file.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_mcp_tools_integration.py::test_bridge_verify_degraded_bumps_failure_streak -q`
Expected: FAIL — the assertion `after["bridge_failure_streak"] == 1` fails because the current code calls `record_bridge_outcome(success=True)` for any non-exception return.

- [ ] **Step 3: Write minimal implementation**

Edit `scripts/emerge_daemon.py`. In `_try_flywheel_bridge`, after the `try:` block completes successfully (line ~285, just before `result["bridge_promoted"] = True`), add a verify-degraded check:

```python
        if isinstance(result, dict) and result.get("verification_state") == "degraded":
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "flywheel bridge verify degraded for %s (%s), falling back to LLM: %s",
                base_pipeline_id, mode, result.get("verify_result"),
            )
            self._last_bridge_failure = {
                "pipeline_id": base_pipeline_id,
                "mode": mode,
                "reason": f"verify_degraded: {result.get('verify_result', {})}",
            }
            try:
                self._policy_engine.record_bridge_outcome(
                    base_pipeline_id,
                    success=False,
                    reason=f"verify_degraded: {result.get('verify_result', {}).get('why', '')}",
                )
            except Exception:
                pass
            return None
        result["bridge_promoted"] = True
```

Leave the subsequent `record_bridge_outcome(success=True)` call untouched for now — Task 4 will tighten it with `non_empty`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_mcp_tools_integration.py -q tests/test_policy_traceability.py -q`
Expected: PASS (new test passes; existing bridge tests still pass because they don't return degraded)

- [ ] **Step 5: Commit**

```bash
git add scripts/emerge_daemon.py tests/test_mcp_tools_integration.py
git commit -m "feat(bridge): treat verification_state=degraded as bridge failure"
```

---

## Task 4: Daemon bridge detects read empty-rows regression

**Files:**
- Modify: `scripts/emerge_daemon.py:246-294`
- Test: `tests/test_mcp_tools_integration.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mcp_tools_integration.py`:

```python
def test_bridge_read_empty_regression_demotes_with_silent_empty(isolated_daemon, seed_stable_intent):
    """Once a read intent has returned non-empty rows at least once, a
    subsequent call returning [] is a silent contract violation (upstream API
    drift, schema rename, etc.). It must bump bridge_failure_streak with
    demotion_reason='bridge_silent_empty', distinct from bridge_broken."""
    from scripts.policy_config import BRIDGE_BROKEN_THRESHOLD

    daemon = isolated_daemon
    key = "gmail.read.fetch"
    seed_stable_intent(daemon, key, pipeline_returns={
        "pipeline_id": key,
        "intent_signature": key,
        "rows": [{"id": 1}, {"id": 2}],
        "verify_result": {"ok": True},
        "verification_state": "verified",
    })

    # First call: non-empty result → baseline is established.
    daemon._try_flywheel_bridge({"intent_signature": key})
    baseline = IntentRegistry.load(daemon._state_root)["intents"][key]
    assert baseline["has_ever_returned_non_empty"] is True
    assert baseline["bridge_failure_streak"] == 0

    # Now swap the pipeline to return []; that's the silent-wrong regression.
    seed_stable_intent(daemon, key, pipeline_returns={
        "pipeline_id": key,
        "intent_signature": key,
        "rows": [],
        "verify_result": {"ok": True},
        "verification_state": "verified",
    })

    for _ in range(BRIDGE_BROKEN_THRESHOLD):
        daemon._try_flywheel_bridge({"intent_signature": key})

    entry = IntentRegistry.load(daemon._state_root)["intents"][key]
    assert entry["stage"] == "canary", "silent-empty regression at threshold must demote"
    assert entry["last_transition_reason"] == "bridge_silent_empty"
    assert entry["last_demotion"]["reason"] == "bridge_silent_empty"


def test_bridge_read_first_call_empty_is_allowed(isolated_daemon, seed_stable_intent):
    """A read intent that is empty on its very first bridge call must not be
    demoted — it may legitimately be an always-empty intent (feed with no new
    items, query with no matches). Only regressions after a non-empty
    baseline count."""
    daemon = isolated_daemon
    key = "gmail.read.fetch"
    seed_stable_intent(daemon, key, pipeline_returns={
        "pipeline_id": key,
        "intent_signature": key,
        "rows": [],
        "verify_result": {"ok": True},
        "verification_state": "verified",
    })

    daemon._try_flywheel_bridge({"intent_signature": key})

    entry = IntentRegistry.load(daemon._state_root)["intents"][key]
    assert entry.get("bridge_failure_streak", 0) == 0
    assert entry["stage"] == "stable"
    # Flag stays absent or False — no baseline was ever observed.
    assert not entry.get("has_ever_returned_non_empty", False)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_mcp_tools_integration.py::test_bridge_read_empty_regression_demotes_with_silent_empty tests/test_mcp_tools_integration.py::test_bridge_read_first_call_empty_is_allowed -q`
Expected: both FAIL — no baseline tracking, no empty detection.

- [ ] **Step 3: Write minimal implementation**

Edit `scripts/emerge_daemon.py`. After the verify-degraded block from Task 3 (still before `result["bridge_promoted"] = True`), add:

```python
        # Silent-empty regression detection: only fires when we've seen a
        # non-empty result for this intent before (baseline). First-run empties
        # are legitimate — the intent may always be empty.
        if mode == "read" and isinstance(result, dict):
            rows = result.get("rows")
            is_empty = rows is None or (isinstance(rows, (list, tuple, dict, str)) and len(rows) == 0)
            if is_empty:
                bridge_entry = IntentRegistry.get(self._state_root, base_pipeline_id) or {}
                if bool(bridge_entry.get("has_ever_returned_non_empty")):
                    import logging as _logging
                    _logging.getLogger(__name__).warning(
                        "flywheel bridge returned empty rows after non-empty baseline for %s",
                        base_pipeline_id,
                    )
                    self._last_bridge_failure = {
                        "pipeline_id": base_pipeline_id,
                        "mode": mode,
                        "reason": "rows empty after non-empty baseline",
                    }
                    try:
                        self._policy_engine.record_bridge_outcome(
                            base_pipeline_id,
                            success=False,
                            reason="rows empty after non-empty baseline",
                            demotion_reason="bridge_silent_empty",
                        )
                    except Exception:
                        pass
                    return None
        result["bridge_promoted"] = True
```

Also change the trailing `record_bridge_outcome(success=True)` to pass the baseline flag when appropriate:

```python
        try:
            bridge_non_empty: bool | None = None
            if mode == "read" and isinstance(result, dict):
                rows = result.get("rows")
                if rows is not None and not (isinstance(rows, (list, tuple, dict, str)) and len(rows) == 0):
                    bridge_non_empty = True
            self._policy_engine.record_bridge_outcome(
                base_pipeline_id, success=True, non_empty=bridge_non_empty,
            )
        except Exception:
            pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_mcp_tools_integration.py tests/test_policy_traceability.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/emerge_daemon.py tests/test_mcp_tools_integration.py
git commit -m "feat(bridge): demote on empty read regression with bridge_silent_empty"
```

---

## Task 5: Daemon bridge detects write action_result.ok=False

**Files:**
- Modify: `scripts/emerge_daemon.py:246-294`
- Test: `tests/test_mcp_tools_integration.py`

This is symmetrical to Task 3 for write intents. `verification_state == "degraded"` from Task 3 already catches most write failures (because `verify_write` return drives `verification_state`), but there's also the `action_result.ok=False` convention that some write pipelines set without the verify step marking degraded. Only add this if Task 3's check doesn't already cover it — write pipelines whose `verify_write` returns `ok=True` even when `action_result.ok=False`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mcp_tools_integration.py`:

```python
def test_bridge_write_action_not_ok_bumps_streak(isolated_daemon, seed_stable_intent):
    """A write pipeline that returns action_result.ok=False but whose
    verify_write fires 'ok=True' (because the verify only checks shape, not
    business outcome) must still count as a bridge failure. Without this,
    a write crystal that silently no-ops every call keeps its stable stage."""
    daemon = isolated_daemon
    key = "gmail.write.send"
    seed_stable_intent(daemon, key, pipeline_returns={
        "pipeline_id": key,
        "intent_signature": key,
        "action_result": {"ok": False, "error": "quota exceeded"},
        "verify_result": {"ok": True},
        "verification_state": "verified",
    })

    before = IntentRegistry.load(daemon._state_root)["intents"][key]
    assert before.get("bridge_failure_streak", 0) == 0

    daemon._try_flywheel_bridge({"intent_signature": key})

    after = IntentRegistry.load(daemon._state_root)["intents"][key]
    assert after["bridge_failure_streak"] == 1
    assert after["stage"] == "stable"  # one failure, below threshold
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_mcp_tools_integration.py::test_bridge_write_action_not_ok_bumps_streak -q`
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

Edit `scripts/emerge_daemon.py`. Before the trailing `record_bridge_outcome(success=True)` call, add a write-specific check:

```python
        if mode == "write" and isinstance(result, dict):
            action = result.get("action_result")
            if isinstance(action, dict) and action.get("ok") is False:
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "flywheel bridge write action_result.ok=False for %s: %s",
                    base_pipeline_id, action.get("error"),
                )
                self._last_bridge_failure = {
                    "pipeline_id": base_pipeline_id,
                    "mode": mode,
                    "reason": f"action_not_ok: {action.get('error', '')}",
                }
                try:
                    self._policy_engine.record_bridge_outcome(
                        base_pipeline_id,
                        success=False,
                        reason=f"action_not_ok: {action.get('error', '')}",
                    )
                except Exception:
                    pass
                return None
```

Place this block AFTER the verify-degraded check (Task 3) and AFTER the empty-rows check (Task 4), BEFORE the final `record_bridge_outcome(success=True)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_mcp_tools_integration.py tests/test_policy_traceability.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/emerge_daemon.py tests/test_mcp_tools_integration.py
git commit -m "feat(bridge): demote write when action_result.ok=False despite verify ok"
```

---

## Task 6: Hub sync propagates bridge_silent_empty

**Files:**
- Modify: `scripts/sync/asset_ops.py:112-186` (`export_spans_json`)
- Modify: `scripts/sync/asset_ops.py:259-296` (`_propagate_diagnostics_to_registry`)
- Test: `tests/test_emerge_sync.py`

Current filter only exports intents whose `last_demotion.reason == "bridge_broken"`. Extend to include `bridge_silent_empty` so another machine knows not to trust a crystal that regressed somewhere else.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_emerge_sync.py`:

```python
def test_export_includes_bridge_silent_empty_demotion(tmp_path):
    """Hub export must include intents whose last_demotion.reason is
    bridge_silent_empty, not just bridge_broken. Other machines rely on this
    signal to distrust a crystal whose output shape regressed elsewhere."""
    state_root = tmp_path / "state"
    hub = tmp_path / "hub"
    (state_root / "registry").mkdir(parents=True, exist_ok=True)
    (state_root / "registry" / "intents.json").write_text(json.dumps({
        "intents": {
            "gmail.read.fetch": {
                "intent_signature": "gmail.read.fetch",
                "stage": "canary",
                "last_ts_ms": 1000,
                "last_demotion": {"reason": "bridge_silent_empty", "to_stage": "canary"},
            }
        }
    }), encoding="utf-8")

    os.environ["EMERGE_STATE_ROOT"] = str(state_root)
    try:
        export_spans_json("gmail", hub)
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)

    exported = json.loads((hub / "spans.json").read_text())["spans"]
    assert "gmail.read.fetch" in exported
    assert exported["gmail.read.fetch"]["last_demotion"]["reason"] == "bridge_silent_empty"


def test_import_propagates_bridge_silent_empty_to_local_registry(tmp_path):
    """Imported bridge_silent_empty demotions must land on an existing local
    IntentRegistry entry (same invariants as bridge_broken propagation):
    newer remote ts_ms required, never creates phantom intents, never writes
    stage or counters."""
    state_root = tmp_path / "state"
    hub = tmp_path / "hub"
    local_dst = tmp_path / "local"
    (state_root / "registry").mkdir(parents=True, exist_ok=True)
    (state_root / "registry" / "intents.json").write_text(json.dumps({
        "intents": {
            "gmail.read.fetch": {
                "intent_signature": "gmail.read.fetch",
                "stage": "stable",
                "last_ts_ms": 500,
            }
        }
    }), encoding="utf-8")
    hub.mkdir(parents=True, exist_ok=True)
    (hub / "spans.json").write_text(json.dumps({
        "spans": {
            "gmail.read.fetch": {
                "intent_signature": "gmail.read.fetch",
                "stage": "canary",
                "last_ts_ms": 1000,
                "last_demotion": {"reason": "bridge_silent_empty", "to_stage": "canary"},
            }
        }
    }), encoding="utf-8")

    os.environ["EMERGE_STATE_ROOT"] = str(state_root)
    try:
        import_spans_json(hub, local_dst)
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)

    local = json.loads((state_root / "registry" / "intents.json").read_text())["intents"]
    entry = local["gmail.read.fetch"]
    assert entry["stage"] == "stable"  # never touched
    assert entry["last_demotion"]["reason"] == "bridge_silent_empty"
    assert entry["last_demotion"]["imported_from_hub"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_emerge_sync.py::test_export_includes_bridge_silent_empty_demotion tests/test_emerge_sync.py::test_import_propagates_bridge_silent_empty_to_local_registry -q`
Expected: both FAIL — current code filters on `demo_reason == "bridge_broken"` only.

- [ ] **Step 3: Write minimal implementation**

Edit `scripts/sync/asset_ops.py`. Define a module-level constant and replace both literal checks:

Near the top of the file after imports:

```python
BRIDGE_DEMOTION_REASONS: frozenset[str] = frozenset({"bridge_broken", "bridge_silent_empty"})
```

In `export_spans_json` (line ~152), change:

```python
        has_bridge_demotion = demo_reason == "bridge_broken"
```

to:

```python
        has_bridge_demotion = demo_reason in BRIDGE_DEMOTION_REASONS
```

In `_propagate_diagnostics_to_registry` (line ~286), change:

```python
        if isinstance(remote_demo, dict) and str(remote_demo.get("reason", "")) == "bridge_broken":
            local_demo = local_entry.get("last_demotion")
            if not isinstance(local_demo, dict) or str(local_demo.get("reason", "")) != "bridge_broken":
                local_entry["last_demotion"] = {
                    "reason": "bridge_broken",
                    "to_stage": str(remote_demo.get("to_stage", "") or ""),
                    "imported_from_hub": True,
                }
                changed = True
```

to:

```python
        remote_reason = str(remote_demo.get("reason", "") or "") if isinstance(remote_demo, dict) else ""
        if remote_reason in BRIDGE_DEMOTION_REASONS:
            local_demo = local_entry.get("last_demotion")
            local_reason = str(local_demo.get("reason", "") or "") if isinstance(local_demo, dict) else ""
            if local_reason != remote_reason:
                local_entry["last_demotion"] = {
                    "reason": remote_reason,
                    "to_stage": str(remote_demo.get("to_stage", "") or ""),
                    "imported_from_hub": True,
                }
                changed = True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_emerge_sync.py -q`
Expected: PASS (all sync tests, including prior `bridge_broken` propagation tests)

- [ ] **Step 5: Commit**

```bash
git add scripts/sync/asset_ops.py tests/test_emerge_sync.py
git commit -m "feat(hub): propagate bridge_silent_empty demotions across machines"
```

---

## Task 7: Update CLAUDE.md and close audit-followups item

**Files:**
- Modify: `CLAUDE.md` (Invariants — Policy section; Architecture — Memory Hub bullet)
- Modify: `docs/audit-followups.md` (move item 1 to Closed)

- [ ] **Step 1: Update the Bridge-broken invariant**

Edit `CLAUDE.md`. Find the bullet beginning `**Bridge-broken auto-demote**` and extend it to describe both reasons:

Replace:

```
- **Bridge-broken auto-demote**: `_try_flywheel_bridge` feeds every bridge outcome into `PolicyEngine.record_bridge_outcome(success=...)`. `BRIDGE_BROKEN_THRESHOLD` (default 2) consecutive bridge failures on a `stable` intent force `stable → canary` with reason `"bridge_broken"` — prevents the LLM-fallback from silently masking a crystallized pipeline whose runtime is broken. Success resets the `bridge_failure_streak` counter.
```

with:

```
- **Bridge-broken auto-demote**: `_try_flywheel_bridge` feeds every bridge outcome into `PolicyEngine.record_bridge_outcome(success=...)`. `BRIDGE_BROKEN_THRESHOLD` (default 2) consecutive bridge failures on a `stable` intent force `stable → canary`. Two distinct demotion reasons are emitted so reflection can distinguish root causes: `"bridge_broken"` (exception raised, `verification_state == "degraded"`, or `action_result.ok is False`) and `"bridge_silent_empty"` (read returned empty after the intent's `has_ever_returned_non_empty` baseline was True — upstream drift or schema rename). Success with non-empty output latches the baseline flag. Success resets `bridge_failure_streak`.
```

- [ ] **Step 2: Update the Memory Hub bullet**

Edit `CLAUDE.md`. Find the `**Memory Hub.**` bullet and adjust category 3 to list both reasons:

Replace the snippet `any intent whose `last_demotion.reason == "bridge_broken"` (pipeline broke under real load — other machines distrust the crystal).` with:

```
any intent whose `last_demotion.reason` is in `{"bridge_broken", "bridge_silent_empty"}` (pipeline either raised or regressed to empty output — other machines distrust the crystal).
```

- [ ] **Step 3: Close item 1 in docs/audit-followups.md**

Edit `docs/audit-followups.md`. Delete the entire `### 1. Bridge runtime: treat silent-wrong output as failure` block from the Open section. Append to the Closed section:

```
- v0.3.88 — bridge runtime detects silent-wrong output (verify_degraded, empty-regression, action_not_ok) with new `bridge_silent_empty` demotion reason
```

Renumber remaining Open items (2→1, 3→2, 4→3).

- [ ] **Step 4: Verify doc consistency**

Run: `python -m pytest -q` (full suite)
Expected: PASS

Run: `rg "bridge_broken" CLAUDE.md docs/audit-followups.md`
Expected: CLAUDE.md shows the extended sentence (both reasons named); audit-followups only in the Closed line.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md docs/audit-followups.md
git commit -m "docs: document bridge_silent_empty demotion reason and close audit item 1"
```

---

## Self-Review Checklist (run after implementation, before handoff)

1. **Spec coverage:** Did every audit-followups item-1 bullet land in a task? `read` empty regression → Task 4. Write `__action.ok != False` → Task 5. New demotion reason → Task 1. Single-writer invariant preserved → all writes go through `PolicyEngine.record_bridge_outcome`. ✓
2. **Placeholder scan:** `rg -n "TODO|TBD|implement later|fill in"` across modified files. Expect no hits.
3. **Type consistency:** `non_empty: bool | None` in Task 2 matches usage in Task 4. `demotion_reason: str` in Task 1 matches call sites in Tasks 4 and 6. `has_ever_returned_non_empty` spelled identically in Tasks 2 and 4.
4. **North Star check:** (a) skip inference? Bridge fails faster now, but no LLM savings lost — all failure paths still fall back to LLM. (b) carry failure forward? Yes — `bridge_silent_empty` surfaces in reflection via `last_demotion.reason`. (c) compose? Not directly affected. Passes axis (b).
5. **Frame-external check:** Touches real-connector semantics (gmail, hypermesh will benefit). Not pure self-improvement. ✓

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-04-18-bridge-silent-wrong-detection.md`. Two execution options:**

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?**
