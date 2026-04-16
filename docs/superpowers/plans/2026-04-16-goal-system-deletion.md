# Goal System Deletion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete the entire GoalControlPlane system — MCP tools, hooks injection, cockpit UI, admin APIs, test coverage — with no compatibility shims left behind.

**Architecture:** Outside-in, consumer-before-library. Tests are cleaned first (each still passes against the existing code), then production code is stripped layer by layer, ending with `rm goal_control_plane.py`. Each task ends with a clean `pytest tests -q`. Svelte frontend cleaned in a final task (build-verified, no pytest).

**Tech Stack:** Python 3, pytest, Svelte 5 / TypeScript, Vite

---

## File Map

**Delete entirely:**
- `tests/test_goal_control_plane.py`
- `tests/test_goal_migration.py`
- `scripts/goal_control_plane.py`
- `scripts/admin/cockpit/src/components/shared/GoalBar.svelte`
- `scripts/admin/cockpit/src/stores/goal.ts`

**Modify:**
- `tests/test_state_tracker_persistence.py` — drop set_goal / goal-key assertions
- `tests/test_context_budgeting.py` — drop set_goal call
- `tests/test_hook_scripts_output.py` — drop goal-specific tests, rewrite pre_compact test
- `tests/test_hooks_pre_tool_use.py` — drop icc_goal_rollback tests
- `tests/test_mcp_tools_integration.py` — drop goal tool / resource assertions
- `scripts/state_tracker.py` — remove MAX_GOAL_CHARS, goal fields, set_goal(), goal params from format_*
- `hooks/user_prompt_submit.py` — remove goal ingest + snap injection
- `hooks/session_start.py` — remove goal ingest + snap injection
- `hooks/pre_compact.py` — remove goal snap, remove Goal paragraph from systemMessage
- `hooks/setup.py` — remove GoalControlPlane.ensure_initialized()
- `hooks/pre_tool_use.py` — remove _validate_icc_goal_rollback + ask branch
- `scripts/mcp/schemas.py` — remove icc_goal_ingest / icc_goal_read / icc_goal_rollback schemas
- `scripts/mcp/resources.py` — remove goal_control param, state://goal and state://goal-ledger
- `scripts/emerge_daemon.py` — remove GoalControlPlane init, migration method, 3 dispatch entries, 3 handlers, goal fields from reconcile response
- `scripts/admin/api.py` — remove _cmd_set_goal / _cmd_goal_history / _cmd_goal_rollback
- `scripts/admin/control_plane.py` — remove _load_hook_state_summary, clean cmd_control_plane_hook_state
- `scripts/admin/pipeline.py` — remove goal fields from cmd_policy_status
- `scripts/admin/cockpit.py` — remove /api/goal* routes
- `scripts/repl_admin.py` — remove goal imports
- `scripts/admin/cockpit/src/App.svelte` — remove GoalBar + goalStore
- `scripts/admin/cockpit/src/components/audit/AuditTab.svelte` — remove goal event handling
- `scripts/admin/cockpit/src/lib/types.ts` — remove GoalResponse / GoalHistoryEvent / GoalSetRequest / GoalRollbackResponse / GoalHistoryResponse interfaces + goal fields on PolicyResponse
- `scripts/admin/cockpit/src/lib/api.ts` — remove getGoal / getGoalHistory / postGoal / rollbackGoal
- `scripts/admin/cockpit/src/stores/policy.ts` — remove goal field
- `CLAUDE.md` — remove Goal Control Plane architecture + key invariants
- `README.md` — remove goal tool rows from MCP table + resource rows

---

## Task 1: Delete dedicated goal test files

**Files:**
- Delete: `tests/test_goal_control_plane.py`
- Delete: `tests/test_goal_migration.py`

- [ ] **Step 1: Delete both files**

```bash
rm tests/test_goal_control_plane.py tests/test_goal_migration.py
```

- [ ] **Step 2: Run tests — must still pass**

```bash
python -m pytest tests -q
```

Expected: all existing tests pass (these two files are gone; no remaining test imports from them).

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "test: delete goal control plane test files"
```

---

## Task 2: Clean up remaining test files

Remove goal-incidental assertions from 5 test files. After this task, no test file references goal APIs — so the subsequent production code deletions won't break the suite.

**Files:**
- Modify: `tests/test_state_tracker_persistence.py`
- Modify: `tests/test_context_budgeting.py`
- Modify: `tests/test_hook_scripts_output.py`
- Modify: `tests/test_hooks_pre_tool_use.py`
- Modify: `tests/test_mcp_tools_integration.py`

- [ ] **Step 1: Update `tests/test_state_tracker_persistence.py`**

Replace the entire file with:

```python
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.state_tracker import StateTracker, load_tracker, save_tracker


def test_load_tracker_recovers_from_invalid_json(tmp_path: Path):
    state_path = tmp_path / "state.json"
    state_path.write_text("{bad json", encoding="utf-8")
    tracker = load_tracker(state_path)
    ctx = tracker.format_context()
    assert ctx["Delta"] == "- No changes."


def test_load_tracker_normalizes_wrong_shapes(tmp_path: Path):
    state_path = tmp_path / "state.json"
    state_path.write_text(
        '{"open_risks": "oops", "deltas": [{"message": 42}, "bad"], "consistency_window_ms":"x"}',
        encoding="utf-8",
    )
    tracker = load_tracker(state_path)
    state = tracker.to_dict()
    assert state["open_risks"] == []
    assert len(state["deltas"]) == 1
    assert state["deltas"][0]["message"] == "42"
    assert state["consistency_window_ms"] == 0


def test_save_tracker_writes_valid_json_atomically(tmp_path: Path):
    state_path = tmp_path / "state.json"
    tracker = StateTracker()
    tracker.add_delta("atomic delta")
    save_tracker(state_path, tracker)
    loaded = load_tracker(state_path)
    assert loaded.to_dict()["deltas"][0]["message"] == "atomic delta"


def test_format_recovery_token_includes_schema_and_deltas():
    tracker = StateTracker()
    tracker.add_delta("core update")
    token = tracker.format_recovery_token()
    assert token["schema_version"] == "flywheel.v1"
    assert token["deltas"]


def test_format_recovery_token_hard_budget_cap():
    """Token must fit within budget_chars even when all deltas are CORE_CRITICAL."""
    import json
    from scripts.state_tracker import LEVEL_CORE_CRITICAL
    tracker = StateTracker()
    for i in range(30):
        tracker.add_delta(
            message=f"critical delta {i}: " + "x" * 60,
            level=LEVEL_CORE_CRITICAL,
        )
    budget = 800
    token = tracker.format_recovery_token(budget_chars=budget)
    encoded = json.dumps(token, ensure_ascii=True, separators=(",", ":"))
    assert len(encoded) <= budget, (
        f"Token ({len(encoded)} chars) exceeds budget ({budget} chars)"
    )
    assert token["schema_version"] == "flywheel.v1"


def test_add_risk_deduplicates_exact_duplicates():
    """add_risk must not add the same risk string twice."""
    from scripts.state_tracker import StateTracker
    tracker = StateTracker()
    tracker.add_risk("pipeline verification failed: zwcad.write.apply-change")
    tracker.add_risk("pipeline verification failed: zwcad.write.apply-change")
    tracker.add_risk("pipeline verification failed: zwcad.write.apply-change")
    assert len(tracker.state["open_risks"]) == 1, (
        "duplicate risk entries must be suppressed"
    )


def test_add_risk_keeps_distinct_risks():
    """Different risk strings must all be preserved."""
    from scripts.state_tracker import StateTracker
    tracker = StateTracker()
    tracker.add_risk("pipeline verification failed: mock.read.state")
    tracker.add_risk("pipeline verification failed: mock.write.apply-change")
    tracker.add_risk("runner unreachable: mycader-1")
    assert len(tracker.state["open_risks"]) == 3
```

- [ ] **Step 2: Update `tests/test_context_budgeting.py`**

Remove the `tracker.set_goal("Reduce token usage")` line from `test_budget_trims_peripheral_then_aggregates_secondary`. Replace the entire file:

```python
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.state_tracker import (
    LEVEL_CORE_CRITICAL,
    LEVEL_CORE_SECONDARY,
    LEVEL_PERIPHERAL,
    StateTracker,
)


def test_budget_trims_peripheral_then_aggregates_secondary():
    tracker = StateTracker()
    tracker.add_delta("Critical wall length changed", level=LEVEL_CORE_CRITICAL)
    tracker.add_delta("Secondary read detail A", level=LEVEL_CORE_SECONDARY)
    tracker.add_delta("Secondary read detail B", level=LEVEL_CORE_SECONDARY)
    tracker.add_delta("Peripheral debug line", level=LEVEL_PERIPHERAL)
    tracker.add_delta("Peripheral trace line", level=LEVEL_PERIPHERAL)

    full_ctx = tracker.format_context()
    assert "Peripheral debug line" in full_ctx["Delta"]

    trimmed_ctx = tracker.format_context(budget_chars=80)
    assert "Peripheral debug line" not in trimmed_ctx["Delta"]
    assert "Secondary changes: 2 (aggregated)" in trimmed_ctx["Delta"]
    assert "Critical wall length changed" in trimmed_ctx["Delta"]
```

- [ ] **Step 3: Update `tests/test_hook_scripts_output.py`**

Make the following targeted changes (leave all other tests intact):

**Remove these 3 complete test functions** (search and delete each function body):
- `test_session_start_without_goal_does_not_write_default_goal`
- `test_goal_is_capped_and_source_marked`

**In `test_session_start_and_user_prompt_submit_output_parseable`:** remove the line:
```python
assert token["goal_source"] in {"unset", "hook_payload"}
```

**Replace `test_pre_compact_resets_tracker_state_and_keeps_goal_in_snapshot`** with a simpler version that only checks the deltas/risks reset:

```python
def test_pre_compact_resets_tracker_state(tmp_path: Path):
    """After PreCompact, state.json deltas and risks are cleared."""
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_DATA"] = str(tmp_path)

    # Seed some deltas via post_tool_use
    subprocess.run(
        ["python3", str(ROOT / "hooks" / "session_start.py")],
        input=json.dumps({}),
        capture_output=True, text=True, env=env, check=True,
    )
    subprocess.run(
        ["python3", str(ROOT / "hooks" / "post_tool_use.py")],
        input=json.dumps({
            "tool_name": "mcp__plugin_emerge__icc_exec",
            "tool_result": {"isError": False, "content": [{"type": "text", "text": "ok"}]},
            "delta_message": "Wrote mesh to HyperMesh",
        }),
        capture_output=True, text=True, env=env, check=True,
    )

    state_path = tmp_path / "state.json"
    before = json.loads(state_path.read_text())
    assert before["deltas"], "must have deltas before compaction"

    subprocess.run(
        ["python3", str(ROOT / "hooks" / "pre_compact.py")],
        input="{}",
        capture_output=True, text=True, env=env, check=True,
    )

    after = json.loads(state_path.read_text())
    assert after["deltas"] == [], "deltas must be cleared after PreCompact"
    assert after["open_risks"] == [], "open_risks must be cleared after PreCompact"
```

**In `test_session_start_clears_stale_active_span`:** remove `"goal": ""` and `"goal_source": "unset"` from the `stale_state` dict.

- [ ] **Step 4: Update `tests/test_hooks_pre_tool_use.py`**

Delete these 4 complete test functions:
- `test_icc_goal_rollback_returns_ask`
- `test_icc_goal_rollback_missing_target_blocks`
- `test_validate_icc_goal_rollback_valid`
- `test_validate_icc_goal_rollback_missing`

- [ ] **Step 5: Update `tests/test_mcp_tools_integration.py`**

**In the test that asserts listed tool names** (the test that checks `"icc_goal_ingest" in names`): remove these 3 lines:
```python
assert "icc_goal_ingest" in names
assert "icc_goal_read" in names
assert "icc_goal_rollback" in names
```

**In the test that asserts listed resource URIs**: remove these 2 lines:
```python
assert "state://goal" in uris
assert "state://goal-ledger" in uris
```

**Delete the entire `test_resources_read_goal_snapshot_and_ledger` function.**

- [ ] **Step 6: Run tests — must pass**

```bash
python -m pytest tests -q
```

Expected: all tests pass (goal code still exists; tests simply no longer reference it).

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "test: remove goal system test coverage"
```

---

## Task 3: Strip goal from state_tracker.py

**Files:**
- Modify: `scripts/state_tracker.py`

- [ ] **Step 1: Apply all changes to `scripts/state_tracker.py`**

**Remove** the constant at line 15:
```python
MAX_GOAL_CHARS = 120
```

**Replace** the `StateTracker.__init__` initial state dict — remove `"goal"` and `"goal_source"` keys:
```python
def __init__(self, state: dict[str, Any] | None = None) -> None:
    if state is None:
        self.state = {
            "open_risks": [],
            "deltas": [],
            "verification_state": "verified",
            "consistency_window_ms": 0,
        }
    else:
        self.state = _normalize_state(state)
```

**Delete** the entire `set_goal` method:
```python
def set_goal(self, goal: str, source: str = "unknown") -> None:
    ...
```

**Replace** `format_context` signature and body — remove goal params and Goal/Goal Source keys:
```python
def format_context(
    self,
    budget_chars: int | None = None,
) -> dict[str, str]:
    critical, secondary, peripheral = self._partition_deltas()

    delta_lines = [f"- {d['message']}" for d in critical]
    if secondary:
        delta_lines.extend([f"- {d['message']}" for d in secondary])
    if peripheral:
        delta_lines.extend([f"- {d['message']}" for d in peripheral])

    delta_text = "\n".join(delta_lines) if delta_lines else "- No changes."

    if budget_chars and len(delta_text) > budget_chars:
        delta_lines = [f"- {d['message']}" for d in critical]
        if secondary:
            delta_lines.append(f"- Secondary changes: {len(secondary)} (aggregated)")
        delta_text = "\n".join(delta_lines)
        if len(delta_text) > budget_chars:
            delta_text = "\n".join([f"- {d['message']}" for d in critical]) or "- No changes."

    risks = self.state["open_risks"]
    open_risks = [
        r for r in risks
        if (isinstance(r, dict) and r.get("status") == "open") or isinstance(r, str)
    ]
    open_risks.sort(
        key=lambda r: int(r.get("created_at_ms", 0)) if isinstance(r, dict) else 0,
        reverse=True,
    )

    def _risk_line(r) -> str:
        return f"- {r['text']}" if isinstance(r, dict) else f"- {r}"

    risk_lines = [_risk_line(r) for r in open_risks]
    risks_text = "\n".join(risk_lines) if risk_lines else "- None."

    if budget_chars and len(risks_text) > budget_chars // 3:
        allowed = budget_chars // 3
        kept, total = [], 0
        for line in risk_lines:
            if total + len(line) + 1 > allowed:
                remaining = len(risk_lines) - len(kept)
                kept.append(f"- … {remaining} more risks (read state://deltas for full list)")
                break
            kept.append(line)
            total += len(line) + 1
        risks_text = "\n".join(kept) if kept else "- None."

    return {
        "Delta": delta_text,
        "Open Risks": risks_text,
    }
```

**Replace** `format_recovery_token` — remove goal params and goal/goal_source from token dict:
```python
def format_recovery_token(
    self,
    budget_chars: int | None = None,
) -> dict[str, Any]:
    critical, secondary, peripheral = self._partition_deltas()
    selected: list[dict[str, Any]] = [*critical, *secondary, *peripheral]
    aggregated_secondary = 0
    aggregated_peripheral = 0

    if budget_chars:
        encoded = json.dumps(selected, ensure_ascii=True, separators=(",", ":"))
        if len(encoded) > budget_chars:
            selected = [*critical]
            aggregated_secondary = len(secondary)
            aggregated_peripheral = len(peripheral)

    token_deltas: list[dict[str, Any]] = []
    for item in selected:
        row = {
            "id": str(item.get("id", "")),
            "level": str(item.get("level", LEVEL_CORE_CRITICAL)),
            "message": str(item.get("message", "")),
            "verification_state": str(item.get("verification_state", "verified")),
            "provisional": bool(item.get("provisional", False)),
        }
        if "reconcile_outcome" in item:
            row["reconcile_outcome"] = str(item.get("reconcile_outcome", ""))
        token_deltas.append(row)
    if aggregated_secondary:
        token_deltas.append({
            "id": "agg-secondary",
            "level": LEVEL_CORE_SECONDARY,
            "message": f"aggregated:{aggregated_secondary}",
            "verification_state": "verified",
            "provisional": False,
            "aggregated": True,
        })
    if aggregated_peripheral:
        token_deltas.append({
            "id": "agg-peripheral",
            "level": LEVEL_PERIPHERAL,
            "message": f"aggregated:{aggregated_peripheral}",
            "verification_state": "verified",
            "provisional": False,
            "aggregated": True,
        })

    token: dict[str, Any] = {
        "schema_version": "flywheel.v1",
        "verification_state": self.state.get("verification_state", "verified"),
        "consistency_window_ms": int(self.state.get("consistency_window_ms", 0) or 0),
        "open_risks": [
            (r["text"] if isinstance(r, dict) else str(r))
            for r in self.state.get("open_risks", [])
            if (isinstance(r, dict) and r.get("status") == "open") or isinstance(r, str)
        ],
        "deltas": token_deltas,
        "active_span_id": self.state.get("active_span_id") or None,
        "active_span_intent": self.state.get("active_span_intent") or None,
    }
    if budget_chars:
        encoded = json.dumps(token, ensure_ascii=True, separators=(",", ":"))
        if len(encoded) > budget_chars:
            kept: list[dict[str, Any]] = []
            overhead = len(encoded) - sum(
                len(json.dumps(d, ensure_ascii=True, separators=(",", ":")))
                for d in token_deltas
            )
            budget_left = budget_chars - overhead
            for d in token_deltas:
                s = json.dumps(d, ensure_ascii=True, separators=(",", ":"))
                if budget_left - len(s) - 2 >= 0:
                    kept.append(d)
                    budget_left -= len(s) + 2
                else:
                    break
            token["deltas"] = kept
    return token
```

**Replace** `format_additional_context` — remove goal params, update idle check and output:
```python
def format_additional_context(
    self,
    budget_chars: int | None = None,
) -> str:
    context = self.format_context(budget_chars=budget_chars)
    token = self.format_recovery_token(budget_chars=budget_chars)
    token_json = json.dumps(token, ensure_ascii=True, separators=(",", ":"))

    delta_text = context["Delta"]
    risks_text = context["Open Risks"]
    open_risks = [
        r for r in self.state.get("open_risks", [])
        if (isinstance(r, dict) and r.get("status") == "open") or isinstance(r, str)
    ]
    is_idle = not self.state.get("deltas") and not open_risks
    if is_idle:
        return f"FLYWHEEL_TOKEN\n{token_json}"

    return (
        f"Delta\n{delta_text}\n\n"
        f"Open Risks\n{risks_text}\n\n"
        f"FLYWHEEL_TOKEN\n{token_json}"
    )
```

**Replace** `_normalize_state` — remove goal/goal_source processing:
```python
def _normalize_state(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {
            "open_risks": [],
            "deltas": [],
            "verification_state": "verified",
            "consistency_window_ms": 0,
        }
    verification_state = (
        "degraded" if str(raw.get("verification_state", "verified")) == "degraded" else "verified"
    )
    try:
        consistency_window_ms = max(0, int(raw.get("consistency_window_ms", 0)))
    except Exception:
        consistency_window_ms = 0

    open_risks_raw = raw.get("open_risks", [])
    open_risks: list[dict[str, Any]] = []
    if isinstance(open_risks_raw, list):
        for item in open_risks_raw:
            if isinstance(item, str):
                open_risks.append(
                    {
                        "risk_id": "r-" + hashlib.sha256(item.encode()).hexdigest()[:12],
                        "text": item,
                        "status": "open",
                        "created_at_ms": 0,
                        "snoozed_until_ms": None,
                        "handled_reason": None,
                        "source_delta_id": None,
                        "intent_signature": None,
                    }
                )
            elif isinstance(item, dict):
                open_risks.append(
                    {
                        "risk_id": str(item.get("risk_id", "")),
                        "text": str(item.get("text", "")),
                        "status": str(item.get("status", "open")),
                        "created_at_ms": int(item.get("created_at_ms", 0) or 0),
                        "snoozed_until_ms": item.get("snoozed_until_ms"),
                        "handled_reason": item.get("handled_reason"),
                        "source_delta_id": item.get("source_delta_id"),
                        "intent_signature": item.get("intent_signature"),
                    }
                )

    deltas_raw = raw.get("deltas", [])
    deltas: list[dict[str, Any]] = []
    if isinstance(deltas_raw, list):
        for item in deltas_raw:
            if not isinstance(item, dict):
                continue
            delta_id = str(item.get("id", ""))
            message = str(item.get("message", ""))
            level = str(item.get("level", LEVEL_CORE_CRITICAL))
            if level not in {LEVEL_CORE_CRITICAL, LEVEL_CORE_SECONDARY, LEVEL_PERIPHERAL}:
                level = LEVEL_CORE_CRITICAL
            delta_state = (
                "degraded"
                if str(item.get("verification_state", "verified")) == "degraded"
                else "verified"
            )
            normalized = {
                "id": delta_id or f"d-{int(time.time() * 1000)}-{len(deltas)}",
                "message": message,
                "level": level,
                "verification_state": delta_state,
                "provisional": bool(item.get("provisional", False)),
            }
            if "reconcile_outcome" in item:
                normalized["reconcile_outcome"] = str(item["reconcile_outcome"])
            normalized["intent_signature"] = item.get("intent_signature") or None
            normalized["tool_name"] = item.get("tool_name") or None
            try:
                normalized["ts_ms"] = int(item.get("ts_ms", 0))
            except Exception:
                normalized["ts_ms"] = 0
            if item.get("args_summary"):
                normalized["args_summary"] = str(item["args_summary"])[:200]
            deltas.append(normalized)

    out: dict[str, Any] = {
        "open_risks": open_risks,
        "deltas": deltas,
        "verification_state": verification_state,
        "consistency_window_ms": consistency_window_ms,
    }
    for _k in ("active_span_id", "active_span_intent", "turn_count"):
        if _k in raw:
            out[_k] = raw[_k]
    return out
```

- [ ] **Step 2: Run tests**

```bash
python -m pytest tests -q
```

Expected: all tests pass.

- [ ] **Step 3: Commit**

```bash
git add scripts/state_tracker.py
git commit -m "refactor: remove goal fields and set_goal from StateTracker"
```

---

## Task 4: Strip goal from hooks

**Files:**
- Modify: `hooks/user_prompt_submit.py`
- Modify: `hooks/session_start.py`
- Modify: `hooks/pre_compact.py`
- Modify: `hooks/setup.py`
- Modify: `hooks/pre_tool_use.py`

- [ ] **Step 1: Replace `hooks/user_prompt_submit.py`**

```python
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.policy_config import REFLECTION_CACHE_TTL_MS, default_exec_root, default_hook_state_root, pin_plugin_data_path_if_present  # noqa: E402
from scripts.span_tracker import SpanTracker  # noqa: E402
from scripts.state_tracker import load_tracker, save_tracker  # noqa: E402

_REFLECTION_TURN_THRESHOLD = 1
_SPAN_REMINDER_INTERVAL = 5


def _drain_pending_actions(state_root: Path) -> str:
    """Read and consume pending cockpit actions. Returns formatted text or ''."""
    from scripts.pending_actions import format_pending_actions
    for name in ("pending-actions.processed.json", "pending-actions.json"):
        p = state_root / name
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        actions = data.get("actions", [])
        if not actions:
            continue
        delivered = state_root / "pending-actions.delivered.json"
        try:
            p.rename(delivered)
        except OSError:
            continue
        return format_pending_actions(actions)
    return ""


def main() -> None:
    payload_text = sys.stdin.read().strip()
    try:
        payload = json.loads(payload_text) if payload_text else {}
    except Exception:
        payload = {}

    pin_plugin_data_path_if_present()
    state_root = Path(default_hook_state_root())
    state_path = state_root / "state.json"
    tracker = load_tracker(state_path)
    turn_count = int(tracker.state.get("turn_count", 0) or 0) + 1
    tracker.state["turn_count"] = turn_count
    save_tracker(state_path, tracker)

    raw_budget = payload.get("budget_chars", 0)
    try:
        budget_chars = int(raw_budget)
        if budget_chars <= 0:
            budget_chars = None
    except Exception:
        budget_chars = None
    context_text = tracker.format_additional_context(budget_chars=budget_chars)
    if turn_count == _REFLECTION_TURN_THRESHOLD:
        exec_root = Path(os.environ.get("EMERGE_STATE_ROOT", str(default_exec_root())))
        reflection = SpanTracker(
            state_root=exec_root,
            hook_state_root=state_root,
        ).format_reflection_with_cache(cache_ttl_ms=REFLECTION_CACHE_TTL_MS)
        if reflection:
            context_text = reflection + "\n\n" + context_text

    active_span_id = str(tracker.state.get("active_span_id", "") or "")
    if not active_span_id and turn_count > 1 and turn_count % _SPAN_REMINDER_INTERVAL == 0:
        _skip_reminder = False
        if turn_count == _SPAN_REMINDER_INTERVAL:
            try:
                _raw = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
                _skip_reminder = bool(_raw.get("_span_nudge_sent"))
            except Exception:
                pass
        if not _skip_reminder:
            reminder = (
                "[Span] No active span. "
                "If this turn involves repeatable tool use, open one first: "
                "icc_span_open(intent_signature='<connector>.(read|write).<name>') "
                "e.g. 'lark.read.get-doc'."
            )
            context_text = reminder + "\n\n" + context_text

    pending_text = _drain_pending_actions(state_root)
    if pending_text:
        context_text = pending_text + "\n\n" + context_text

    out = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context_text,
        }
    }
    print(json.dumps(out))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Replace `hooks/session_start.py`**

```python
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.policy_config import default_hook_state_root, pin_plugin_data_path_if_present  # noqa: E402
from scripts.state_tracker import load_tracker, save_tracker  # noqa: E402


def _write_connector_rules(cwd: str) -> None:
    """Generate .claude/rules/connector-<name>.md for each connector with NOTES.md."""
    connectors_root = Path.home() / ".emerge" / "connectors"
    if not connectors_root.is_dir():
        return
    try:
        rules_dir = Path(cwd) / ".claude" / "rules"
        rules_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    for connector_dir in sorted(connectors_root.iterdir()):
        if not connector_dir.is_dir():
            continue
        notes_path = connector_dir / "NOTES.md"
        if not notes_path.exists():
            continue
        name = connector_dir.name
        try:
            excerpt = notes_path.read_text(encoding="utf-8").strip()[:400]
            content = (
                f"<!-- emerge:connector:{name} — auto-generated at SessionStart -->\n"
                f"# Connector: {name}\n\n"
                f"{excerpt}\n"
            )
            (rules_dir / f"connector-{name}.md").write_text(content, encoding="utf-8")
        except OSError:
            continue


def main() -> None:
    payload_text = sys.stdin.read().strip()
    try:
        payload = json.loads(payload_text) if payload_text else {}
    except Exception:
        payload = {}
    pin_plugin_data_path_if_present()
    state_root = Path(default_hook_state_root())
    state_path = state_root / "state.json"
    tracker = load_tracker(state_path)
    # Always save on SessionStart: clear stale flywheel span from the previous session.
    tracker.state.pop("active_span_id", None)
    tracker.state.pop("active_span_intent", None)
    tracker.state.pop("turn_count", None)
    save_tracker(state_path, tracker)
    context_text = tracker.format_additional_context()

    _SPAN_PROTOCOL = (
        "Span Protocol\n"
        "At the start of each user task that involves tool use, open a span: "
        'icc_span_open(intent_signature="connector.mode.name") '
        "→ execute all steps → icc_span_close(outcome=success|failure|aborted). "
        "Skip only for trivial one-off lookups (Read/Glob/Grep). "
        "Do NOT open sub-spans inside an active span — one span per top-level task. "
        "Repeated patterns auto-promote to zero-LLM pipelines."
    )
    context_text = _SPAN_PROTOCOL + "\n\n" + context_text

    conflicts_path = Path.home() / ".emerge" / "pending-conflicts.json"
    try:
        conflicts_data = json.loads(conflicts_path.read_text(encoding="utf-8"))
        pending = [c for c in conflicts_data.get("conflicts", []) if c.get("status") == "pending"]
        if pending:
            by_connector: dict[str, int] = {}
            for c in pending:
                connector = c.get("connector", "unknown")
                by_connector[connector] = by_connector.get(connector, 0) + 1
            connector_summary = ", ".join(
                f"{name} ({count} file{'s' if count != 1 else ''})"
                for name, count in sorted(by_connector.items())
            )
            context_text += (
                f"\n\n⚠️ Memory Hub has {len(pending)} unresolved sync conflict(s)."
                " Run /emerge:hub to resolve them.\n"
                f"Connectors affected: {connector_summary}"
            )
    except (OSError, json.JSONDecodeError, AttributeError):
        pass

    _write_connector_rules(payload.get("cwd") or str(Path.cwd()))

    import subprocess as _sub
    _plugin_root = Path(__file__).resolve().parents[1]
    try:
        _sub.Popen(
            [sys.executable,
             str(_plugin_root / "scripts" / "emerge_daemon.py"),
             "--ensure-running"],
            start_new_session=True,
            stdout=_sub.DEVNULL,
            stderr=_sub.DEVNULL,
        )
    except Exception:
        pass

    out = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context_text,
        }
    }
    print(json.dumps(out))


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Replace `hooks/pre_compact.py`**

```python
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.policy_config import REFLECTION_CACHE_TTL_MS, default_exec_root, default_hook_state_root, pin_plugin_data_path_if_present  # noqa: E402
from scripts.span_tracker import SpanTracker  # noqa: E402
from scripts.state_tracker import StateTracker, load_tracker, save_tracker  # noqa: E402

_BUDGET_CHARS = 800


def main() -> None:
    sys.stdin.read()  # consume stdin (unused by PreCompact)

    pin_plugin_data_path_if_present()
    state_root = Path(default_hook_state_root())
    state_path = state_root / "state.json"
    tracker = load_tracker(state_path)

    token = tracker.format_recovery_token(budget_chars=_BUDGET_CHARS)
    token_json = json.dumps(token, ensure_ascii=True, separators=(",", ":"))
    _SPAN_PROTOCOL = (
        "Span Protocol\n"
        "At the start of each user task that involves tool use, open a span: "
        'icc_span_open(intent_signature="connector.mode.name") '
        "→ execute all steps → icc_span_close(outcome=success|failure|aborted). "
        "Skip only for trivial one-off lookups (Read/Glob/Grep). "
        "Do NOT open sub-spans inside an active span — one span per top-level task. "
        "Repeated patterns auto-promote to zero-LLM pipelines."
    )
    span_line = ""
    if tracker.state.get("active_span_id"):
        sid = tracker.state["active_span_id"]
        sint = tracker.state.get("active_span_intent", "")
        span_line = f"\nActive span: {sid} ({sint}) -- call icc_span_close when done."
    exec_root = Path(os.environ.get("EMERGE_STATE_ROOT", str(default_exec_root())))
    reflection = SpanTracker(
        state_root=exec_root,
        hook_state_root=state_root,
    ).format_reflection_with_cache(cache_ttl_ms=REFLECTION_CACHE_TTL_MS)
    reflection_block = f"{reflection}\n\n" if reflection else ""

    context_text = (
        _SPAN_PROTOCOL + span_line + "\n\n" + reflection_block
        + f"Open Risks\n"
        + ("\n".join(f"- {r}" for r in token.get("open_risks", [])) or "- None.")
        + f"\n\nFLYWHEEL_TOKEN\n{token_json}"
    )

    # Reset tracker so the next session starts fresh.
    fresh = StateTracker()
    save_tracker(state_path, fresh)

    out = {"systemMessage": context_text}
    print(json.dumps(out))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Replace `hooks/setup.py`**

```python
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.policy_config import default_emerge_home, pin_plugin_data_path_if_present  # noqa: E402


def main() -> None:
    payload_text = sys.stdin.read().strip()
    try:
        payload = json.loads(payload_text) if payload_text else {}
    except Exception:
        payload = {}

    emerge_home = default_emerge_home()
    for subdir in ("hook-state", "connectors", "repl"):
        (emerge_home / subdir).mkdir(parents=True, exist_ok=True)

    pin_plugin_data_path_if_present()

    out = {"systemMessage": f"emerge plugin ready. Home: {emerge_home}"}
    print(json.dumps(out))


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Update `hooks/pre_tool_use.py`**

Delete the `_validate_icc_goal_rollback` function (find it around line 131):
```python
def _validate_icc_goal_rollback(args: dict) -> str | None:
    if not str(args.get("target_event_id", "")).strip():
        return "icc_goal_rollback: 'target_event_id' is required"
    return None
```

Remove `"__icc_goal_rollback": _validate_icc_goal_rollback,` from the `_VALIDATORS` dict.

Delete the `if suffix == "__icc_goal_rollback":` block (the `permissionDecision: ask` branch that fires around line 174).

- [ ] **Step 6: Run tests**

```bash
python -m pytest tests -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add hooks/
git commit -m "refactor: remove goal system from hooks"
```

---

## Task 5: Strip goal from MCP layer

**Files:**
- Modify: `scripts/mcp/schemas.py`
- Modify: `scripts/mcp/resources.py`
- Modify: `scripts/emerge_daemon.py`

- [ ] **Step 1: Update `scripts/mcp/schemas.py`**

Delete the three tool schema dicts (they appear after the comment about `icc_read`/`icc_write`):
- The entire dict for `"name": "icc_goal_ingest"` (lines 206–230)
- The entire dict for `"name": "icc_goal_read"` (lines 231–244)
- The entire dict for `"name": "icc_goal_rollback"` (lines 245–260)

- [ ] **Step 2: Update `scripts/mcp/resources.py`**

**Remove** `goal_control: Any,` parameter from `McpResourceHandler.__init__` and the `self._goal_control = goal_control` assignment.

**Remove** the two resource entries from `list_resources()` static list:
```python
{
    "uri": "state://goal",
    "name": "Goal control snapshot",
    "mimeType": "application/json",
    "description": "Current active goal and decision metadata",
},
{
    "uri": "state://goal-ledger",
    "name": "Goal control ledger",
    "mimeType": "application/json",
    "description": "Recent goal events and decision outcomes",
},
```

**Update** the `state://deltas` description (remove "goal"):
```python
{
    "uri": "state://deltas",
    "name": "State tracker deltas",
    "mimeType": "application/json",
    "description": "Recorded deltas and open risks for the current session",
},
```

**In** `read_resource()`, in the `state://deltas` branch, remove the 3 goal-injection lines:
```python
# Remove these lines:
data["goal"] = snapshot.get("text", "")
data["goal_source"] = snapshot.get("source", "unset")
data["goal_version"] = snapshot.get("version", 0)
```
Also remove the `snapshot = self._goal_control.read_snapshot()` line above them.

**Delete** the entire `state://goal` handling block:
```python
if uri == "state://goal":
    snapshot = self._goal_control.read_snapshot()
    return {"uri": uri, "mimeType": "application/json", "text": json.dumps(snapshot)}
```

**Delete** the entire `state://goal-ledger` handling block:
```python
if uri == "state://goal-ledger":
    rows = self._goal_control.read_ledger(limit=500)
    return {"uri": uri, "mimeType": "application/json", "text": json.dumps({"events": rows})}
```

- [ ] **Step 3: Update `scripts/emerge_daemon.py`**

**Remove** the goal import (lines 28–30):
```python
from scripts.goal_control_plane import (  # noqa: E402
    EVENT_SYSTEM_REFINE,
    GoalControlPlane,
)
```

**Remove** from `__init__` (around line 84–86):
```python
self._goal_control = GoalControlPlane(Path(default_hook_state_root()))
self._goal_control.ensure_initialized()
self._migrate_legacy_goal_once()
```

**Remove** `goal_control=self._goal_control,` from the `McpResourceHandler(...)` constructor call (around line 109).

**Delete** the entire `_migrate_legacy_goal_once` method (lines 151–168).

**Remove** from `_TOOL_DISPATCH`:
```python
"icc_goal_ingest":  "_handle_icc_goal_ingest",
"icc_goal_read":    "_handle_icc_goal_read",
"icc_goal_rollback": "_handle_icc_goal_rollback",
```

**Delete** the 3 handler methods:
- `_handle_icc_goal_ingest` (lines 407–425)
- `_handle_icc_goal_read` (lines 427–431)
- `_handle_icc_goal_rollback` (lines 433–445)

**In `_handle_icc_reconcile`**, remove the `goal_snapshot` fetch and the 3 goal fields from the return dict:
```python
# Remove this line:
goal_snapshot = self._goal_control.read_snapshot()

# Update return to:
return self._tool_ok_json({
    "delta_id": delta_id,
    "outcome": outcome,
    "intent_signature": intent_signature or None,
    "verification_state": td.get("verification_state", "unverified"),
})
```

**Also remove** from `handle_jsonrpc`'s resources/list response the two goal resource template entries (search for `"uriTemplate": "state://goal"`):
```python
{
    "uriTemplate": "state://goal",
    "name": "Active goal snapshot",
    "description": "Goal Control Plane active goal decision snapshot",
},
```
(There may be a similar entry for `state://goal-ledger` — delete it too.)

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/mcp/ scripts/emerge_daemon.py
git commit -m "refactor: remove goal tools and resources from MCP layer"
```

---

## Task 6: Strip goal from admin layer

**Files:**
- Modify: `scripts/admin/api.py`
- Modify: `scripts/admin/control_plane.py`
- Modify: `scripts/admin/pipeline.py`
- Modify: `scripts/admin/cockpit.py`
- Modify: `scripts/repl_admin.py`

- [ ] **Step 1: Update `scripts/admin/api.py`**

**Remove** the import line:
```python
from scripts.goal_control_plane import EVENT_HUMAN_EDIT, GoalControlPlane  # noqa: E402
```

**Delete** the 3 goal management functions:
- `_cmd_set_goal` (lines 311–339)
- `_cmd_goal_history` (lines 342–344)
- `_cmd_goal_rollback` (lines 347–359)

Also remove the comment block `# Goal management helpers (cockpit internal)` above them.

- [ ] **Step 2: Update `scripts/admin/control_plane.py`**

**Remove** the import:
```python
from scripts.goal_control_plane import GoalControlPlane  # noqa: E402
```

**Delete** the entire `_load_hook_state_summary` function (lines 119–140).

**In `cmd_control_plane_hook_state`**: remove the `init_goal_control_plane` import, `goal_cp`, `snap` lines, and the `"goal"` / `"goal_source"` keys from `hook_fields`. Also remove `goal_override` / `goal_source_override` params from `tracker.format_additional_context()` call. The updated function body around that area:

```python
def cmd_control_plane_hook_state() -> dict:
    """Hook state: fields tracked by hooks in state.json + context injection preview."""
    hook_state_root = Path(default_hook_state_root())
    state_path = hook_state_root / "state.json"
    from scripts.span_tracker import SpanTracker
    tracker = load_tracker(state_path)

    raw_state: dict = {}
    if state_path.exists():
        try:
            raw_state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    state = tracker.state
    hook_fields = {
        "turn_count": int(state.get("turn_count", 0) or 0),
        "active_span_id": state.get("active_span_id") or None,
        "active_span_intent": state.get("active_span_intent") or None,
        "span_nudge_sent": bool(raw_state.get("_span_nudge_sent")),
    }

    try:
        exec_root = Path(os.environ.get("EMERGE_STATE_ROOT", str(default_exec_root())))
        reflection = SpanTracker(
            state_root=exec_root,
            hook_state_root=hook_state_root,
        ).format_reflection_with_cache(cache_ttl_ms=15 * 60 * 1000)
        context_preview = tracker.format_additional_context()
        if reflection:
            context_preview = reflection + "\n\n" + context_preview
        active_span = hook_fields["active_span_id"]
        if not active_span and hook_fields["turn_count"] > 1 and hook_fields["turn_count"] % 5 == 0:
            context_preview = (
                "[Span] No active span. "
                "If this turn involves tool use, open one first: "
                'icc_span_open(intent_signature="connector.mode.name").'
                "\n\n" + context_preview
            )
    except Exception as e:
        context_preview = f"(preview unavailable: {e})"
    # ... rest of function unchanged
```

- [ ] **Step 3: Update `scripts/admin/pipeline.py`**

**Remove** the import of `_load_hook_state_summary` from `cmd_policy_status`:
```python
# change:
from scripts.admin.control_plane import _load_hook_state_summary, _resolve_session_id
# to:
from scripts.admin.control_plane import _resolve_session_id
```

**Remove** from `cmd_policy_status`:
```python
hook_summary = _load_hook_state_summary()
```

**Remove** the two goal lines from the return dict:
```python
"goal": hook_summary["goal"],
"goal_source": hook_summary["goal_source"],
```

- [ ] **Step 4: Update `scripts/admin/cockpit.py`**

**Remove** from imports:
```python
_cmd_set_goal,
_cmd_goal_history,
_cmd_goal_rollback,
```

**Remove** the `/api/goal` GET route handler:
```python
elif path == "/api/goal":
    self._json({"ok": True, **_load_hook_state_summary()})
```

**Remove** the `/api/goal-history` GET route handler:
```python
elif path == "/api/goal-history":
    ...
    self._json(_cmd_goal_history(limit=limit))
```

**Remove** the `/api/goal` POST route handler:
```python
elif path == "/api/goal":
    self._json(_cmd_set_goal(body))
```

**Remove** the `/api/goal/rollback` POST route handler:
```python
elif path == "/api/goal/rollback":
    self._json(_cmd_goal_rollback(body))
```

Also remove `_load_hook_state_summary` from the control_plane import list if it's listed there.

- [ ] **Step 5: Update `scripts/repl_admin.py`**

Remove these lines from the import blocks:

From the `scripts.admin.api` import:
```python
_cmd_set_goal,
_cmd_goal_history,
_cmd_goal_rollback,
```

From the `scripts.admin.control_plane` import:
```python
_load_hook_state_summary,
```

Also update the module docstring comment that says `scripts/admin/api.py — SSE, cockpit HTML, goal, settings, status` → remove "goal, ".

- [ ] **Step 6: Run tests**

```bash
python -m pytest tests -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add scripts/admin/ scripts/repl_admin.py
git commit -m "refactor: remove goal APIs from admin and cockpit layers"
```

---

## Task 7: Delete goal_control_plane.py

**Files:**
- Delete: `scripts/goal_control_plane.py`

- [ ] **Step 1: Verify no imports remain**

```bash
grep -r "goal_control_plane" scripts/ hooks/ tests/ --include="*.py"
```

Expected: no output (all imports were removed in previous tasks).

- [ ] **Step 2: Delete the file**

```bash
rm scripts/goal_control_plane.py
```

- [ ] **Step 3: Run tests**

```bash
python -m pytest tests -q
```

Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "feat: delete GoalControlPlane — goal system fully removed"
```

---

## Task 8: Clean up Svelte frontend

No pytest here — verify with a Vite build.

**Files:**
- Delete: `scripts/admin/cockpit/src/components/shared/GoalBar.svelte`
- Delete: `scripts/admin/cockpit/src/stores/goal.ts`
- Modify: `scripts/admin/cockpit/src/App.svelte`
- Modify: `scripts/admin/cockpit/src/components/audit/AuditTab.svelte`
- Modify: `scripts/admin/cockpit/src/lib/types.ts`
- Modify: `scripts/admin/cockpit/src/lib/api.ts`
- Modify: `scripts/admin/cockpit/src/stores/policy.ts`

- [ ] **Step 1: Delete GoalBar.svelte and goal store**

```bash
rm scripts/admin/cockpit/src/components/shared/GoalBar.svelte
rm scripts/admin/cockpit/src/stores/goal.ts
```

- [ ] **Step 2: Update `scripts/admin/cockpit/src/App.svelte`**

Remove this import line:
```typescript
import GoalBar from './components/shared/GoalBar.svelte';
```

Remove:
```typescript
import { goalStore } from './stores/goal';
```

Remove `goalStore.refresh(),` from the initial data load call (the `Promise.all([...])`).

Change:
```typescript
$: shellLoading =
  $policyStore.loading || $monitorsStore.loading || $sessionStore.loading || $goalStore.loading || $stateStore.loading || assetsLoading;
$: shellError =
  $policyStore.error ?? $monitorsStore.error ?? $sessionStore.error ?? $goalStore.error ?? $stateStore.error ?? assetsError;
```
to:
```typescript
$: shellLoading =
  $policyStore.loading || $monitorsStore.loading || $sessionStore.loading || $stateStore.loading || assetsLoading;
$: shellError =
  $policyStore.error ?? $monitorsStore.error ?? $sessionStore.error ?? $stateStore.error ?? assetsError;
```

Remove from the header template:
```svelte
<GoalBar embedded={true} />
```

- [ ] **Step 3: Update `scripts/admin/cockpit/src/components/audit/AuditTab.svelte`**

Remove the import:
```typescript
import type { GoalHistoryEvent } from '../../lib/types';
```

Remove the `mapGoal` function.

In the `loadAuditData()` function, remove `goalPayload` from the `Promise.all` array and remove the goal API call:
```typescript
// remove:
api.request<{ events?: JsonObject[] }>('/api/goal-history', {
  query: { limit: 30 }
}),
```

Remove `const goalItems = ...` and remove `...goalItems` from the items array merge.

Remove the `{:else if item.type === 'goal'}` branch from the template.

Remove the `.goal-pill` CSS rule.

Update the `type` field in `AuditItem` interface: change `'exec' | 'pipeline' | 'span' | 'goal' | 'tool'` to `'exec' | 'pipeline' | 'span' | 'tool'`.

- [ ] **Step 4: Update `scripts/admin/cockpit/src/lib/types.ts`**

Delete these interfaces:
- `GoalResponse`
- `GoalHistoryEvent`
- `GoalSetRequest`
- `GoalHistoryResponse`
- `GoalRollbackResponse`

Remove `goal?` and `goal_source?` fields from `PolicyResponse`.

- [ ] **Step 5: Update `scripts/admin/cockpit/src/lib/api.ts`**

Remove imports:
```typescript
GoalResponse,
GoalHistoryResponse,
GoalSetRequest,
```
(and `GoalRollbackResponse` if imported)

Delete these 4 API methods from the `api` object:
- `getGoal`
- `getGoalHistory`
- `rollbackGoal`
- `postGoal`

- [ ] **Step 6: Update `scripts/admin/cockpit/src/stores/policy.ts`**

Remove `goal: string | null;` from `PolicyStoreState` interface.

Remove `goal: null,` from `initialState`.

Remove `goal: payload.goal ?? null,` from the `update(...)` call in `refresh`.

- [ ] **Step 7: Build to verify no TypeScript errors**

```bash
cd scripts/admin/cockpit && npm run build
```

Expected: build succeeds with no errors.

- [ ] **Step 8: Commit**

```bash
git add scripts/admin/cockpit/src/
git commit -m "feat: remove GoalBar and goal store from cockpit frontend"
```

---

## Task 9: Update documentation

**Files:**
- Modify: `CLAUDE.md`
- Modify: `README.md`

- [ ] **Step 1: Update `CLAUDE.md`**

In the **Architecture** section, delete the entire **Goal Control Plane** bullet:
> `**Goal Control Plane**: active goal no longer lives in state.json...`

In the **Key Invariants** section, remove:
- The `**Cockpit session reset span guard**` bullet mentioning `active_span_open` (keep if it's not purely about goal — read it first)
- Any bullets that mention `icc_goal_*`, `goal-snapshot.json`, `goal-ledger.jsonl`, `GoalControlPlane`

In the **Documentation Update Rules** table, remove:
- The row `Cockpit API contract change` only if it specifically references goal — keep the general row.

- [ ] **Step 2: Update `README.md`**

In the MCP Tools table: remove the rows for `icc_goal_ingest`, `icc_goal_read`, `icc_goal_rollback`.

In the Resources/MCP resources section: remove the rows for `state://goal` and `state://goal-ledger`.

Remove any architecture narrative that describes the Goal Control Plane.

- [ ] **Step 3: Run tests one final time**

```bash
python -m pytest tests -q
```

Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md README.md
git commit -m "docs: remove Goal Control Plane from architecture docs"
```
