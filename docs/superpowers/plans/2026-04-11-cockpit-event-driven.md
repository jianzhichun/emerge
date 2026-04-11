# Cockpit Event-Driven Overhaul Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the dead `cc_listening` gate that permanently disables the cockpit submit button, upgrade the frontend from polling to SSE for real-time status, add span safety to session reset, and clean up stale messaging.

**Architecture:** The backend already has SSE infrastructure (`/api/sse/status`, `_sse_broadcast()`) but the frontend doesn't use it. The `cc_listening: false` field in `/api/status` is hardcoded and was designed for a `wait-for-submit` mode that no longer exists — the daemon uses `EventRouter` + MCP channel notifications now. The fix: (1) remove `cc_listening` from the API contract, (2) wire SSE into the frontend for real-time `pending` state, (3) guard session reset against active spans, (4) clean up obsolete text. Tests are adapted — the existing `test_serve_get_status_returns_ok` test checks `cc_listening in data`, so it must be updated too.

**Tech Stack:** Python 3.11+ (backend: `scripts/repl_admin.py`), vanilla JS (frontend: `scripts/cockpit_shell.html`), SSE (`EventSource` API), existing `_sse_broadcast()` helper.

---

## File Map

| File | Change |
|------|--------|
| `scripts/repl_admin.py` | Remove `cc_listening` from `/api/status`; broadcast `pending` changes via SSE |
| `scripts/cockpit_shell.html` | Remove `ccListening`/`ccListeningSignalKnown`; add `EventSource` for SSE; unify submit-disable logic |
| `tests/test_cockpit_server.py` | Update `test_serve_get_status_returns_ok`; new span-guard test for session reset |
| `tests/test_cockpit_sse.py` | New: test pending-broadcast via SSE |
| `CLAUDE.md` | Update Architecture section with cockpit SSE; add Key Invariant |
| `README.md` | Update cockpit description in component table |

---

### Task 1: Remove `cc_listening` from `/api/status` response

The `cc_listening: False` is hardcoded and permanently disables the submit button. Replace it with `server_online: True` to indicate the cockpit server is alive without implying CC is in a "listening" mode.

**Files:**
- Modify: `scripts/repl_admin.py:1697-1700`
- Test: `tests/test_cockpit_server.py`

- [ ] **Step 1: Update the test to match the new API contract**

In `tests/test_cockpit_server.py`, find `test_serve_get_status_returns_ok` (lines 157–163) and replace:

```python
def test_serve_get_status_returns_ok(tmp_path, monkeypatch):
    url = _start_test_server(tmp_path, monkeypatch)
    with urllib.request.urlopen(f"{url}/api/status") as resp:
        data = json.loads(resp.read())
    assert data["ok"] is True
    assert "cc_listening" not in data, "cc_listening removed — use server_online"
    assert data["server_online"] is True
    assert isinstance(data["pending"], bool)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_cockpit_server.py::test_serve_get_status_returns_ok -xvs
```

Expected: `FAILED` — `cc_listening` still present, `server_online` missing.

- [ ] **Step 3: Update `/api/status` in `repl_admin.py`**

In `scripts/repl_admin.py`, find line 1700 and change:

```python
        elif path == "/api/status":
            state_root = _resolve_repl_root()
            pending = (state_root / "pending-actions.json").exists()
            self._json({"ok": True, "pending": pending, "server_online": True})
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_cockpit_server.py::test_serve_get_status_returns_ok -xvs
```

Expected: `PASSED`

- [ ] **Step 5: Run full suite to check for regressions**

```bash
python -m pytest tests/ -q --tb=short
```

Expected: 476 passed (no regressions — nothing else references `cc_listening` server-side).

- [ ] **Step 6: Commit**

```bash
git add scripts/repl_admin.py tests/test_cockpit_server.py
git commit -m "fix: remove hardcoded cc_listening:false from /api/status — replace with server_online"
```

---

### Task 2: Backend — broadcast pending state changes via SSE

When `pending-actions.json` is submitted or processed, broadcast the change to all SSE clients so the frontend gets real-time updates instead of polling every 3 seconds.

**Files:**
- Modify: `scripts/repl_admin.py` (after `cmd_submit_actions` write and in the POST `/api/submit` handler)
- Test: `tests/test_cockpit_sse.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cockpit_sse.py`:

```python
def test_sse_broadcast_pending_on_submit(tmp_path, monkeypatch):
    """POST /api/submit must broadcast a pending=true event via SSE."""
    import scripts.repl_admin as repl_admin
    url = _start_cockpit_server(tmp_path, monkeypatch)
    resp = urllib.request.urlopen(f"{url}/api/sse/status", timeout=3)
    resp.readline()  # data: {status: online}\n
    resp.readline()  # blank separator

    deadline = time.time() + 2.0
    while not repl_admin._sse_clients and time.time() < deadline:
        time.sleep(0.01)

    body = json.dumps({"actions": [{"type": "pipeline-delete", "key": "x"}]}).encode()
    req = urllib.request.Request(
        f"{url}/api/submit", data=body,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req) as r:
        assert json.loads(r.read())["ok"] is True

    time.sleep(0.2)
    line = resp.readline().decode().strip()
    assert line.startswith("data:")
    data = json.loads(line[5:])
    assert data["pending"] is True
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_cockpit_sse.py::test_sse_broadcast_pending_on_submit -xvs
```

Expected: `FAILED` — no pending event broadcast after submit.

- [ ] **Step 3: Add SSE broadcast to the POST `/api/submit` handler**

In `scripts/repl_admin.py`, find the `POST /api/submit` handler (around line 1787–1794). After the `self._json(result)` call, add the broadcast:

```python
        if path == "/api/submit":
            actions = body.get("actions", [])
            result = cmd_submit_actions(actions)
            self._json(result)
            if result.get("ok"):
                _sse_broadcast({"pending": True, "action_count": result.get("action_count", 0),
                                "ts_ms": int(time.time() * 1000)})
```

Note: the `_sse_broadcast` call must happen **after** `self._json(result)` so the HTTP response is sent first. The SSE broadcast goes to already-connected clients on a separate channel.

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_cockpit_sse.py::test_sse_broadcast_pending_on_submit -xvs
```

Expected: `PASSED`

- [ ] **Step 5: Run full suite**

```bash
python -m pytest tests/ -q --tb=short
```

Expected: 477 passed (one new test).

- [ ] **Step 6: Commit**

```bash
git add scripts/repl_admin.py tests/test_cockpit_sse.py
git commit -m "feat: broadcast pending state changes via SSE on submit"
```

---

### Task 3: Frontend — remove `ccListening` gate, add SSE for real-time status

This is the core frontend change. Remove the dead `ccListening`/`ccListeningSignalKnown` mechanism, wire up `EventSource` for SSE status, and simplify the submit-disable logic.

**Files:**
- Modify: `scripts/cockpit_shell.html`

- [ ] **Step 1: Remove `ccListening` state variables and `updateCcIndicator()`**

In `scripts/cockpit_shell.html`, find and remove/replace:

**Line 683–684** — replace the state variables:
```javascript
let ccListening = false;  // whether wait-for-submit is active in CC
let ccListeningSignalKnown = true; // false when connected to legacy /api/status
```
with:
```javascript
let sseConnected = false;  // true when EventSource is open
let serverPending = false; // true when pending-actions.json exists server-side
```

**Lines 1030–1045** — replace `updateCcIndicator()`:
```javascript
function updateCcIndicator() {
  const el = document.getElementById('cc-indicator');
  if (!el) return;
  if (sseConnected) {
    el.textContent = '● Server online';
    el.style.color = '#3fb950';
  } else {
    el.textContent = '◐ Connecting to server…';
    el.style.color = '#d29922';
  }
}
```

- [ ] **Step 2: Add SSE connection setup**

Add a new function after `updateCcIndicator()`:

```javascript
function connectSSE() {
  const es = new EventSource('/api/sse/status');
  es.onopen = () => {
    sseConnected = true;
    updateCcIndicator();
  };
  es.onmessage = (evt) => {
    try {
      const d = JSON.parse(evt.data);
      if (Object.prototype.hasOwnProperty.call(d, 'pending')) {
        serverPending = !!d.pending;
        updateSubmitState();
      }
    } catch(e) {}
  };
  es.onerror = () => {
    sseConnected = false;
    updateCcIndicator();
    es.close();
    setTimeout(connectSSE, 3000);
  };
}
```

- [ ] **Step 3: Simplify `refreshStatus()`**

Replace the current `refreshStatus()` (lines 866–893) with:

```javascript
async function refreshStatus() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    serverPending = !!d.pending;
  } catch(e) {
    // SSE will track liveness; status poll is only for pending state fallback
  }
  updateSubmitState();
}
```

- [ ] **Step 4: Create unified `updateSubmitState()` function**

Add after `refreshStatus()`:

```javascript
function updateSubmitState() {
  const btn = document.getElementById('submit-btn');
  const statusMsg = document.getElementById('status-msg');
  if (serverPending && statusMsg && !statusMsg.textContent.startsWith('✓')) {
    statusMsg.textContent = '⏳ CC is processing previous submission…';
  } else if (!serverPending && statusMsg && statusMsg.textContent.startsWith('⏳')) {
    statusMsg.textContent = '';
  }
  if (btn) btn.disabled = queue.length === 0 || serverPending;
}
```

- [ ] **Step 5: Update `renderQueue()` submit-disable logic**

Replace lines 2566–2573:

```javascript
function renderQueue() {
  const count = queue.length;
  document.getElementById('queue-count').textContent = count;
  const btn = document.getElementById('submit-btn');
  btn.disabled = count === 0 || serverPending;
  btn.title = '';
  btn.textContent = `✓ Submit to CC (${count})`;
```

- [ ] **Step 6: Update `init()` to start SSE and reduce status polling**

In the `init()` function (lines 759–788), replace the `setInterval(refreshStatus, 3000)` line with:

```javascript
  connectSSE();
  setInterval(refreshStatus, 10000);  // fallback poll every 10s (SSE is primary)
```

- [ ] **Step 7: Run the cockpit server manually and verify the UI**

```bash
cd /Users/apple/Documents/workspace/emerge
EMERGE_CONNECTOR_ROOT=~/.emerge/connectors python3 scripts/repl_admin.py serve --port 9999 --no-open
```

Open `http://localhost:9999` in a browser. Verify:
- CC indicator shows "● Server online" (green)
- Submit button is enabled when queue has items
- Submit button is properly disabled when queue is empty

- [ ] **Step 8: Commit**

```bash
git add scripts/cockpit_shell.html
git commit -m "feat: cockpit frontend — replace ccListening gate with SSE-based status"
```

---

### Task 4: Session reset span safety guard

The cockpit session reset (`/api/control-plane/session/reset`) calls `save_tracker(state_path, StateTracker())` which blanks the entire state including `active_span_id`. This silently breaks an open flywheel span. The Stop hook guards CC stop, but the cockpit reset bypasses it.

**Files:**
- Modify: `scripts/repl_admin.py:1507-1533`
- Test: `tests/test_cockpit_server.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cockpit_server.py`:

```python
def test_session_reset_blocked_when_span_active(tmp_path, monkeypatch):
    """session/reset must refuse when active_span_id is present in state."""
    monkeypatch.setenv("EMERGE_REPL_ROOT", str(tmp_path))
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(tmp_path / "connectors"))
    (tmp_path / "connectors").mkdir(exist_ok=True)

    from scripts.policy_config import default_hook_state_root, pin_plugin_data_path_if_present
    from scripts.state_tracker import load_tracker, save_tracker

    pin_plugin_data_path_if_present()
    state_path = Path(default_hook_state_root()) / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tracker = load_tracker(state_path)
    tracker.state["active_span_id"] = "span-123"
    tracker.state["active_span_intent"] = "gmail.read.fetch"
    save_tracker(state_path, tracker)

    from scripts.repl_admin import cmd_control_plane_session_reset
    result = cmd_control_plane_session_reset(confirm="RESET")

    assert result["ok"] is False
    assert "active_span" in result.get("error", "").lower() or "span" in result.get("error", "").lower()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_cockpit_server.py::test_session_reset_blocked_when_span_active -xvs
```

Expected: `FAILED` — reset succeeds even with active span.

- [ ] **Step 3: Add span guard to `cmd_control_plane_session_reset`**

In `scripts/repl_admin.py`, find `cmd_control_plane_session_reset` (line 1507). After the `confirm != "RESET"` check, add:

```python
def cmd_control_plane_session_reset(confirm: str, full: bool = False) -> dict:
    if confirm != "RESET":
        return {"ok": False, "error": "must pass confirm='RESET'"}
    pin_plugin_data_path_if_present()
    state_path = Path(default_hook_state_root()) / "state.json"
    try:
        tracker = load_tracker(state_path)
        if tracker.state.get("active_span_id"):
            return {
                "ok": False,
                "error": "active_span_open",
                "message": (
                    f"Cannot reset while span is active "
                    f"(intent={tracker.state.get('active_span_intent', '?')}). "
                    "Close or abort the span first via icc_span_close(outcome='aborted')."
                ),
            }
    except Exception:
        pass
    export = cmd_control_plane_session_export()
    save_tracker(state_path, StateTracker())
```

Note: `pin_plugin_data_path_if_present()` was already called on line 1511 — move it above the span check since `default_hook_state_root()` needs it. Remove the duplicate call that was on line 1511.

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_cockpit_server.py::test_session_reset_blocked_when_span_active -xvs
```

Expected: `PASSED`

- [ ] **Step 5: Run full suite**

```bash
python -m pytest tests/ -q --tb=short
```

Expected: 478 passed.

- [ ] **Step 6: Commit**

```bash
git add scripts/repl_admin.py tests/test_cockpit_server.py
git commit -m "fix: block cockpit session reset when flywheel span is active"
```

---

### Task 5: Tests — verify SSE status flow end-to-end

**Files:**
- Test: `tests/test_cockpit_sse.py`

- [ ] **Step 1: Add SSE online event test with `server_online` field**

Add to `tests/test_cockpit_sse.py`:

```python
def test_sse_initial_event_has_status_online(tmp_path, monkeypatch):
    """Initial SSE event must have status=online with pid and ts_ms."""
    url = _start_cockpit_server(tmp_path, monkeypatch)
    resp = urllib.request.urlopen(f"{url}/api/sse/status", timeout=3)
    line = resp.readline().decode().strip()
    assert line.startswith("data:")
    data = json.loads(line[5:])
    assert data["status"] == "online"
    assert isinstance(data["pid"], int)
    assert isinstance(data["ts_ms"], int)
```

- [ ] **Step 2: Run all SSE tests**

```bash
python -m pytest tests/test_cockpit_sse.py -xvs
```

Expected: all 4 tests pass (2 existing + 2 new from Tasks 2 and 5).

- [ ] **Step 3: Run full suite**

```bash
python -m pytest tests/ -q --tb=short
```

Expected: 479 passed.

- [ ] **Step 4: Commit**

```bash
git add tests/test_cockpit_sse.py
git commit -m "test: add SSE pending-broadcast and initial-event regression tests"
```

---

### Task 6: Documentation — update CLAUDE.md and README.md

**Files:**
- Modify: `CLAUDE.md`
- Modify: `README.md`

- [ ] **Step 1: Update CLAUDE.md Key Invariants**

Add after the existing cockpit control plane entry:

```markdown
- **Cockpit status contract**: `/api/status` returns `{ok, pending, server_online}`. The `cc_listening` field is removed — submit availability is determined only by `queue.length > 0 && !serverPending`. Frontend uses SSE (`/api/sse/status`) as primary status channel; `/api/status` is a 10s fallback poll for the `pending` flag. `_sse_broadcast()` pushes `{pending: true/false}` on submission and processing events.
- **Cockpit session reset span guard**: `cmd_control_plane_session_reset` checks `active_span_id` in state before resetting. If a span is open, returns `{ok: false, error: "active_span_open"}`. Mirrors the Stop hook safety contract.
```

- [ ] **Step 2: Update CLAUDE.md Architecture section**

In the **Cockpit control plane** entry, update to mention SSE:

```markdown
**Cockpit control plane**: `repl_admin.py` exposes `/api/control-plane/*` read endpoints (state, intents, session, exec-events, pipeline-events, spans, span-candidates) and write endpoints (delta/reconcile, risk/update, risk/add, policy/freeze, policy/unfreeze, session/export, session/reset). `/api/sse/status` streams real-time events (online, pending-state changes) via Server-Sent Events; `_sse_broadcast()` pushes to all connected clients. The cockpit HTML has an Overview intent table, connector sub-panels (Deltas/Risks/Spans/Exec Events), and global Audit/Session/Operator tabs.
```

- [ ] **Step 3: Update README.md cockpit description**

In the component table, update the Cockpit row to mention SSE-based status and the removal of `cc_listening`.

- [ ] **Step 4: Add Documentation Update Rule to CLAUDE.md**

Add to the documentation update rules table:

```markdown
| Cockpit API contract change | `repl_admin.py` endpoint + `cockpit_shell.html` consumer + `CLAUDE.md` Architecture section |
```

- [ ] **Step 5: Run full suite final verification**

```bash
python -m pytest tests/ -q --tb=short
```

Expected: 479 passed.

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md README.md
git commit -m "docs: update cockpit architecture — SSE status, cc_listening removal, session reset span guard"
```
