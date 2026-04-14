# Phase 2: PatternDetector 统一流 + daemon/cockpit 进程合并 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire PatternDetector into the runner push path, write pattern alerts to unified event streams, and merge the cockpit HTTP server into the daemon process to eliminate file-based IPC.

**Architecture:** `DaemonHTTPServer._on_runner_event` maintains per-runner sliding-window event buffers and runs `PatternDetector.ingest()` on every push, writing `type=pattern_alert` directly to `events-{profile}.jsonl`. `OperatorMonitor.process_local_file` drops its `push_fn` callback and writes `type=local_pattern_alert` directly to `events-local.jsonl`. `CockpitHTTPServer` (new class in `repl_admin.py`) holds the SSE client list and injected HTML state; `run_http()` in `emerge_daemon.py` starts it in-process so `broadcast()` can be called directly on pattern detection and runner connect/disconnect.

**Tech Stack:** Python 3.10+, stdlib `threading`, `collections.deque`, `http.server`, existing `PatternDetector`, `SpanTracker.get_policy_status`.

---

## File Map

| File | Type | What changes |
|---|---|---|
| `scripts/operator_monitor.py` | Modify | Add `state_root`, make `push_fn` optional → None, `process_local_file` writes `events-local.jsonl` |
| `scripts/daemon_http.py` | Modify | Add `_detector`, `_runner_event_buffers`, detection logic in `_on_runner_event`, `broadcast` calls on runner online/offline |
| `scripts/repl_admin.py` | Modify | Add `CockpitHTTPServer` class + `_StandaloneDaemonStub`; `_make_cockpit_handler(cockpit)` factory; refactor `_CockpitHandler` to use `self._cockpit`; update `cmd_serve()`, `_cockpit_list_injected_html`, `_cockpit_inject_html`, `cmd_assets` |
| `scripts/emerge_daemon.py` | Modify | Delete `_push_pattern`, `_build_explore_message`, `_ensure_cockpit`; pass `state_root` to `OperatorMonitor`; `run_http()` starts `CockpitHTTPServer` in-process |
| `tests/test_operator_monitor.py` | Modify | Replace push_fn assertions with events-local.jsonl file checks |
| `tests/test_daemon_http.py` | Modify | Add pattern detection and broadcast tests |
| `tests/test_repl_admin.py` | Modify | Add CockpitHTTPServer tests |
| `CLAUDE.md` | Modify | Update Key Invariants for CockpitHTTPServer, pattern alert routing |
| `README.md` | Modify | Update component table and architecture diagram |

---

## Task 1: OperatorMonitor — add `state_root`, make `push_fn` optional, write events-local.jsonl

**Files:**
- Modify: `scripts/operator_monitor.py`
- Modify: `tests/test_operator_monitor.py`

- [ ] **Step 1: Write failing tests**

Replace the contents of `tests/test_operator_monitor.py` with:

```python
# tests/test_operator_monitor.py
from __future__ import annotations
import json
import sys
import time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.operator_monitor import OperatorMonitor


def _make_events(n: int, tmp_path: Path) -> Path:
    """Write n operator events to tmp_path/operator-events/m1/events.jsonl, return path."""
    now_ms = int(time.time() * 1000)
    events = [
        {
            "ts_ms": now_ms - i * 60_000,
            "machine_id": "m1",
            "session_role": "operator",
            "event_type": "entity_added",
            "app": "zwcad",
            "payload": {"layer": "标注", "content": f"room_{i}"},
        }
        for i in range(n)
    ]
    machine_dir = tmp_path / "operator-events" / "m1"
    machine_dir.mkdir(parents=True)
    events_file = machine_dir / "events.jsonl"
    events_file.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return events_file


def test_process_local_file_writes_events_local_jsonl(tmp_path):
    """process_local_file writes local_pattern_alert to events-local.jsonl (no push_fn)."""
    state_root = tmp_path / "repl"
    state_root.mkdir()
    events_file = _make_events(3, tmp_path)

    monitor = OperatorMonitor(
        machines={},
        poll_interval_s=0.05,
        event_root=tmp_path / "operator-events",
        adapter_root=tmp_path / "adapters",
        state_root=state_root,
    )
    monitor.process_local_file(events_file)

    events_local = state_root / "events-local.jsonl"
    assert events_local.exists(), "events-local.jsonl must be written"
    lines = [json.loads(l) for l in events_local.read_text().splitlines() if l.strip()]
    assert len(lines) >= 1
    alert = lines[0]
    assert alert["type"] == "local_pattern_alert"
    assert alert["stage"] == "explore"
    assert "intent_signature" in alert
    assert alert["meta"]["occurrences"] >= 3


def test_process_local_file_no_events_does_not_write(tmp_path):
    """No events → events-local.jsonl not created."""
    state_root = tmp_path / "repl"
    state_root.mkdir()
    machine_dir = tmp_path / "operator-events" / "m1"
    machine_dir.mkdir(parents=True)
    events_file = machine_dir / "events.jsonl"
    events_file.write_text("")  # empty

    monitor = OperatorMonitor(
        machines={},
        poll_interval_s=0.05,
        event_root=tmp_path / "operator-events",
        adapter_root=tmp_path / "adapters",
        state_root=state_root,
    )
    monitor.process_local_file(events_file)

    assert not (state_root / "events-local.jsonl").exists()


def test_operator_monitor_stops_cleanly(tmp_path):
    """start() / stop() lifecycle works without push_fn."""
    state_root = tmp_path / "repl"
    state_root.mkdir()
    monitor = OperatorMonitor(
        machines={},
        poll_interval_s=0.05,
        event_root=tmp_path / "events",
        adapter_root=tmp_path / "adapters",
        state_root=state_root,
    )
    monitor.start()
    assert monitor.is_alive()
    monitor.stop()
    monitor.join(timeout=1.0)
    assert not monitor.is_alive()


def test_process_local_file_accumulates_across_calls(tmp_path):
    """Calling process_local_file again with more events accumulates and fires."""
    state_root = tmp_path / "repl"
    state_root.mkdir()
    events_file = _make_events(3, tmp_path)

    monitor = OperatorMonitor(
        machines={},
        poll_interval_s=0.05,
        event_root=tmp_path / "operator-events",
        adapter_root=tmp_path / "adapters",
        state_root=state_root,
    )
    monitor.process_local_file(events_file)

    events_local = state_root / "events-local.jsonl"
    assert events_local.exists()
    count_first = len(events_local.read_text().splitlines())
    assert count_first >= 1, "should write at least one alert after 3 events"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_operator_monitor.py -q
```

Expected: FAIL — `OperatorMonitor.__init__` doesn't accept `state_root`; `push_fn` still required.

- [ ] **Step 3: Update OperatorMonitor**

Replace `scripts/operator_monitor.py` with:

```python
from __future__ import annotations

import time
import threading
from collections import deque
from pathlib import Path
from typing import Any, Callable

from scripts.observer_plugin import AdapterRegistry
from scripts.pattern_detector import PatternDetector, PatternSummary


class OperatorMonitor(threading.Thread):
    """Background thread that watches local operator event files,
    runs PatternDetector against a per-machine sliding window buffer,
    and writes pattern alerts directly to events-local.jsonl."""

    def __init__(
        self,
        machines: dict[str, Any],
        push_fn: Callable[[str, dict, PatternSummary], None] | None = None,
        poll_interval_s: float = 5.0,
        event_root: Path | None = None,
        adapter_root: Path | None = None,
        state_root: Path | None = None,
    ) -> None:
        super().__init__(daemon=True, name="OperatorMonitor")
        # machines parameter kept for API compatibility; polling removed (runner pushes events via daemon)
        self._machines = machines
        self._push_fn = push_fn  # deprecated: kept for backward compat, prefer state_root path
        self._poll_interval_s = poll_interval_s
        self._event_root = event_root or (Path.home() / ".emerge" / "operator-events")
        self._state_root = state_root or (Path.home() / ".emerge" / "repl")
        self._adapter_registry = AdapterRegistry(adapter_root=adapter_root)
        self._detector = PatternDetector()
        self._last_poll_ms: dict[str, int] = {}
        # Sliding window buffer: accumulates events within FREQ_WINDOW_MS per machine.
        self._event_buffers: dict[str, deque] = {}
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        """Block until stop() is called. Operator events arrive via process_local_file()."""
        self._stop_event.wait()

    def process_local_file(self, events_path: Path) -> None:
        """Process a single local events.jsonl file. Called by EventRouter on file change."""
        import json as _json
        import time as _time
        if not events_path.exists() or events_path.name != "events.jsonl":
            return
        machine_id = events_path.parent.name
        key = f"local:{machine_id}"
        since_ms = self._last_poll_ms.get(key, 0)
        events: list[dict] = []
        with events_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                if e.get("ts_ms", 0) > since_ms:
                    events.append(e)
        if events:
            latest_ts = max(e.get("ts_ms", 0) for e in events)
            self._last_poll_ms[key] = latest_ts
            buf = self._event_buffers.setdefault(key, deque())
            buf.extend(events)
        buf = self._event_buffers.get(key)
        if not buf:
            return
        now_ms = int(_time.time() * 1000)
        window_ms = self._detector.FREQ_WINDOW_MS
        while buf and now_ms - buf[0].get("ts_ms", 0) > window_ms:
            buf.popleft()
        if not buf:
            return
        summaries = self._detector.ingest(list(buf))
        for summary in summaries:
            app = summary.context_hint.get("app", machine_id)
            plugin = self._adapter_registry.get_plugin(app)
            try:
                context = plugin.get_context(summary.context_hint)
            except Exception:
                context = summary.context_hint.copy()
            # Primary path: write directly to events-local.jsonl
            ts_ms = int(_time.time() * 1000)
            events_local = self._state_root / "events-local.jsonl"
            events_local.parent.mkdir(parents=True, exist_ok=True)
            alert = {
                "type": "local_pattern_alert",
                "ts_ms": ts_ms,
                "stage": summary.policy_stage,   # "explore" (PatternDetector default)
                "intent_signature": summary.intent_signature,
                "meta": {
                    "occurrences": summary.occurrences,
                    "window_minutes": round(summary.window_minutes, 1),
                    "machine_ids": summary.machine_ids,
                    "detector_signals": summary.detector_signals,
                    "app": summary.context_hint.get("app", ""),
                },
            }
            with events_local.open("a", encoding="utf-8") as f:
                f.write(_json.dumps(alert, ensure_ascii=False) + "\n")
            # Deprecated callback path (backward compat)
            if self._push_fn is not None:
                self._push_fn(summary.policy_stage, context, summary)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_operator_monitor.py -q
```

Expected: 4 passed.

- [ ] **Step 5: Run full suite to check for regressions**

```bash
python -m pytest tests -q --tb=short
```

Expected: all tests pass (no existing tests use `push_fn` without a fallback).

- [ ] **Step 6: Commit**

```bash
git add scripts/operator_monitor.py tests/test_operator_monitor.py
git commit -m "feat: OperatorMonitor writes events-local.jsonl directly, push_fn optional"
```

---

## Task 2: DaemonHTTPServer — per-runner PatternDetector buffer + broadcast

**Files:**
- Modify: `scripts/daemon_http.py`
- Modify: `tests/test_daemon_http.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_daemon_http.py`:

```python
def _make_server_with_daemon(tmp_path, daemon=None):
    """Start DaemonHTTPServer with a daemon that has _span_tracker stubbed."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.daemon_http import DaemonHTTPServer

    class _SpanTrackerStub:
        def get_policy_status(self, intent_signature: str) -> str:
            return "explore"

    class _DaemonStub:
        def handle_jsonrpc(self, req):
            return {"jsonrpc": "2.0", "id": req.get("id"), "result": {}}
        _span_tracker = _SpanTrackerStub()
        _cockpit_server = None

    d = daemon or _DaemonStub()
    srv = DaemonHTTPServer(daemon=d, port=0, pid_path=tmp_path / "d.pid",
                           state_root=tmp_path / "repl")
    srv.start()
    time.sleep(0.1)
    return srv


def test_runner_push_pattern_alert_written_to_events_jsonl(tmp_path):
    """Pushing >=3 matching events → pattern_alert in events-{profile}.jsonl."""
    import urllib.request as _req
    srv = _make_server_with_daemon(tmp_path)

    # Register runner
    body = json.dumps({"runner_profile": "p1", "machine_id": "m1"}).encode()
    r = urllib.request.Request(f"http://localhost:{srv.port}/runner/online",
                               data=body, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(r, timeout=5)

    # Push 3 identical-pattern events
    now_ms = int(time.time() * 1000)
    for i in range(3):
        event = {
            "runner_profile": "p1",
            "machine_id": "m1",
            "session_role": "operator",
            "event_type": "entity_added",
            "app": "zwcad",
            "payload": {"layer": "标注", "content": f"room_{i}"},
            "ts_ms": now_ms - i * 60_000,
        }
        body2 = json.dumps(event).encode()
        r2 = urllib.request.Request(f"http://localhost:{srv.port}/runner/event",
                                    data=body2, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(r2, timeout=5)

    events_file = tmp_path / "repl" / "events-p1.jsonl"
    assert events_file.exists(), "events-p1.jsonl must exist"
    alerts = [
        json.loads(l)
        for l in events_file.read_text().splitlines()
        if l.strip() and json.loads(l).get("type") == "pattern_alert"
    ]
    assert len(alerts) >= 1, "at least one pattern_alert expected"
    alert = alerts[0]
    assert alert["stage"] == "explore"
    assert "intent_signature" in alert
    assert alert["meta"]["occurrences"] >= 3
    srv.stop()


def test_runner_push_pattern_updates_last_alert(tmp_path):
    """After >=3 events, _connected_runners[profile]['last_alert'] is populated."""
    import urllib.request
    srv = _make_server_with_daemon(tmp_path)

    body = json.dumps({"runner_profile": "p2", "machine_id": "m2"}).encode()
    r = urllib.request.Request(f"http://localhost:{srv.port}/runner/online",
                               data=body, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(r, timeout=5)

    now_ms = int(time.time() * 1000)
    for i in range(3):
        event = {
            "runner_profile": "p2", "machine_id": "m2",
            "session_role": "operator", "event_type": "entity_added",
            "app": "zwcad", "payload": {"layer": "标注", "content": f"x_{i}"},
            "ts_ms": now_ms - i * 60_000,
        }
        body2 = json.dumps(event).encode()
        r2 = urllib.request.Request(f"http://localhost:{srv.port}/runner/event",
                                    data=body2, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(r2, timeout=5)

    with srv._runners_lock:
        last_alert = srv._connected_runners.get("p2", {}).get("last_alert")
    assert last_alert is not None, "last_alert must be set after pattern detection"
    assert last_alert["stage"] == "explore"
    srv.stop()


def test_team_active_true_when_runner_connected(tmp_path):
    """_write_monitor_state sets team_active=True when a runner is connected."""
    import json as _json
    srv = _make_server_with_daemon(tmp_path)

    body = json.dumps({"runner_profile": "p3", "machine_id": "m3"}).encode()
    r = urllib.request.Request(f"http://localhost:{srv.port}/runner/online",
                               data=body, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(r, timeout=5)

    state_path = tmp_path / "repl" / "runner-monitor-state.json"
    # Give the server a moment to write the state
    time.sleep(0.1)
    assert state_path.exists()
    state = _json.loads(state_path.read_text())
    assert state["team_active"] is True
    srv.stop()


def test_broadcast_called_on_pattern_detection(tmp_path):
    """cockpit.broadcast({"monitors_updated": True}) is called after pattern detected."""
    import urllib.request
    broadcasts = []

    class _MockCockpit:
        def broadcast(self, event: dict) -> None:
            broadcasts.append(event)

    class _SpanTrackerStub:
        def get_policy_status(self, _sig): return "explore"

    class _DaemonStub:
        def handle_jsonrpc(self, req):
            return {"jsonrpc": "2.0", "id": req.get("id"), "result": {}}
        _span_tracker = _SpanTrackerStub()
        _cockpit_server = _MockCockpit()

    srv = _make_server_with_daemon(tmp_path, daemon=_DaemonStub())

    body = json.dumps({"runner_profile": "p4", "machine_id": "m4"}).encode()
    r = urllib.request.Request(f"http://localhost:{srv.port}/runner/online",
                               data=body, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(r, timeout=5)

    now_ms = int(time.time() * 1000)
    for i in range(3):
        event = {
            "runner_profile": "p4", "machine_id": "m4",
            "session_role": "operator", "event_type": "entity_added",
            "app": "zwcad", "payload": {"layer": "标注", "content": f"y_{i}"},
            "ts_ms": now_ms - i * 60_000,
        }
        body2 = json.dumps(event).encode()
        r2 = urllib.request.Request(f"http://localhost:{srv.port}/runner/event",
                                    data=body2, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(r2, timeout=5)

    assert any(b.get("monitors_updated") for b in broadcasts), \
        "broadcast(monitors_updated=True) must be called after pattern detection"
    srv.stop()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_daemon_http.py -q
```

Expected: FAIL — `DaemonHTTPServer` has no `_detector`, `_runner_event_buffers`, or pattern detection logic.

- [ ] **Step 3: Add PatternDetector fields and detection logic to DaemonHTTPServer**

In `scripts/daemon_http.py`, update `DaemonHTTPServer.__init__` to add (after the `_popup_lock` line, ~line 56):

```python
        # Pattern detection: per-runner sliding-window event buffers
        from scripts.pattern_detector import PatternDetector as _PatternDetector
        from collections import deque as _deque
        self._detector = _PatternDetector()
        self._runner_event_buffers: dict[str, _deque] = {}
        self._runner_buffers_lock = threading.Lock()
```

- [ ] **Step 4: Update `_on_runner_event` to run detection**

In `scripts/daemon_http.py`, at the end of `_on_runner_event` (after the existing `if runner_profile:` block that writes to the per-runner jsonl and updates `last_event_ts_ms`), append:

```python
        # Pattern detection on runner push events
        if runner_profile:
            window_ms = self._detector.FREQ_WINDOW_MS
            with self._runner_buffers_lock:
                buf = self._runner_event_buffers.setdefault(runner_profile, __import__('collections').deque())
                buf.append({
                    **{k: v for k, v in payload.items() if k != "runner_profile"},
                    "ts_ms": ts_ms,
                    "machine_id": machine_id or runner_profile,
                })
                while buf and ts_ms - buf[0].get("ts_ms", 0) > window_ms:
                    buf.popleft()
                snapshot = list(buf)

            summaries = self._detector.ingest(snapshot)
            for summary in summaries:
                try:
                    stage = self._daemon._span_tracker.get_policy_status(
                        summary.intent_signature
                    )
                except Exception:
                    stage = summary.policy_stage  # fallback: "explore"

                alert = {
                    "type": "pattern_alert",
                    "ts_ms": ts_ms,
                    "runner_profile": runner_profile,
                    "stage": stage,
                    "intent_signature": summary.intent_signature,
                    "meta": {
                        "occurrences": summary.occurrences,
                        "window_minutes": round(summary.window_minutes, 1),
                        "machine_ids": summary.machine_ids,
                        "detector_signals": summary.detector_signals,
                    },
                }
                self._append_event(
                    self._state_root / f"events-{runner_profile}.jsonl", alert
                )
                with self._runners_lock:
                    if runner_profile in self._connected_runners:
                        self._connected_runners[runner_profile]["last_alert"] = {
                            "stage": stage,
                            "intent_signature": summary.intent_signature,
                            "ts_ms": ts_ms,
                        }

            if summaries:
                self._write_monitor_state()
                cockpit = getattr(self._daemon, "_cockpit_server", None)
                if cockpit is not None:
                    cockpit.broadcast({"monitors_updated": True})
```

- [ ] **Step 5: Fix `_write_monitor_state` `team_active` value**

In `scripts/daemon_http.py`, in `_write_monitor_state`, change:

```python
                 "team_active": False,
```

to:

```python
                 "team_active": len(self._connected_runners) > 0,
```

Note: the runners snapshot is built inside `with self._runners_lock`, and `len(runners) > 0` computed outside. Use `len(runners) > 0` after building the list (which is already done):

```python
        state = {"runners": runners, "team_active": len(runners) > 0,
                 "updated_ts_ms": int(time.time() * 1000)}
```

- [ ] **Step 6: Add broadcast on runner online/offline**

In `scripts/daemon_http.py`, at the end of `_on_runner_online` (after `self._write_monitor_state()`), add:

```python
        cockpit = getattr(self._daemon, "_cockpit_server", None)
        if cockpit is not None:
            cockpit.broadcast({"monitors_updated": True})
```

In `scripts/daemon_http.py`, in `_handle_runner_sse`'s `finally` block (after `srv._write_monitor_state()`), add:

```python
                    cockpit = getattr(srv._daemon, "_cockpit_server", None)
                    if cockpit is not None:
                        cockpit.broadcast({"monitors_updated": True})
```

- [ ] **Step 7: Run tests to verify they pass**

```bash
python -m pytest tests/test_daemon_http.py -q
```

Expected: all tests pass (including new 4 tests).

- [ ] **Step 8: Run full suite**

```bash
python -m pytest tests -q --tb=short
```

Expected: all tests pass.

- [ ] **Step 9: Commit**

```bash
git add scripts/daemon_http.py tests/test_daemon_http.py
git commit -m "feat: DaemonHTTPServer — PatternDetector per-runner buffer, pattern_alert to events stream, broadcast on detection"
```

---

## Task 3: CockpitHTTPServer class in repl_admin.py

**Files:**
- Modify: `scripts/repl_admin.py`
- Modify: `tests/test_repl_admin.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_repl_admin.py`:

```python
# ---------------------------------------------------------------------------
# CockpitHTTPServer tests
# ---------------------------------------------------------------------------

def test_cockpit_http_server_starts_and_returns_url(tmp_path: Path, monkeypatch):
    """CockpitHTTPServer.start() returns a http://localhost:<port> URL."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import time as _time
    from scripts.repl_admin import CockpitHTTPServer, _StandaloneDaemonStub

    monkeypatch.setenv("EMERGE_REPL_ROOT", str(tmp_path))
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path))

    cockpit = CockpitHTTPServer(daemon=_StandaloneDaemonStub(), port=0, repl_root=tmp_path)
    url = cockpit.start()
    _time.sleep(0.1)

    assert url.startswith("http://localhost:")
    assert (tmp_path / "cockpit.pid").exists()
    cockpit.stop()


def test_cockpit_get_monitor_data_reads_memory(tmp_path: Path):
    """get_monitor_data() reads _connected_runners from daemon._http_server directly."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import threading
    from scripts.repl_admin import CockpitHTTPServer

    class _MockHTTPServer:
        _runners_lock = threading.Lock()
        _connected_runners = {
            "profile-1": {
                "connected_at_ms": 1000,
                "last_event_ts_ms": 2000,
                "machine_id": "m1",
                "last_alert": None,
            }
        }

    class _MockDaemon:
        _http_server = _MockHTTPServer()

    cockpit = CockpitHTTPServer(daemon=_MockDaemon(), port=0, repl_root=tmp_path)
    data = cockpit.get_monitor_data()

    assert data["team_active"] is True
    assert len(data["runners"]) == 1
    r = data["runners"][0]
    assert r["runner_profile"] == "profile-1"
    assert r["machine_id"] == "m1"
    assert r["connected"] is True


def test_cockpit_get_monitor_data_standalone_fallback(tmp_path: Path):
    """get_monitor_data() falls back to runner-monitor-state.json when _http_server is None."""
    import sys, json
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.repl_admin import CockpitHTTPServer, _StandaloneDaemonStub

    state_file = tmp_path / "runner-monitor-state.json"
    state_file.write_text(json.dumps({
        "runners": [{"runner_profile": "p1", "connected": True}],
        "team_active": True,
    }), encoding="utf-8")

    cockpit = CockpitHTTPServer(daemon=_StandaloneDaemonStub(), port=0, repl_root=tmp_path)
    data = cockpit.get_monitor_data()

    assert data["team_active"] is True
    assert any(r["runner_profile"] == "p1" for r in data["runners"])


def test_cockpit_broadcast_pushes_to_sse_clients(tmp_path: Path):
    """broadcast() writes SSE data to all connected wfile-like objects."""
    import sys, json, io
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.repl_admin import CockpitHTTPServer, _StandaloneDaemonStub

    cockpit = CockpitHTTPServer(daemon=_StandaloneDaemonStub(), port=0, repl_root=tmp_path)

    buf = io.BytesIO()
    with cockpit._sse_lock:
        cockpit._sse_clients.append(buf)

    cockpit.broadcast({"monitors_updated": True})

    written = buf.getvalue().decode()
    assert "monitors_updated" in written
    assert written.startswith("data: ")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_repl_admin.py -k "cockpit_http_server or cockpit_get_monitor or cockpit_broadcast" -q
```

Expected: FAIL — `CockpitHTTPServer` and `_StandaloneDaemonStub` don't exist yet.

- [ ] **Step 3: Update `_cockpit_pid_path` to accept optional repl_root**

In `scripts/repl_admin.py`, change the function at line ~2084 from:

```python
def _cockpit_pid_path() -> "Path":
    return _resolve_repl_root() / "cockpit.pid"
```

to:

```python
def _cockpit_pid_path(repl_root: "Path | None" = None) -> "Path":
    return (repl_root or _resolve_repl_root()) / "cockpit.pid"
```

Update all call sites `_cockpit_pid_path()` in `cmd_serve()` and `cmd_serve_stop()` — they pass no argument, so they continue to work unchanged.

- [ ] **Step 4: Update `_cockpit_list_injected_html` and `_cockpit_inject_html` to accept optional store/lock**

In `scripts/repl_admin.py`, change `_cockpit_list_injected_html` (~line 1069) from:

```python
def _cockpit_list_injected_html(connector: str) -> list[str]:
    with _COCKPIT_INJECT_LOCK:
        return [s["html"] for s in _COCKPIT_INJECTED_HTML.get(connector, [])]
```

to:

```python
def _cockpit_list_injected_html(connector: str, store: "dict | None" = None) -> list[str]:
    d = store if store is not None else _COCKPIT_INJECTED_HTML
    # No lock needed for read — callers hold their own lock when mutating
    return [s["html"] for s in d.get(connector, [])]
```

In `scripts/repl_admin.py`, change `_cockpit_inject_html` (~line 1047) signature:

```python
def _cockpit_inject_html(connector: str, html: str, slot_id: "str | None" = None,
                         *, store: "dict | None" = None, lock: "threading.Lock | None" = None) -> None:
    d = store if store is not None else _COCKPIT_INJECTED_HTML
    lk = lock if lock is not None else _COCKPIT_INJECT_LOCK
    with lk:
        slots = d.setdefault(connector, [])
        if slot_id is not None:
            for i, s in enumerate(slots):
                if s.get("id") == slot_id:
                    slots[i] = {"id": slot_id, "html": html}
                    return
            slots.append({"id": slot_id, "html": html})
        else:
            slots.append({"id": None, "html": html})
        if len(slots) > _MAX_INJECTED_PER_CONNECTOR:
            d[connector] = slots[-_MAX_INJECTED_PER_CONNECTOR:]
```

Update `cmd_assets` (~line 1074) to accept `injected_html` parameter:

```python
def cmd_assets(injected_html: "dict | None" = None) -> dict:
```

And change the internal call to `_cockpit_list_injected_html(name)` to:

```python
        injected = _cockpit_list_injected_html(name, store=injected_html)
```

- [ ] **Step 5: Add `_StandaloneDaemonStub` class**

Add near the bottom of `scripts/repl_admin.py`, before `_cockpit_pid_path`:

```python
class _StandaloneDaemonStub:
    """Minimal daemon stub used by CockpitHTTPServer when running in standalone CLI mode
    (no EmergeDaemon instance available). get_monitor_data() falls back to file."""
    _http_server = None
```

- [ ] **Step 6: Add `CockpitHTTPServer` class**

Add after `_StandaloneDaemonStub`, before `_cockpit_pid_path`:

```python
class CockpitHTTPServer:
    """Cockpit HTTP server that can run inside the daemon process (in-process mode)
    or standalone (CLI mode via cmd_serve).

    In in-process mode, daemon._http_server is set and get_monitor_data() reads
    _connected_runners from memory (zero file I/O). In standalone mode, fallback
    reads runner-monitor-state.json.
    """

    def __init__(
        self,
        daemon: "object",
        port: int = 0,
        repl_root: "Path | None" = None,
        connector_root: "Path | None" = None,
    ) -> None:
        self._daemon = daemon
        self._port = port
        self._repl_root = repl_root or _resolve_repl_root()
        self._connector_root = connector_root or _resolve_connector_root()
        self._server: "socketserver.TCPServer | None" = None
        self._thread: "threading.Thread | None" = None
        # Instance-level SSE clients and injected HTML (moved from module globals)
        self._sse_clients: list = []
        self._sse_lock = threading.Lock()
        self._injected_html: dict = {}
        self._inject_lock = threading.Lock()
        self.url: "str | None" = None

    def start(self) -> str:
        """Start the cockpit HTTP server in a daemon thread. Returns the URL."""
        handler = _make_cockpit_handler(self)
        self._server = _ReuseAddrTCPServer(("127.0.0.1", self._port), handler)
        actual_port = self._server.server_address[1]
        self.url = f"http://localhost:{actual_port}"
        # Write pid file
        pid_path = _cockpit_pid_path(self._repl_root)
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(
            json.dumps({"pid": os.getpid(), "port": actual_port, "cwd": str(Path.cwd())}),
            encoding="utf-8",
        )
        import atexit as _atexit
        _atexit.register(self.stop)
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="CockpitHTTPServer"
        )
        self._thread.start()
        return self.url

    def stop(self) -> None:
        if self._server:
            try:
                self._server.shutdown()
            except Exception:
                pass
            self._server = None
        _cockpit_pid_path(self._repl_root).unlink(missing_ok=True)

    def broadcast(self, event: dict) -> None:
        """Push SSE event to all connected browser clients."""
        data = f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode()
        with self._sse_lock:
            dead = []
            for wfile in self._sse_clients:
                try:
                    wfile.write(data)
                    wfile.flush()
                except OSError:
                    dead.append(wfile)
            for wfile in dead:
                self._sse_clients.remove(wfile)

    def get_monitor_data(self) -> dict:
        """Return runner monitor data. Reads from daemon memory; falls back to file."""
        hsrv = getattr(self._daemon, "_http_server", None)
        if hsrv is not None:
            # In-process mode: read directly from DaemonHTTPServer memory
            with hsrv._runners_lock:
                items = list(hsrv._connected_runners.items())
            runners = [
                {
                    "runner_profile": profile,
                    "connected": True,
                    "connected_at_ms": info.get("connected_at_ms", 0),
                    "last_event_ts_ms": info.get("last_event_ts_ms", 0),
                    "machine_id": info.get("machine_id", ""),
                    "last_alert": info.get("last_alert"),
                }
                for profile, info in items
            ]
            return {"runners": runners, "team_active": len(runners) > 0}
        # Standalone mode: fallback to file
        state_path = self._repl_root / "runner-monitor-state.json"
        if not state_path.exists():
            return {"runners": [], "team_active": False}
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            return {
                "runners": data.get("runners", []),
                "team_active": bool(data.get("team_active", False)),
            }
        except (OSError, json.JSONDecodeError):
            return {"runners": [], "team_active": False}
```

- [ ] **Step 7: Add `_make_cockpit_handler(cockpit)` factory**

Add immediately before `_ReuseAddrTCPServer` (line ~1825):

```python
def _make_cockpit_handler(cockpit: "CockpitHTTPServer"):
    """Return a _CockpitHandler subclass that uses cockpit's instance state."""
    class _Handler(_CockpitHandler):
        _cockpit = cockpit
    return _Handler
```

- [ ] **Step 8: Add `_cockpit` class attribute to `_CockpitHandler`**

In `_CockpitHandler` class definition (~line 1829), add a class-level attribute after the `_shell_path` line:

```python
class _CockpitHandler(http.server.BaseHTTPRequestHandler):
    _shell_path: "Path" = Path(__file__).parent / "cockpit_shell.html"
    _cockpit: "CockpitHTTPServer | None" = None   # set by _make_cockpit_handler()
```

- [ ] **Step 9: Update `/api/sse/status` SSE client management in `_CockpitHandler`**

In `do_GET`, the SSE handler at line ~1939 uses module globals. Update:

```python
            with _sse_lock:
                _sse_clients.append(self.wfile)
```
→
```python
            if self._cockpit is not None:
                with self._cockpit._sse_lock:
                    self._cockpit._sse_clients.append(self.wfile)
            else:
                with _sse_lock:
                    _sse_clients.append(self.wfile)
```

And the `finally` cleanup:

```python
            finally:
                with _sse_lock:
                    if self.wfile in _sse_clients:
                        _sse_clients.remove(self.wfile)
```
→
```python
            finally:
                if self._cockpit is not None:
                    with self._cockpit._sse_lock:
                        if self.wfile in self._cockpit._sse_clients:
                            self._cockpit._sse_clients.remove(self.wfile)
                else:
                    with _sse_lock:
                        if self.wfile in _sse_clients:
                            _sse_clients.remove(self.wfile)
```

- [ ] **Step 10: Update `/api/submit` broadcast and `/api/control-plane/monitors` in `_CockpitHandler`**

In `do_POST`, the `/api/submit` success handler (~line 1964) calls `_sse_broadcast(...)`. Update:

```python
            if result.get("ok"):
                _sse_broadcast({"pending": True, "action_count": result.get("action_count", 0),
                                "ts_ms": int(time.time() * 1000)})
```
→
```python
            if result.get("ok"):
                _ev = {"pending": True, "action_count": result.get("action_count", 0),
                       "ts_ms": int(time.time() * 1000)}
                if self._cockpit is not None:
                    self._cockpit.broadcast(_ev)
                else:
                    _sse_broadcast(_ev)
```

In `do_GET`, the `/api/control-plane/monitors` handler (~line 1906) calls `cmd_control_plane_monitors()`. Update:

```python
        elif path == "/api/control-plane/monitors":
            self._json(cmd_control_plane_monitors())
```
→
```python
        elif path == "/api/control-plane/monitors":
            if self._cockpit is not None:
                self._json(self._cockpit.get_monitor_data())
            else:
                self._json(cmd_control_plane_monitors())
```

- [ ] **Step 11: Update `/api/inject-component` and `/api/assets` in `_CockpitHandler`**

In `do_POST`, the `/api/inject-component` handler (~line 1973). Update the inner `if connector and html:` block:

```python
            if connector and html:
                if replace:
                    with _COCKPIT_INJECT_LOCK:
                        _COCKPIT_INJECTED_HTML[connector] = [{"id": slot_id, "html": html}]
                else:
                    _cockpit_inject_html(connector, html, slot_id)
```
→
```python
            if connector and html:
                if self._cockpit is not None:
                    store = self._cockpit._injected_html
                    lock = self._cockpit._inject_lock
                else:
                    store, lock = None, None
                if replace:
                    lk = lock if lock is not None else _COCKPIT_INJECT_LOCK
                    d = store if store is not None else _COCKPIT_INJECTED_HTML
                    with lk:
                        d[connector] = [{"id": slot_id, "html": html}]
                else:
                    _cockpit_inject_html(connector, html, slot_id,
                                         store=store, lock=lock)
```

In `do_GET`, the `/api/assets` handler:

```python
        elif path == "/api/assets":
            self._json(cmd_assets())
```
→
```python
        elif path == "/api/assets":
            ih = self._cockpit._injected_html if self._cockpit is not None else None
            self._json(cmd_assets(injected_html=ih))
```

- [ ] **Step 12: Update `cmd_serve()` to use CockpitHTTPServer**

In `scripts/repl_admin.py`, replace `cmd_serve()` (~line 2088) with:

```python
def cmd_serve(port: int = 0, open_browser: bool = False) -> dict:
    """Start the cockpit HTTP server. Idempotent — returns existing instance if already
    running FOR THE SAME PROJECT. If a server is running for a different project (cwd
    mismatch), it is stopped and a new one is started.
    """
    import signal as _signal
    repl_root = _resolve_repl_root()
    pid_path = _cockpit_pid_path(repl_root)
    current_cwd = str(Path.cwd())

    # Reuse existing instance if alive AND same project
    if pid_path.exists():
        try:
            info = json.loads(pid_path.read_text(encoding="utf-8"))
            existing_pid = int(info["pid"])
            existing_port = int(info["port"])
            existing_cwd = info.get("cwd", "")
            os.kill(existing_pid, 0)  # raises OSError if process is gone
            if existing_cwd == current_cwd:
                url = f"http://localhost:{existing_port}"
                if open_browser:
                    webbrowser.open(url)
                return {"ok": True, "port": existing_port, "url": url, "reused": True}
            # Different project — stop the old server and start fresh
            try:
                os.kill(existing_pid, _signal.SIGTERM)
            except OSError:
                pass
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            pass
        pid_path.unlink(missing_ok=True)

    cockpit = CockpitHTTPServer(daemon=_StandaloneDaemonStub(), port=port, repl_root=repl_root)
    url = cockpit.start()
    actual_port = int(url.split(":")[-1])

    import atexit as _atexit
    _atexit.register(lambda: cockpit.broadcast({"status": "offline"}))

    if open_browser:
        webbrowser.open(url)
    return {"ok": True, "port": actual_port, "url": url, "reused": False}
```

- [ ] **Step 13: Run tests to verify they pass**

```bash
python -m pytest tests/test_repl_admin.py -q
```

Expected: all tests pass (including 4 new cockpit tests).

- [ ] **Step 14: Run full suite**

```bash
python -m pytest tests -q --tb=short
```

Expected: all tests pass.

- [ ] **Step 15: Commit**

```bash
git add scripts/repl_admin.py tests/test_repl_admin.py
git commit -m "feat: CockpitHTTPServer class — in-process mode, broadcast(), get_monitor_data() from memory"
```

---

## Task 4: EmergeDaemon — delete dead code, pass state_root to OperatorMonitor, in-process cockpit in run_http

**Files:**
- Modify: `scripts/emerge_daemon.py`

- [ ] **Step 1: Delete `_push_pattern` and `_build_explore_message`**

In `scripts/emerge_daemon.py`, delete the entire `_push_pattern` method (lines ~2988–3025) and `_build_explore_message` method (lines ~3027–3036). These are replaced by the direct-write paths in `OperatorMonitor` and `DaemonHTTPServer`.

- [ ] **Step 2: Update `start_operator_monitor` to pass `state_root` and remove `push_fn`**

In `scripts/emerge_daemon.py`, in `start_operator_monitor` (~line 2889), replace:

```python
        self._operator_monitor = OperatorMonitor(
            machines={},
            push_fn=self._push_pattern,
            poll_interval_s=poll_s,
            event_root=Path.home() / ".emerge" / "operator-events",
            adapter_root=Path.home() / ".emerge" / "adapters",
        )
```

with:

```python
        self._operator_monitor = OperatorMonitor(
            machines={},
            poll_interval_s=poll_s,
            event_root=Path.home() / ".emerge" / "operator-events",
            adapter_root=Path.home() / ".emerge" / "adapters",
            state_root=self._state_root,
        )
```

- [ ] **Step 3: Delete `_ensure_cockpit`**

In `scripts/emerge_daemon.py`, delete the entire `_ensure_cockpit` function (~lines 3109–3148).

- [ ] **Step 4: Update `run_http()` to start cockpit in-process**

In `scripts/emerge_daemon.py`, replace the body of `run_http` (~lines 3078–3107) with:

```python
def run_http(port: int = 8789) -> None:
    """Start emerge daemon in HTTP MCP server mode with in-process cockpit."""
    import atexit
    import threading as _threading
    from scripts.daemon_http import DaemonHTTPServer
    from scripts.repl_admin import CockpitHTTPServer

    daemon = EmergeDaemon()
    daemon._http_mode = True  # disable _elicit() blocking
    daemon.start_operator_monitor()
    daemon.start_event_router()
    atexit.register(daemon.stop_operator_monitor)
    atexit.register(daemon.stop_event_router)

    pid_path = Path.home() / ".emerge" / "daemon.pid"
    srv = DaemonHTTPServer(daemon=daemon, port=port, pid_path=pid_path)
    daemon._http_server = srv
    srv.start()
    print(f"Emerge daemon HTTP server running on port {srv.port}", flush=True)

    # Start cockpit in-process — no subprocess, shares daemon memory
    try:
        cockpit = CockpitHTTPServer(daemon=daemon, port=0)
        url = cockpit.start()
        daemon._cockpit_server = cockpit
        print(f"[emerge] Cockpit: {url}", flush=True)
        atexit.register(cockpit.stop)
    except Exception as _e:
        print(f"[emerge] Cockpit failed to start: {_e}", flush=True)

    try:
        _threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        srv.stop()
```

- [ ] **Step 5: Run the full test suite**

```bash
python -m pytest tests -q --tb=short
```

Expected: all tests pass. The deleted methods are not referenced by any test.

- [ ] **Step 6: Verify no remaining references to deleted symbols**

```bash
grep -rn "_push_pattern\|_build_explore_message\|_ensure_cockpit" scripts/ tests/
```

Expected: no matches (other than this grep command itself).

- [ ] **Step 7: Commit**

```bash
git add scripts/emerge_daemon.py
git commit -m "feat: EmergeDaemon — delete _push_pattern/_build_explore_message/_ensure_cockpit, in-process cockpit in run_http"
```

---

## Task 5: Docs — update CLAUDE.md and README.md

**Files:**
- Modify: `CLAUDE.md`
- Modify: `README.md`

- [ ] **Step 1: Update CLAUDE.md Key Invariants**

In `CLAUDE.md`, find the bullet beginning with `**Runner push architecture**:` and update it to:

```
**Runner push architecture**: Runners connect to the daemon via `GET /runner/sse?runner_profile=<p>`, register via `POST /runner/online`, and push events via `POST /runner/event`. `DaemonHTTPServer._on_runner_event` maintains a per-runner sliding-window `deque` and runs `PatternDetector.ingest()` on each push; pattern alerts are written directly to `events-{profile}.jsonl` (type=`pattern_alert`). `cockpit.broadcast({"monitors_updated": True})` is called directly on pattern detection and runner connect/disconnect — no file IPC.
```

Find the bullet beginning with `**Cockpit control plane**:` and append to it:

```
`CockpitHTTPServer` (in `repl_admin.py`) is the cockpit HTTP server class — used both in-process (started by `run_http()`, `_cockpit_server` attribute on EmergeDaemon) and standalone (CLI `python repl_admin.py serve`). In-process mode: `get_monitor_data()` reads `_connected_runners` from `DaemonHTTPServer` memory; `broadcast()` pushes SSE directly. Standalone mode: `get_monitor_data()` falls back to `runner-monitor-state.json`. `_StandaloneDaemonStub` is the sentinel daemon for CLI mode.
```

Find the bullet beginning with `**OperatorMonitor** auto-starts` and update the `push_fn` reference:

Replace:
```
`OperatorMonitor` auto-starts when a runner is configured ... `push_fn` callback writes ...
```

with (keep existing text but remove/update push_fn references):
```
`OperatorMonitor` auto-starts when a runner is configured (`_get_runner_router() is not None`) OR `EMERGE_OPERATOR_MONITOR=1`. `push_fn` parameter is deprecated (optional, default None). `process_local_file` writes `local_pattern_alert` events directly to `events-local.jsonl` in `state_root`. `state_root` is injected by `start_operator_monitor` from `self._state_root`.
```

Find the bullet with `**Per-runner alert routing**:` and update it:

```
**Per-runner alert routing**: `DaemonHTTPServer._on_runner_event` writes `pattern_alert` to `events-{runner_profile}.jsonl` when `PatternDetector` fires. `OperatorMonitor.process_local_file` writes `local_pattern_alert` to `events-local.jsonl`. The old `_push_pattern` / `pattern-alerts-{profile}.json` file format is removed. `watch_emerge.py --runner-profile <name>` watches `events-{name}.jsonl`; agents-team watcher monitors its own file.
```

- [ ] **Step 2: Update README.md component table**

In `README.md`, find the component table row for `repl_admin.py` and update the description to include `CockpitHTTPServer`.

Find the architecture diagram or data-flow description mentioning cockpit startup and update to reflect in-process startup via `CockpitHTTPServer` instead of subprocess.

- [ ] **Step 3: Run full suite one more time**

```bash
python -m pytest tests -q --tb=short
```

Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md README.md
git commit -m "docs: update architecture for CockpitHTTPServer, in-process cockpit, per-runner pattern_alert routing"
```

---

## Self-Review

**Spec coverage check:**

1. ✅ PatternDetector接入runner push — Task 2 (`_on_runner_event` buffer + ingest)
2. ✅ `pattern_alert` to `events-{profile}.jsonl` — Task 2 (`_append_event`)
3. ✅ `last_alert` updated — Task 2
4. ✅ `broadcast()` on pattern/online/offline — Task 2
5. ✅ `team_active` fix — Task 2 (`len(runners) > 0`)
6. ✅ OperatorMonitor `push_fn` optional, `state_root` added — Task 1
7. ✅ `process_local_file` writes `events-local.jsonl` — Task 1
8. ✅ `CockpitHTTPServer` class — Task 3
9. ✅ `_make_cockpit_handler` factory — Task 3
10. ✅ `get_monitor_data()` from memory — Task 3
11. ✅ `_StandaloneDaemonStub` fallback — Task 3
12. ✅ `cmd_serve()` uses CockpitHTTPServer — Task 3
13. ✅ `/api/control-plane/monitors` uses `get_monitor_data()` — Task 3
14. ✅ `/api/sse/status` SSE clients use instance vars — Task 3
15. ✅ Delete `_push_pattern`, `_build_explore_message`, `_ensure_cockpit` — Task 4
16. ✅ `run_http()` in-process cockpit — Task 4
17. ✅ `start_operator_monitor` passes `state_root` — Task 4
18. ✅ Docs — Task 5

**Placeholder scan:** No TBDs or incomplete sections found.

**Type consistency check:**
- `OperatorMonitor(state_root=...)` — defined Task 1, used Task 4 ✅
- `CockpitHTTPServer(daemon, port, repl_root)` — defined Task 3, used Task 4 ✅
- `cockpit.broadcast(dict)` — defined Task 3, called Task 2 and Task 4 ✅
- `cockpit.get_monitor_data()` — defined Task 3, called Task 3 handler ✅
- `DaemonHTTPServer._detector`, `._runner_event_buffers` — defined and used Task 2 ✅
- `_cockpit_pid_path(repl_root=None)` — updated Task 3, called with no args in `cmd_serve_stop()` ✅
