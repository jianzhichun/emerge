# Cockpit Control Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade cockpit from pipeline-only display to a full 8-layer control plane with delta/risk/goal/span/exec/policy/session/operator visibility and control actions.

**Architecture:** Enrich `StateTracker` schema (delta intent_signature + risk objects + frozen flags), add `/api/control-plane/*` read/write endpoints in `repl_admin.py`, then extend `cockpit_shell.html` with Overview intent table, connector sub-panels (Deltas/Risks/Spans/Exec Events), and three new global tabs (Audit/Session/Operator). All within the existing single-file HTML + Python HTTP handler pattern.

**Tech Stack:** Python 3.11+ (stdlib only), single-file HTML/CSS/JS (no build tools), pytest for testing.

---

## File Structure

| File | Responsibility | Action |
|------|---------------|--------|
| `scripts/state_tracker.py` | StateTracker: delta enrichment + risk object model | Modify |
| `hooks/post_tool_use.py` | Pass intent_signature/tool_name/ts_ms when adding deltas | Modify |
| `hooks/post_tool_use_failure.py` | Pass ts_ms when marking degraded | Modify |
| `scripts/emerge_daemon.py` | Respect `frozen` flag in auto-promotion/demotion | Modify |
| `scripts/span_tracker.py` | Add `frozen` field to span candidates | Modify |
| `scripts/repl_admin.py` | New `/api/control-plane/*` endpoints (read + write) | Modify |
| `scripts/cockpit_shell.html` | Overview intent table, connector sub-panels, Audit/Session/Operator tabs | Modify |
| `tests/test_state_tracker.py` | Tests for delta enrichment + risk object model + migration | Modify |
| `tests/test_cockpit_api.py` | Tests for new control-plane API endpoints | Create |
| `tests/test_mcp_tools_integration.py` | Verify frozen flag behavior in policy transitions | Modify |

---

### Task 1: Delta Enrichment in StateTracker

**Files:**
- Modify: `scripts/state_tracker.py`
- Test: `tests/test_state_tracker.py`

- [ ] **Step 1: Write failing tests for delta enrichment**

```python
# In tests/test_state_tracker.py — add these test functions

def test_add_delta_with_intent_signature():
    tracker = StateTracker()
    delta_id = tracker.add_delta(
        message="exec zwcad.write.apply-change",
        level=LEVEL_CORE_CRITICAL,
        intent_signature="zwcad.write.apply-change",
        tool_name="icc_exec",
    )
    deltas = tracker.to_dict()["deltas"]
    assert len(deltas) == 1
    assert deltas[0]["intent_signature"] == "zwcad.write.apply-change"
    assert deltas[0]["tool_name"] == "icc_exec"
    assert deltas[0]["ts_ms"] > 0


def test_add_delta_without_intent_defaults_to_none():
    tracker = StateTracker()
    tracker.add_delta(message="generic tool call")
    deltas = tracker.to_dict()["deltas"]
    assert deltas[0]["intent_signature"] is None
    assert deltas[0]["tool_name"] is None
    assert deltas[0]["ts_ms"] > 0


def test_normalize_state_fills_missing_delta_fields():
    raw = {
        "goal": "",
        "goal_source": "unset",
        "open_risks": [],
        "deltas": [{"id": "d-1", "message": "old delta", "level": "core_critical",
                     "verification_state": "verified", "provisional": False}],
        "verification_state": "verified",
        "consistency_window_ms": 0,
    }
    tracker = StateTracker(state=raw)
    d = tracker.to_dict()["deltas"][0]
    assert d["intent_signature"] is None
    assert d["tool_name"] is None
    assert d["ts_ms"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_state_tracker.py::test_add_delta_with_intent_signature tests/test_state_tracker.py::test_add_delta_without_intent_defaults_to_none tests/test_state_tracker.py::test_normalize_state_fills_missing_delta_fields -v`
Expected: FAIL — `add_delta` does not accept `intent_signature`/`tool_name` params

- [ ] **Step 3: Implement delta enrichment in StateTracker**

In `scripts/state_tracker.py`, modify `add_delta`:

```python
def add_delta(
    self,
    message: str,
    level: str = LEVEL_CORE_CRITICAL,
    verification_state: str = "verified",
    provisional: bool = False,
    intent_signature: str | None = None,
    tool_name: str | None = None,
    ts_ms: int | None = None,
) -> str:
    message = str(message).strip() or "(no message)"
    if ts_ms is None:
        ts_ms = int(time.time() * 1000)
    delta_id = f"d-{int(time.time() * 1000)}-{len(self.state['deltas'])}"
    self.state["deltas"].append(
        {
            "id": delta_id,
            "message": message,
            "level": level,
            "verification_state": verification_state,
            "provisional": provisional,
            "intent_signature": intent_signature,
            "tool_name": tool_name,
            "ts_ms": ts_ms,
        }
    )
    if verification_state == "degraded":
        self.state["verification_state"] = "degraded"
    return delta_id
```

In `_normalize_state`, inside the delta normalization loop, after building `normalized` dict, add:

```python
normalized["intent_signature"] = item.get("intent_signature") or None
normalized["tool_name"] = item.get("tool_name") or None
try:
    normalized["ts_ms"] = int(item.get("ts_ms", 0))
except Exception:
    normalized["ts_ms"] = 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_state_tracker.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/state_tracker.py tests/test_state_tracker.py
git commit -m "feat: enrich delta with intent_signature, tool_name, ts_ms"
```

---

### Task 2: Risk Object Model in StateTracker

**Files:**
- Modify: `scripts/state_tracker.py`
- Test: `tests/test_state_tracker.py`

- [ ] **Step 1: Write failing tests for risk objects**

```python
# In tests/test_state_tracker.py

def test_add_risk_creates_object():
    tracker = StateTracker()
    tracker.add_risk("pipeline verification failed")
    risks = tracker.to_dict()["open_risks"]
    assert len(risks) == 1
    assert isinstance(risks[0], dict)
    assert risks[0]["text"] == "pipeline verification failed"
    assert risks[0]["status"] == "open"
    assert "risk_id" in risks[0]
    assert risks[0]["created_at_ms"] > 0


def test_add_risk_dedup_by_text():
    tracker = StateTracker()
    tracker.add_risk("same risk")
    tracker.add_risk("same risk")
    assert len(tracker.to_dict()["open_risks"]) == 1


def test_normalize_state_migrates_bare_string_risks():
    raw = {
        "goal": "", "goal_source": "unset",
        "open_risks": ["bare risk string", "another risk"],
        "deltas": [], "verification_state": "verified", "consistency_window_ms": 0,
    }
    tracker = StateTracker(state=raw)
    risks = tracker.to_dict()["open_risks"]
    assert len(risks) == 2
    assert risks[0]["text"] == "bare risk string"
    assert risks[0]["status"] == "open"
    assert isinstance(risks[0]["risk_id"], str)


def test_update_risk_handle():
    tracker = StateTracker()
    tracker.add_risk("test risk")
    risk_id = tracker.to_dict()["open_risks"][0]["risk_id"]
    tracker.update_risk(risk_id, action="handle", reason="manually resolved")
    r = tracker.to_dict()["open_risks"][0]
    assert r["status"] == "handled"
    assert r["handled_reason"] == "manually resolved"


def test_update_risk_snooze():
    tracker = StateTracker()
    tracker.add_risk("snooze risk")
    risk_id = tracker.to_dict()["open_risks"][0]["risk_id"]
    tracker.update_risk(risk_id, action="snooze", snooze_duration_ms=3600000)
    r = tracker.to_dict()["open_risks"][0]
    assert r["status"] == "snoozed"
    assert r["snoozed_until_ms"] > 0


def test_format_context_uses_risk_text():
    tracker = StateTracker()
    tracker.add_risk("a real risk")
    ctx = tracker.format_context()
    assert "a real risk" in ctx["Open Risks"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_state_tracker.py::test_add_risk_creates_object tests/test_state_tracker.py::test_normalize_state_migrates_bare_string_risks tests/test_state_tracker.py::test_update_risk_handle -v`
Expected: FAIL

- [ ] **Step 3: Implement risk object model**

In `scripts/state_tracker.py`:

1. Update `add_risk`:

```python
def add_risk(
    self,
    risk: str,
    intent_signature: str | None = None,
    source_delta_id: str | None = None,
) -> None:
    text = str(risk).strip()
    if not text:
        return
    for existing in self.state["open_risks"]:
        if isinstance(existing, dict) and existing.get("text") == text:
            return
        if isinstance(existing, str) and existing == text:
            return
    import hashlib
    risk_id = "r-" + hashlib.sha256(text.encode()).hexdigest()[:12]
    self.state["open_risks"].append({
        "risk_id": risk_id,
        "text": text,
        "status": "open",
        "created_at_ms": int(time.time() * 1000),
        "snoozed_until_ms": None,
        "handled_reason": None,
        "source_delta_id": source_delta_id,
        "intent_signature": intent_signature,
    })
```

2. Add `update_risk`:

```python
def update_risk(
    self,
    risk_id: str,
    action: str,
    reason: str | None = None,
    snooze_duration_ms: int | None = None,
) -> None:
    if action not in ("handle", "snooze", "reopen"):
        raise ValueError(f"update_risk: action must be handle/snooze/reopen, got {action!r}")
    for r in self.state["open_risks"]:
        if not isinstance(r, dict):
            continue
        if r.get("risk_id") == risk_id:
            if action == "handle":
                r["status"] = "handled"
                r["handled_reason"] = reason
            elif action == "snooze":
                r["status"] = "snoozed"
                r["snoozed_until_ms"] = int(time.time() * 1000) + (snooze_duration_ms or 3600000)
            elif action == "reopen":
                r["status"] = "open"
                r["snoozed_until_ms"] = None
                r["handled_reason"] = None
            break
```

3. Update `_normalize_state` risk section:

```python
open_risks_raw = raw.get("open_risks", [])
open_risks: list[dict[str, Any]] = []
if isinstance(open_risks_raw, list):
    for item in open_risks_raw:
        if isinstance(item, str):
            import hashlib
            open_risks.append({
                "risk_id": "r-" + hashlib.sha256(item.encode()).hexdigest()[:12],
                "text": item,
                "status": "open",
                "created_at_ms": 0,
                "snoozed_until_ms": None,
                "handled_reason": None,
                "source_delta_id": None,
                "intent_signature": None,
            })
        elif isinstance(item, dict):
            open_risks.append({
                "risk_id": str(item.get("risk_id", "")),
                "text": str(item.get("text", "")),
                "status": str(item.get("status", "open")),
                "created_at_ms": int(item.get("created_at_ms", 0) or 0),
                "snoozed_until_ms": item.get("snoozed_until_ms"),
                "handled_reason": item.get("handled_reason"),
                "source_delta_id": item.get("source_delta_id"),
                "intent_signature": item.get("intent_signature"),
            })
```

4. Update `format_context` — change `risks_text`:

```python
risks = self.state["open_risks"]
risk_texts = []
for r in risks:
    if isinstance(r, dict):
        if r.get("status") == "open":
            risk_texts.append(f"- {r['text']}")
    elif isinstance(r, str):
        risk_texts.append(f"- {r}")
risks_text = "\n".join(risk_texts) if risk_texts else "- None."
```

5. Update `format_recovery_token` — same pattern for `open_risks`:

```python
"open_risks": [
    (r["text"] if isinstance(r, dict) else str(r))
    for r in self.state.get("open_risks", [])
    if (isinstance(r, dict) and r.get("status") == "open") or isinstance(r, str)
],
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_state_tracker.py -v`
Expected: ALL PASS

- [ ] **Step 5: Run full test suite to catch regressions**

Run: `python -m pytest tests -q`
Expected: All 318+ tests pass

- [ ] **Step 6: Commit**

```bash
git add scripts/state_tracker.py tests/test_state_tracker.py
git commit -m "feat: upgrade risks from bare strings to objects with lifecycle"
```

---

### Task 3: Wire Intent Signature into PostToolUse Hook

**Files:**
- Modify: `hooks/post_tool_use.py`
- Modify: `hooks/post_tool_use_failure.py`

- [ ] **Step 1: Modify post_tool_use.py to pass enrichment fields**

In `hooks/post_tool_use.py`, after line that sets `level = _classify_level(tool_name)`, extract intent_signature from tool input:

```python
tool_input = payload.get("tool_input", {}) or {}
if not isinstance(tool_input, dict):
    tool_input = {}
intent_signature = str(tool_input.get("intent_signature", "")).strip() or None
```

Then update the `tracker.add_delta()` call:

```python
delta_id = tracker.add_delta(
    message=message,
    level=level,
    verification_state=verification_state,
    provisional=provisional,
    intent_signature=intent_signature,
    tool_name=tool_name,
)
```

- [ ] **Step 2: Run full test suite**

Run: `python -m pytest tests -q`
Expected: All tests pass (hooks are tested via integration tests)

- [ ] **Step 3: Commit**

```bash
git add hooks/post_tool_use.py hooks/post_tool_use_failure.py
git commit -m "feat: pass intent_signature and tool_name into delta records"
```

---

### Task 4: Frozen Flag in Policy Registry and Span Candidates

**Files:**
- Modify: `scripts/emerge_daemon.py`
- Modify: `scripts/span_tracker.py`
- Test: `tests/test_mcp_tools_integration.py`

- [ ] **Step 1: Write failing test for frozen pipeline**

```python
# In tests/test_mcp_tools_integration.py

async def test_frozen_pipeline_skips_auto_promotion():
    daemon = EmergeDaemon()
    # Record enough events to normally promote
    for i in range(25):
        daemon._record_exec_event(
            source="exec", mode="inline_code", target_profile="default",
            intent_signature="mock.read.frozen-test", script_ref=None,
            base_pipeline_id=None, verify_passed=True, human_fix=False,
            is_error=False,
        )
    # Manually set frozen
    reg_path = daemon._state_root / "pipelines-registry.json"
    data = json.loads(reg_path.read_text())
    key = "mock.read.frozen-test"
    data["pipelines"][key]["frozen"] = True
    reg_path.write_text(json.dumps(data))
    # Record one more event — should NOT promote
    daemon._record_exec_event(
        source="exec", mode="inline_code", target_profile="default",
        intent_signature="mock.read.frozen-test", script_ref=None,
        base_pipeline_id=None, verify_passed=True, human_fix=False,
        is_error=False,
    )
    data = json.loads(reg_path.read_text())
    assert data["pipelines"][key]["status"] == "explore"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_mcp_tools_integration.py::test_frozen_pipeline_skips_auto_promotion -v`
Expected: FAIL — no frozen check exists

- [ ] **Step 3: Implement frozen flag in daemon**

In `scripts/emerge_daemon.py`, in `_update_pipeline_registry`, after loading current entry and before the promotion/demotion logic block, add:

```python
if entry.get("frozen"):
    # Skip all auto-transitions when frozen
    self._atomic_write_json(registry_path, data)
    return
```

- [ ] **Step 4: Add frozen field to span candidates**

In `scripts/span_tracker.py`, in `_update_candidates`, add `"frozen": False` to the default entry dict (alongside `"skeleton_generated": False`). In `get_policy_status`, add early return:

```python
if entry.get("frozen"):
    return "explore"  # frozen intents stay at explore
```

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests -q`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add scripts/emerge_daemon.py scripts/span_tracker.py tests/test_mcp_tools_integration.py
git commit -m "feat: add frozen flag to pipelines and span candidates"
```

---

### Task 5: Control Plane Read API Endpoints

**Files:**
- Modify: `scripts/repl_admin.py`
- Create: `tests/test_cockpit_api.py`

- [ ] **Step 1: Write failing test for /api/control-plane/state**

```python
# tests/test_cockpit_api.py
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# Reuse session/state setup patterns from existing tests
from scripts.state_tracker import StateTracker, save_tracker, LEVEL_CORE_CRITICAL
from scripts.repl_admin import cmd_policy_status


def _make_state_json(tmp: Path, deltas=None, risks=None):
    tracker = StateTracker()
    for d in (deltas or []):
        tracker.add_delta(**d)
    for r in (risks or []):
        tracker.add_risk(r)
    save_tracker(tmp / "state.json", tracker)
    return tmp / "state.json"


def test_cmd_control_plane_state_returns_deltas_and_risks(tmp_path):
    from scripts.repl_admin import cmd_control_plane_state
    _make_state_json(tmp_path, deltas=[
        {"message": "test delta", "intent_signature": "mock.read.x"},
    ], risks=["test risk"])
    with patch("scripts.repl_admin.default_hook_state_root", return_value=str(tmp_path)):
        result = cmd_control_plane_state()
    assert result["ok"]
    assert len(result["deltas"]) == 1
    assert result["deltas"][0]["intent_signature"] == "mock.read.x"
    assert len(result["risks"]) == 1
    assert result["risks"][0]["text"] == "test risk"
    assert "verification_state" in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cockpit_api.py::test_cmd_control_plane_state_returns_deltas_and_risks -v`
Expected: FAIL — `cmd_control_plane_state` does not exist

- [ ] **Step 3: Implement read endpoints**

In `scripts/repl_admin.py`, add these functions:

```python
def cmd_control_plane_state() -> dict:
    """Full StateTracker snapshot for cockpit."""
    pin_plugin_data_path_if_present()
    state_path = Path(default_hook_state_root()) / "state.json"
    tracker = load_tracker(state_path)
    d = tracker.to_dict()
    return {
        "ok": True,
        "deltas": d.get("deltas", []),
        "risks": d.get("open_risks", []),
        "verification_state": d.get("verification_state", "verified"),
        "consistency_window_ms": d.get("consistency_window_ms", 0),
        "active_span_id": d.get("active_span_id"),
        "active_span_intent": d.get("active_span_intent"),
    }


def cmd_control_plane_intents() -> dict:
    """Merged intent list from pipelines-registry + span-candidates + exec candidates."""
    policy = cmd_policy_status()
    intents: dict[str, dict] = {}
    for p in policy.get("pipelines", []):
        key = p.get("key", "")
        if not key:
            continue
        intents[key] = {
            "intent_signature": key,
            "source": "exec",
            "policy_status": p.get("status", "explore"),
            "success_rate": p.get("success_rate"),
            "human_fix_rate": p.get("human_fix_rate"),
            "consecutive_failures": p.get("consecutive_failures", 0),
            "frozen": p.get("frozen", False),
            "updated_at_ms": p.get("updated_at_ms", 0),
        }
    # Merge span candidates
    state_root = _resolve_state_root()
    span_cand_path = state_root / "span-candidates.json"
    if span_cand_path.exists():
        try:
            sc = json.loads(span_cand_path.read_text(encoding="utf-8"))
            for key, entry in sc.get("spans", {}).items():
                if key in intents:
                    intents[key]["source"] = "both"
                    intents[key]["span_status"] = _span_policy_label(entry)
                else:
                    att = int(entry.get("attempts", 0))
                    succ = int(entry.get("successes", 0))
                    intents[key] = {
                        "intent_signature": key,
                        "source": "span",
                        "policy_status": _span_policy_label(entry),
                        "success_rate": round(succ / att, 4) if att else None,
                        "human_fix_rate": None,
                        "consecutive_failures": int(entry.get("consecutive_failures", 0)),
                        "frozen": entry.get("frozen", False),
                        "updated_at_ms": int(entry.get("last_ts_ms", 0)),
                    }
        except Exception:
            pass
    return {"ok": True, "intents": list(intents.values())}


def _span_policy_label(entry: dict) -> str:
    att = int(entry.get("attempts", 0))
    succ = int(entry.get("successes", 0))
    if att == 0:
        return "explore"
    rate = succ / att
    if att >= 40 and rate >= 0.97:
        return "stable"
    if att >= 20 and rate >= 0.95:
        return "canary"
    return "explore"


def cmd_control_plane_session() -> dict:
    """Session health: checkpoint + recovery + WAL stats."""
    session_dir, wal_path, checkpoint_path = _session_paths()
    wal_entries = 0
    if wal_path.exists():
        with wal_path.open("r", encoding="utf-8") as f:
            wal_entries = sum(1 for line in f if line.strip())
    checkpoint = None
    if checkpoint_path.exists():
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    recovery = None
    recovery_path = session_dir / "recovery.json"
    if recovery_path.exists():
        try:
            recovery = json.loads(recovery_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "ok": True,
        "session_id": _resolve_session_id(),
        "session_dir": str(session_dir),
        "wal_entries": wal_entries,
        "checkpoint": checkpoint,
        "recovery": recovery,
    }


def cmd_control_plane_exec_events(limit: int = 100, since_ms: int = 0, intent: str = "") -> dict:
    """Paginated exec events from session."""
    session_dir, _, _ = _session_paths()
    events_path = session_dir / "exec-events.jsonl"
    events: list[dict] = []
    if events_path.exists():
        for line in events_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if since_ms and int(ev.get("ts_ms", 0)) < since_ms:
                continue
            if intent and ev.get("intent_signature") != intent:
                continue
            events.append(ev)
    events.sort(key=lambda e: int(e.get("ts_ms", 0)), reverse=True)
    return {"ok": True, "events": events[:limit]}


def cmd_control_plane_pipeline_events(limit: int = 100, since_ms: int = 0, intent: str = "") -> dict:
    """Paginated pipeline events from session."""
    session_dir, _, _ = _session_paths()
    events_path = session_dir / "pipeline-events.jsonl"
    events: list[dict] = []
    if events_path.exists():
        for line in events_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if since_ms and int(ev.get("ts_ms", 0)) < since_ms:
                continue
            if intent and ev.get("intent_signature") != intent:
                continue
            events.append(ev)
    events.sort(key=lambda e: int(e.get("ts_ms", 0)), reverse=True)
    return {"ok": True, "events": events[:limit]}


def cmd_control_plane_spans(limit: int = 50, intent: str = "") -> dict:
    """Recent span WAL entries."""
    state_root = _resolve_state_root()
    wal_path = state_root / "span-wal" / "spans.jsonl"
    spans: list[dict] = []
    if wal_path.exists():
        for line in wal_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                sp = json.loads(line)
            except Exception:
                continue
            if intent and sp.get("intent_signature") != intent:
                continue
            spans.append(sp)
    spans.sort(key=lambda s: int(s.get("closed_at_ms", 0) or 0), reverse=True)
    return {"ok": True, "spans": spans[:limit]}


def cmd_control_plane_span_candidates() -> dict:
    """All span candidate entries."""
    state_root = _resolve_state_root()
    path = state_root / "span-candidates.json"
    if not path.exists():
        return {"ok": True, "candidates": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {"ok": True, "candidates": data.get("spans", {})}
    except Exception:
        return {"ok": True, "candidates": {}}
```

- [ ] **Step 4: Wire endpoints into HTTP handler**

In `_CockpitHandler.do_GET`, add before the `else: self._err(404)`:

```python
elif path == "/api/control-plane/state":
    self._json(cmd_control_plane_state())
elif path == "/api/control-plane/intents":
    self._json(cmd_control_plane_intents())
elif path == "/api/control-plane/session":
    self._json(cmd_control_plane_session())
elif path.startswith("/api/control-plane/exec-events"):
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    self._json(cmd_control_plane_exec_events(
        limit=int(qs.get("limit", ["100"])[0]),
        since_ms=int(qs.get("since_ms", ["0"])[0]),
        intent=qs.get("intent", [""])[0],
    ))
elif path.startswith("/api/control-plane/pipeline-events"):
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    self._json(cmd_control_plane_pipeline_events(
        limit=int(qs.get("limit", ["100"])[0]),
        since_ms=int(qs.get("since_ms", ["0"])[0]),
        intent=qs.get("intent", [""])[0],
    ))
elif path.startswith("/api/control-plane/spans"):
    if path == "/api/control-plane/span-candidates":
        self._json(cmd_control_plane_span_candidates())
    else:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        self._json(cmd_control_plane_spans(
            limit=int(qs.get("limit", ["50"])[0]),
            intent=qs.get("intent", [""])[0],
        ))
```

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_cockpit_api.py tests -q`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add scripts/repl_admin.py tests/test_cockpit_api.py
git commit -m "feat: add control-plane read API endpoints for state, intents, session, events, spans"
```

---

### Task 6: Control Plane Write API Endpoints

**Files:**
- Modify: `scripts/repl_admin.py`
- Modify: `tests/test_cockpit_api.py`

- [ ] **Step 1: Write failing test for delta reconcile endpoint**

```python
# In tests/test_cockpit_api.py

def test_cmd_control_plane_delta_reconcile(tmp_path):
    from scripts.repl_admin import cmd_control_plane_delta_reconcile
    tracker = StateTracker()
    delta_id = tracker.add_delta(message="test", intent_signature="mock.read.x")
    save_tracker(tmp_path / "state.json", tracker)
    with patch("scripts.repl_admin.default_hook_state_root", return_value=str(tmp_path)):
        result = cmd_control_plane_delta_reconcile(delta_id=delta_id, outcome="confirm")
    assert result["ok"]
    assert result["outcome"] == "confirm"


def test_cmd_control_plane_risk_update(tmp_path):
    from scripts.repl_admin import cmd_control_plane_risk_update
    tracker = StateTracker()
    tracker.add_risk("test risk")
    save_tracker(tmp_path / "state.json", tracker)
    risk_id = tracker.to_dict()["open_risks"][0]["risk_id"]
    with patch("scripts.repl_admin.default_hook_state_root", return_value=str(tmp_path)):
        result = cmd_control_plane_risk_update(risk_id=risk_id, action="handle", reason="fixed")
    assert result["ok"]


def test_cmd_control_plane_policy_freeze(tmp_path):
    from scripts.repl_admin import cmd_control_plane_policy_freeze
    reg = {"pipelines": {"mock.read.layers": {"status": "explore"}}}
    (tmp_path / "pipelines-registry.json").write_text(json.dumps(reg))
    with patch("scripts.repl_admin._resolve_state_root", return_value=tmp_path):
        result = cmd_control_plane_policy_freeze(key="mock.read.layers")
    assert result["ok"]
    updated = json.loads((tmp_path / "pipelines-registry.json").read_text())
    assert updated["pipelines"]["mock.read.layers"]["frozen"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_cockpit_api.py::test_cmd_control_plane_delta_reconcile tests/test_cockpit_api.py::test_cmd_control_plane_risk_update tests/test_cockpit_api.py::test_cmd_control_plane_policy_freeze -v`
Expected: FAIL

- [ ] **Step 3: Implement write endpoints**

In `scripts/repl_admin.py`:

```python
def cmd_control_plane_delta_reconcile(delta_id: str, outcome: str, intent_signature: str = "") -> dict:
    pin_plugin_data_path_if_present()
    state_path = Path(default_hook_state_root()) / "state.json"
    tracker = load_tracker(state_path)
    tracker.reconcile_delta(delta_id, outcome)
    save_tracker(state_path, tracker)
    return {"ok": True, "delta_id": delta_id, "outcome": outcome}


def cmd_control_plane_risk_update(
    risk_id: str, action: str, reason: str = "", snooze_duration_ms: int = 3600000,
) -> dict:
    pin_plugin_data_path_if_present()
    state_path = Path(default_hook_state_root()) / "state.json"
    tracker = load_tracker(state_path)
    tracker.update_risk(risk_id, action=action, reason=reason or None, snooze_duration_ms=snooze_duration_ms)
    save_tracker(state_path, tracker)
    return {"ok": True, "risk_id": risk_id, "action": action}


def cmd_control_plane_risk_add(text: str, intent_signature: str = "") -> dict:
    pin_plugin_data_path_if_present()
    state_path = Path(default_hook_state_root()) / "state.json"
    tracker = load_tracker(state_path)
    tracker.add_risk(text, intent_signature=intent_signature or None)
    save_tracker(state_path, tracker)
    return {"ok": True, "text": text}


def cmd_control_plane_policy_freeze(key: str) -> dict:
    state_root = _resolve_state_root()
    registry_path, data = _load_registry(state_root)
    if key not in data.get("pipelines", {}):
        return {"ok": False, "error": f"pipeline {key!r} not found"}
    data["pipelines"][key]["frozen"] = True
    _save_registry(registry_path, data)
    return {"ok": True, "key": key, "frozen": True}


def cmd_control_plane_policy_unfreeze(key: str) -> dict:
    state_root = _resolve_state_root()
    registry_path, data = _load_registry(state_root)
    if key not in data.get("pipelines", {}):
        return {"ok": False, "error": f"pipeline {key!r} not found"}
    data["pipelines"][key]["frozen"] = False
    _save_registry(registry_path, data)
    return {"ok": True, "key": key, "frozen": False}


def cmd_control_plane_session_export() -> dict:
    pin_plugin_data_path_if_present()
    state_path = Path(default_hook_state_root()) / "state.json"
    tracker = load_tracker(state_path)
    session_dir, wal_path, checkpoint_path = _session_paths()
    snapshot = {
        "state_tracker": tracker.to_dict(),
        "session_id": _resolve_session_id(),
    }
    if checkpoint_path.exists():
        try:
            snapshot["checkpoint"] = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"ok": True, "snapshot": snapshot}


def cmd_control_plane_session_reset(confirm: str) -> dict:
    if confirm != "RESET":
        return {"ok": False, "error": "must pass confirm='RESET'"}
    export = cmd_control_plane_session_export()
    pin_plugin_data_path_if_present()
    state_path = Path(default_hook_state_root()) / "state.json"
    from scripts.state_tracker import StateTracker as ST
    save_tracker(state_path, ST())
    return {"ok": True, "reset": True, "pre_reset_snapshot": export.get("snapshot")}
```

- [ ] **Step 4: Wire write endpoints into HTTP handler**

In `_CockpitHandler.do_POST`, add before `else: self._err(404)`:

```python
elif path == "/api/control-plane/delta/reconcile":
    self._json(cmd_control_plane_delta_reconcile(
        delta_id=str(body.get("delta_id", "")),
        outcome=str(body.get("outcome", "")),
        intent_signature=str(body.get("intent_signature", "")),
    ))
elif path == "/api/control-plane/risk/update":
    self._json(cmd_control_plane_risk_update(
        risk_id=str(body.get("risk_id", "")),
        action=str(body.get("action", "")),
        reason=str(body.get("reason", "")),
        snooze_duration_ms=int(body.get("snooze_duration_ms", 3600000)),
    ))
elif path == "/api/control-plane/risk/add":
    self._json(cmd_control_plane_risk_add(
        text=str(body.get("text", "")),
        intent_signature=str(body.get("intent_signature", "")),
    ))
elif path == "/api/control-plane/policy/freeze":
    self._json(cmd_control_plane_policy_freeze(key=str(body.get("key", ""))))
elif path == "/api/control-plane/policy/unfreeze":
    self._json(cmd_control_plane_policy_unfreeze(key=str(body.get("key", ""))))
elif path == "/api/control-plane/session/export":
    self._json(cmd_control_plane_session_export())
elif path == "/api/control-plane/session/reset":
    self._json(cmd_control_plane_session_reset(confirm=str(body.get("confirm", ""))))
```

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests -q`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add scripts/repl_admin.py tests/test_cockpit_api.py
git commit -m "feat: add control-plane write API endpoints for delta, risk, policy, session"
```

---

### Task 7: Frontend — Overview Tab with Intent Table

**Files:**
- Modify: `scripts/cockpit_shell.html`

- [ ] **Step 1: Add new state variables and fetch functions**

At the top of the `<script>` block, after existing state variables, add:

```javascript
let stateData = null;
let intentsData = null;
```

Add fetch functions:

```javascript
async function refreshState() {
  try {
    const r = await fetch('/api/control-plane/state');
    stateData = await r.json();
  } catch(e) {}
}

async function refreshIntents() {
  try {
    const r = await fetch('/api/control-plane/intents');
    intentsData = await r.json();
  } catch(e) {}
}
```

Add `refreshState()` and `refreshIntents()` to the `init()` Promise.all and the 5s interval.

- [ ] **Step 2: Replace Overview rendering with intent table + stat cards**

Replace the `renderOverviewTab()` function body. Keep the existing donut and stat cards, but add above the pipeline sections:

1. A 4-card stat strip (Total Intents, Degraded, Open Risks, Unreconciled Deltas)
2. An intent signature table (sortable by column)
3. Keep the existing pipeline breakdown below

The stat cards use `stateData` for deltas/risks and `intentsData` for intent counts.
The intent table rows are clickable — `onclick` navigates to the connector tab for that intent.

- [ ] **Step 3: Verify in browser**

Open cockpit at `http://127.0.0.1:8787`. The Overview tab should now show stat cards + intent table above the existing pipeline sections. Existing donut/pipeline display is preserved below.

- [ ] **Step 4: Commit**

```bash
git add scripts/cockpit_shell.html
git commit -m "feat: cockpit Overview tab with intent table and stat cards"
```

---

### Task 8: Frontend — Connector Sub-panels (Deltas, Risks, Spans, Exec Events)

**Files:**
- Modify: `scripts/cockpit_shell.html`

- [ ] **Step 1: Add sub-panel tabs to connector view**

In the connector tab rendering function, add 4 new sub-tabs after the existing `Pipelines | Notes | Controls`:

```
Pipelines | Notes | Controls | Deltas | Risks | Spans | Exec Events
```

Each new sub-tab shows a count badge when data exists.

- [ ] **Step 2: Implement Deltas sub-panel**

Fetch deltas from `stateData.deltas`, filter by connector prefix. Render each delta with:
- Level dot (red/yellow/grey)
- Message + provisional tag
- intent_signature + ts_ms
- Confirm / Correct / Retract buttons (queue actions via existing `onActionSelect` pattern)

- [ ] **Step 3: Implement Risks sub-panel**

Fetch risks from `stateData.risks`, filter by connector. Render each with:
- Warning icon + text + status badge
- Handle / Snooze buttons

- [ ] **Step 4: Implement Spans sub-panel**

Fetch from `/api/control-plane/spans?intent=<connector>.*`. Render span cards with:
- Policy badge + intent name + outcome badge
- Action count + side-effect count + duration
- Tool sequence preview

- [ ] **Step 5: Implement Exec Events sub-panel**

Fetch from `/api/control-plane/exec-events?intent=<connector>.*`. Render as compact event rows:
- ts | intent | mode | outcome (success/error) | verify | profile

- [ ] **Step 6: Verify all existing panels still work**

Open cockpit, switch between Pipelines / Notes / Controls / Deltas / Risks / Spans / Exec Events. Existing panels must render identically to before.

- [ ] **Step 7: Commit**

```bash
git add scripts/cockpit_shell.html
git commit -m "feat: cockpit connector sub-panels for deltas, risks, spans, exec events"
```

---

### Task 9: Frontend — Audit, Session, Operator Global Tabs

**Files:**
- Modify: `scripts/cockpit_shell.html`

- [ ] **Step 1: Add Audit tab**

New global tab that fetches and merges:
- `/api/control-plane/exec-events`
- `/api/control-plane/pipeline-events`
- `/api/goal-history`
- `/api/control-plane/spans`

Merge all by `ts_ms`, render as unified timeline with:
- Filters: time range, intent, object type, outcome
- Each row: timestamp | type icon | intent | action | summary

Lazy loading: only fetch when tab is active, refresh every 10s.

- [ ] **Step 2: Add Session tab**

Fetch `/api/control-plane/session`. Render:
- Session ID, WAL entry count
- Checkpoint card (wal_seq, state_hash, updated_at)
- Recovery card (degraded?, issues collapsible)
- Export button (calls `/api/control-plane/session/export`, triggers JSON download)
- Reset button (modal with typed "RESET" confirm)

- [ ] **Step 3: Add Operator tab**

Placeholder for now — show "Operator Intelligence Loop" header with:
- "Enable with EMERGE_OPERATOR_MONITOR=1" guidance
- If data exists at `/api/control-plane/operator-events`, show event stream

- [ ] **Step 4: Verify all tabs render and switch correctly**

- [ ] **Step 5: Commit**

```bash
git add scripts/cockpit_shell.html
git commit -m "feat: cockpit Audit, Session, Operator global tabs"
```

---

### Task 10: Confirmation Modals and Safety Gates

**Files:**
- Modify: `scripts/cockpit_shell.html`

- [ ] **Step 1: Add confirmation modal component**

A reusable modal that supports three tiers:
- Silent: no modal (inline action)
- Warn: single-click confirm with yellow highlight
- Block: modal with before/after diff, optional typed confirm

- [ ] **Step 2: Wire destructive actions to Block tier**

Actions that show Block modal: retract delta, promote stable, freeze, delete pipeline, reset session, rollback goal.

- [ ] **Step 3: Wire medium-risk actions to Warn tier**

Actions with Warn highlight: correct delta, promote canary, demote, reset failures, unfreeze.

- [ ] **Step 4: Test all confirmation flows in browser**

- [ ] **Step 5: Commit**

```bash
git add scripts/cockpit_shell.html
git commit -m "feat: cockpit confirmation modals and safety gates"
```

---

### Task 11: Documentation and Test Baseline Update

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update README MCP surface and Resources**

Add `state://deltas` visibility note. Update cockpit description to mention control plane.

- [ ] **Step 2: Update README env var table**

No new env vars needed (all endpoints are on existing cockpit server).

- [ ] **Step 3: Update test badge**

Run `python -m pytest tests -q` and update the badge count in README.md.

- [ ] **Step 4: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs: update README and CLAUDE.md for cockpit control plane"
```
