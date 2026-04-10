# Event-Driven Architecture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace all file-polling loops and blocking waits in emerge with OS-native file events (watchdog), MCP ElicitRequest, and SSE — eliminating 4 daemon threads and making CC non-blocking.

**Architecture:** A new `EventRouter` wraps watchdog to dispatch file change callbacks; the daemon's stdio loop is refactored to dispatch tool calls to a `ThreadPoolExecutor` so ElicitRequest can block a worker thread while the main loop continues; cockpit gets an SSE endpoint replacing the cc-listening.json heartbeat file.

**Tech Stack:** Python `watchdog` ≥3.0, `threading.ThreadPoolExecutor`, MCP protocol 2025-03-26, SSE (text/event-stream), Python stdlib only for SSE.

**Spec:** `docs/superpowers/specs/2026-04-10-event-driven-architecture-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `scripts/event_router.py` | **Create** | watchdog wrapper; dispatches file events to callbacks |
| `scripts/emerge_daemon.py` | **Modify** | ThreadPool stdio, `_elicit()`, EventRouter integration, remove PendingActionMonitor |
| `scripts/emerge_sync.py` | **Modify** | Replace `run_poll_loop` sleep(10) with EventRouter + threading.Timer |
| `scripts/operator_monitor.py` | **Modify** | Remove `_poll_local`; EventRouter handles local events |
| `scripts/repl_admin.py` | **Modify** | Add SSE endpoint; remove `cmd_wait_for_submit`, `_write_cc_listening`, `_cc_listening_path` |
| `tests/test_event_router.py` | **Create** | EventRouter unit tests |
| `tests/test_cockpit_sse.py` | **Create** | SSE endpoint tests |
| `tests/test_mcp_tools_integration.py` | **Modify** | ElicitRequest tests for span_approve, reconcile, hub |
| `tests/test_operator_monitor.py` | **Modify** | Remove tests for _poll_local |
| `tests/test_emerge_sync.py` | **Modify** | Update poll loop tests |

---

## Task 1: EventRouter — watchdog wrapper

**Files:**
- Create: `scripts/event_router.py`
- Create: `tests/test_event_router.py`

- [ ] **Step 1.1: Write failing tests**

```python
# tests/test_event_router.py
from __future__ import annotations
import threading
import time
from pathlib import Path
import pytest
from unittest.mock import patch, MagicMock


def test_event_router_dispatch_calls_handler(tmp_path):
    """_dispatch() must call the matching handler with the path."""
    from scripts.event_router import EventRouter
    called = []
    watch = tmp_path / "queue.jsonl"
    router = EventRouter({watch: lambda p: called.append(p)})
    router._dispatch(watch)
    assert called == [watch]


def test_event_router_dispatch_ignores_unregistered(tmp_path):
    """_dispatch() must silently ignore paths with no handler."""
    from scripts.event_router import EventRouter
    called = []
    router = EventRouter({tmp_path / "a.jsonl": lambda p: called.append(p)})
    router._dispatch(tmp_path / "b.jsonl")
    assert called == []


def test_event_router_handler_exception_does_not_propagate(tmp_path):
    """Handler exceptions must be swallowed so other handlers still fire."""
    from scripts.event_router import EventRouter
    watch = tmp_path / "queue.jsonl"
    def bad(_): raise RuntimeError("boom")
    router = EventRouter({watch: bad})
    router._dispatch(watch)  # must not raise


def test_event_router_fallback_mode_when_watchdog_missing(tmp_path):
    """mode must be 'polling' when watchdog is not importable."""
    import sys
    with patch.dict(sys.modules, {"watchdog": None,
                                   "watchdog.observers": None,
                                   "watchdog.events": None}):
        from importlib import reload
        import scripts.event_router as er_mod
        reload(er_mod)
        router = er_mod.EventRouter({})
        router.start()
        assert router.mode == "polling"
        router.stop()
    reload(er_mod)  # restore for other tests


def test_event_router_polling_fires_on_file_change(tmp_path):
    """Polling fallback must fire callback when a watched file changes."""
    import sys
    with patch.dict(sys.modules, {"watchdog": None,
                                   "watchdog.observers": None,
                                   "watchdog.events": None}):
        from importlib import reload
        import scripts.event_router as er_mod
        reload(er_mod)
        watch = tmp_path / "queue.jsonl"
        watch.write_text("initial")
        fired = threading.Event()
        router = er_mod.EventRouter({watch: lambda _: fired.set()})
        router.start()
        time.sleep(0.1)
        watch.write_text("updated")
        assert fired.wait(timeout=3.0), "polling fallback never fired"
        router.stop()
    reload(er_mod)


def test_event_router_drains_existing_file_on_start(tmp_path):
    """start() must call handler once for any watched file that already exists."""
    from scripts.event_router import EventRouter
    watch = tmp_path / "queue.jsonl"
    watch.write_text("existing data")
    called = []
    router = EventRouter({watch: lambda p: called.append(p)})
    router.start()
    router.stop()
    assert len(called) >= 1
```

- [ ] **Step 1.2: Run tests — verify they all FAIL**

```bash
python -m pytest tests/test_event_router.py -q 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'scripts.event_router'`

- [ ] **Step 1.3: Implement `scripts/event_router.py`**

```python
# scripts/event_router.py
from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Callable, Literal

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler, FileSystemEvent
    _WATCHDOG_AVAILABLE = True
except ImportError:
    _WATCHDOG_AVAILABLE = False


class EventRouter:
    """Dispatches file system events to registered callbacks.

    Uses watchdog (inotify/FSEvents) when available; falls back to a 1s
    mtime-polling thread when watchdog is not installed.

    Usage::

        router = EventRouter({
            Path("~/.emerge/sync-queue.jsonl").expanduser(): on_sync_queue,
            Path("~/.emerge/exec/pending-actions.json").expanduser(): on_pending,
        })
        router.start()   # drains existing files, then watches
        ...
        router.stop()
    """

    def __init__(self, handlers: dict[Path, Callable[[Path], None]]) -> None:
        self._handlers = handlers
        self._mode: Literal["inotify", "polling"] = "polling"
        self._observer: "Observer | None" = None
        self._poll_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    @property
    def mode(self) -> Literal["inotify", "polling"]:
        return self._mode

    def _dispatch(self, path: Path) -> None:
        """Call handler registered for *path* (exact match or child). Swallows exceptions."""
        for watch_path, callback in self._handlers.items():
            if path == watch_path or (watch_path.is_dir() and path.is_relative_to(watch_path)):
                try:
                    callback(path)
                except Exception:
                    pass

    def start(self) -> None:
        """Drain existing watched files, then begin watching."""
        for path in self._handlers:
            if path.exists():
                self._dispatch(path)

        if _WATCHDOG_AVAILABLE:
            self._start_watchdog()
            self._mode = "inotify"
        else:
            print(
                "[EventRouter] watchdog not installed — using polling fallback. "
                "Install with: pip install watchdog",
                file=sys.stderr,
            )
            self._start_polling()
            self._mode = "polling"

    def stop(self) -> None:
        self._stop_event.set()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)

    def _start_watchdog(self) -> None:
        handler = _RouterHandler(self._handlers)
        observer = Observer()
        watched_dirs: set[str] = set()
        for path in self._handlers:
            watch_dir = str(path) if path.is_dir() else str(path.parent)
            if watch_dir not in watched_dirs:
                observer.schedule(handler, watch_dir, recursive=True)
                watched_dirs.add(watch_dir)
        observer.start()
        self._observer = observer

    def _start_polling(self) -> None:
        mtimes: dict[Path, float] = {}

        def _poll() -> None:
            while not self._stop_event.wait(1.0):
                for path in list(self._handlers):
                    if not path.exists():
                        continue
                    try:
                        mtime = path.stat().st_mtime
                    except OSError:
                        continue
                    if mtime != mtimes.get(path):
                        mtimes[path] = mtime
                        self._dispatch(path)

        self._poll_thread = threading.Thread(target=_poll, daemon=True, name="EventRouter-poll")
        self._poll_thread.start()


if _WATCHDOG_AVAILABLE:
    class _RouterHandler(FileSystemEventHandler):
        def __init__(self, handlers: dict[Path, Callable[[Path], None]]) -> None:
            self._handlers = handlers

        def _try_dispatch(self, src: str) -> None:
            p = Path(src)
            for watch_path, callback in self._handlers.items():
                if p == watch_path or (watch_path.is_dir() and p.is_relative_to(watch_path)):
                    try:
                        callback(p)
                    except Exception:
                        pass

        def on_modified(self, event: "FileSystemEvent") -> None:
            if not event.is_directory:
                self._try_dispatch(event.src_path)

        def on_created(self, event: "FileSystemEvent") -> None:
            if not event.is_directory:
                self._try_dispatch(event.src_path)
```

- [ ] **Step 1.4: Run tests — verify they pass**

```bash
python -m pytest tests/test_event_router.py -q
```

Expected: `6 passed`

- [ ] **Step 1.5: Commit**

```bash
git add scripts/event_router.py tests/test_event_router.py
git commit -m "feat: add EventRouter — watchdog wrapper with polling fallback"
```

---

## Task 2: emerge_sync — replace sleep(10) with EventRouter + Timer

**Files:**
- Modify: `scripts/emerge_sync.py:592-613` (replace `run_poll_loop`)
- Modify: `tests/test_emerge_sync.py`

- [ ] **Step 2.1: Read existing emerge_sync test to understand fixture pattern**

```bash
python -m pytest tests/test_emerge_sync.py -q 2>&1 | tail -5
```

- [ ] **Step 2.2: Write failing test for new event-driven loop**

Add to `tests/test_emerge_sync.py`:

```python
def test_run_event_loop_fires_stable_events_on_queue_write(tmp_path, monkeypatch):
    """Writing to sync-queue.jsonl must trigger _run_stable_events immediately."""
    from scripts import emerge_sync
    import threading

    fired = threading.Event()
    monkeypatch.setattr(emerge_sync, "_run_stable_events", lambda: fired.set())
    monkeypatch.setattr(emerge_sync, "_run_pull_cycle", lambda: None)

    queue = tmp_path / "sync-queue.jsonl"
    monkeypatch.setattr(emerge_sync, "sync_queue_path", lambda: queue)
    monkeypatch.setattr(emerge_sync, "load_hub_config",
                        lambda: {"poll_interval_seconds": 999})

    stop = threading.Event()
    t = threading.Thread(target=emerge_sync.run_event_loop, args=(stop,), daemon=True)
    t.start()

    queue.write_text('{"type":"stable"}\n')
    assert fired.wait(timeout=3.0), "stable event handler never fired"
    stop.set()
    t.join(timeout=2)


def test_run_event_loop_pull_cycle_fires_on_timer(tmp_path, monkeypatch):
    """_run_pull_cycle must fire after poll_interval_seconds."""
    from scripts import emerge_sync
    import threading

    pulled = threading.Event()
    monkeypatch.setattr(emerge_sync, "_run_stable_events", lambda: None)
    monkeypatch.setattr(emerge_sync, "_run_pull_cycle", lambda: pulled.set())
    monkeypatch.setattr(emerge_sync, "sync_queue_path", lambda: tmp_path / "q.jsonl")
    monkeypatch.setattr(emerge_sync, "load_hub_config",
                        lambda: {"poll_interval_seconds": 1})  # 1s for test speed

    stop = threading.Event()
    t = threading.Thread(target=emerge_sync.run_event_loop, args=(stop,), daemon=True)
    t.start()

    assert pulled.wait(timeout=4.0), "pull cycle never fired"
    stop.set()
    t.join(timeout=2)
```

- [ ] **Step 2.3: Run new tests — verify FAIL**

```bash
python -m pytest tests/test_emerge_sync.py::test_run_event_loop_fires_stable_events_on_queue_write tests/test_emerge_sync.py::test_run_event_loop_pull_cycle_fires_on_timer -q
```

Expected: `AttributeError: module 'scripts.emerge_sync' has no attribute 'run_event_loop'`

- [ ] **Step 2.4: Add `run_event_loop` to `scripts/emerge_sync.py`**

Replace the body of `run_poll_loop` (lines 592–613) and add `run_event_loop` below it. Keep `run_poll_loop` as an alias for backward compat during transition:

```python
def run_event_loop(stop_event: threading.Event | None = None) -> None:
    """Event-driven sync agent. Watches sync-queue.jsonl via EventRouter.

    Replaces run_poll_loop. poll_interval is re-read each Timer cycle so
    config changes take effect without restart.
    """
    from scripts.event_router import EventRouter

    cfg = load_hub_config()
    poll_interval = int(cfg.get("poll_interval_seconds", 300))
    logger.info("emerge_sync: event loop started (pull_interval=%ds)", poll_interval)

    _timer: list[threading.Timer] = []  # mutable cell for recursive timer

    def _schedule_pull() -> None:
        cfg2 = load_hub_config()
        interval = int(cfg2.get("poll_interval_seconds", 300))
        try:
            _run_pull_cycle()
        except Exception as exc:
            logger.error("emerge_sync pull cycle error: %s", exc)
        if stop_event is None or not stop_event.is_set():
            t = threading.Timer(interval, _schedule_pull)
            t.daemon = True
            _timer.clear()
            _timer.append(t)
            t.start()

    def _on_queue_change(_path: "Path") -> None:
        try:
            _run_stable_events()
        except Exception as exc:
            logger.error("emerge_sync stable events error: %s", exc)

    router = EventRouter({sync_queue_path(): _on_queue_change})
    router.start()

    # First pull immediately, then schedule recurring
    _schedule_pull()

    # Block until stop_event set
    try:
        while True:
            if stop_event and stop_event.is_set():
                break
            threading.Event().wait(timeout=1.0)
    finally:
        router.stop()
        for t in _timer:
            t.cancel()


def run_poll_loop(stop_event: threading.Event | None = None) -> None:
    """Deprecated: use run_event_loop. Kept for CLI backward compat."""
    run_event_loop(stop_event)
```

Also update the CLI entry at the bottom of the file where `run_poll_loop()` is called:

```python
    elif args[0] == "run":
        run_event_loop()
```

- [ ] **Step 2.5: Run tests — verify they pass**

```bash
python -m pytest tests/test_emerge_sync.py -q
```

Expected: all pass (including pre-existing tests)

- [ ] **Step 2.6: Commit**

```bash
git add scripts/emerge_sync.py tests/test_emerge_sync.py
git commit -m "feat: emerge_sync — replace sleep(10) poll loop with EventRouter + Timer"
```

---

## Task 3: Daemon stdio — ThreadPoolExecutor upgrade

**Files:**
- Modify: `scripts/emerge_daemon.py` — `run_stdio()` function (lines 2805–2840)
- Modify: `tests/test_mcp_tools_integration.py`

- [ ] **Step 3.1: Write failing test for concurrent tool calls**

Add to `tests/test_mcp_tools_integration.py`:

```python
def test_concurrent_tool_calls_each_get_correct_response():
    """Multiple simultaneous tool calls must each return their own result."""
    import concurrent.futures, json
    from scripts.emerge_daemon import EmergeDaemon
    daemon = EmergeDaemon()

    def call(i):
        return daemon.handle_jsonrpc({
            "jsonrpc": "2.0", "id": f"req-{i}", "method": "tools/call",
            "params": {"name": "icc_goal_read", "arguments": {}}
        })

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(call, i) for i in range(5)]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]

    ids = {r["id"] for r in results if r}
    assert len(ids) == 5  # each request gets its own response id
```

- [ ] **Step 3.2: Run test — verify FAIL or note current behavior**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_concurrent_tool_calls_each_get_correct_response -q
```

- [ ] **Step 3.3: Refactor `run_stdio()` to use ThreadPoolExecutor**

Replace the existing `run_stdio()` function in `scripts/emerge_daemon.py` (lines 2805–2840):

```python
def run_stdio() -> None:
    import atexit
    from concurrent.futures import ThreadPoolExecutor

    daemon = EmergeDaemon()
    executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="emerge-worker")

    daemon.start_operator_monitor()
    daemon.start_pending_monitor()
    atexit.register(daemon.stop_operator_monitor)
    atexit.register(daemon.stop_pending_monitor)
    atexit.register(lambda: executor.shutdown(wait=False))

    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue
        try:
            req = json.loads(text)
        except json.JSONDecodeError as exc:  # pragma: no cover
            _write_response({"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700, "message": f"Parse error: {exc}"}})
            continue

        req_id = req.get("id")
        method = req.get("method", "")

        # Elicitation responses: wake waiting worker thread, never dispatch as a request
        if req_id and req_id in daemon._elicit_events:
            daemon._elicit_results[req_id] = req.get("result") or {}
            daemon._elicit_events.pop(req_id).set()
            continue

        # Tool calls run in thread pool so _elicit() can block a worker
        # while the main loop continues routing
        if method == "tools/call":
            def _run(_req=req, _id=req_id):
                try:
                    resp = daemon.handle_jsonrpc(_req)
                except Exception as exc:  # pragma: no cover
                    resp = {"jsonrpc": "2.0", "id": _id,
                            "error": {"code": -32603, "message": str(exc)}}
                if resp is not None:
                    _write_response(resp)
            executor.submit(_run)
            continue

        # All other methods (initialize, ping, tools/list, resources/*) are synchronous
        try:
            resp = daemon.handle_jsonrpc(req)
        except Exception as exc:  # pragma: no cover
            resp = {"jsonrpc": "2.0", "id": req_id,
                    "error": {"code": -32603, "message": str(exc)}}
        if resp is not None:
            _write_response(resp)


def _write_response(payload: dict) -> None:
    with _stdout_lock:
        sys.stdout.write(json.dumps(payload) + "\n")
        sys.stdout.flush()
```

Also add these instance variables to `EmergeDaemon.__init__` (after line 135):

```python
        self._elicit_events: dict[str, threading.Event] = {}
        self._elicit_results: dict[str, dict] = {}
```

- [ ] **Step 3.4: Run full test suite — verify no regressions**

```bash
python -m pytest tests/ -q --tb=short 2>&1 | tail -20
```

Expected: same pass count as before (all existing tests still pass)

- [ ] **Step 3.5: Run new concurrency test**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_concurrent_tool_calls_each_get_correct_response -q
```

Expected: PASS

- [ ] **Step 3.6: Commit**

```bash
git add scripts/emerge_daemon.py tests/test_mcp_tools_integration.py
git commit -m "feat: daemon stdio — ThreadPoolExecutor for tool calls, elicit correlation map"
```

---

## Task 4: Protocol upgrade + `_elicit()` helper

**Files:**
- Modify: `scripts/emerge_daemon.py` — `handle_jsonrpc` initialize branch (line 1250), add `_elicit()`

- [ ] **Step 4.1: Write failing tests**

Add to `tests/test_mcp_tools_integration.py`:

```python
def test_initialize_declares_elicitation_capability():
    """initialize response must advertise elicitation capability and protocol 2025-03-26."""
    from scripts.emerge_daemon import EmergeDaemon
    daemon = EmergeDaemon()
    resp = daemon.handle_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    result = resp["result"]
    assert result["protocolVersion"] == "2025-03-26"
    assert "elicitation" in result["capabilities"]


def test_elicit_returns_result_when_response_arrives():
    """_elicit() must return the response payload set via correlation map."""
    import threading
    from scripts.emerge_daemon import EmergeDaemon
    daemon = EmergeDaemon()
    captured_push = []
    daemon._write_mcp_push = lambda p: captured_push.append(p)

    result_holder = []

    def _run():
        result_holder.append(daemon._elicit("Confirm?", {"type": "object",
            "properties": {"confirmed": {"type": "boolean"}}}, timeout=5.0))

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    # Simulate CC sending back an elicitation response
    import time; time.sleep(0.05)  # let _elicit register its event
    assert len(captured_push) == 1
    elicit_id = captured_push[0]["id"]
    # Inject the response as if the main loop received it from stdin
    daemon._elicit_results[elicit_id] = {"confirmed": True}
    daemon._elicit_events.pop(elicit_id).set()

    t.join(timeout=2.0)
    assert result_holder == [{"confirmed": True}]


def test_elicit_returns_none_on_timeout():
    """_elicit() must return None when no response arrives within timeout."""
    from scripts.emerge_daemon import EmergeDaemon
    daemon = EmergeDaemon()
    daemon._write_mcp_push = lambda _: None
    result = daemon._elicit("Confirm?", {}, timeout=0.1)
    assert result is None
```

- [ ] **Step 4.2: Run tests — verify FAIL**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_initialize_declares_elicitation_capability tests/test_mcp_tools_integration.py::test_elicit_returns_result_when_response_arrives tests/test_mcp_tools_integration.py::test_elicit_returns_none_on_timeout -q
```

Expected: all FAIL (wrong protocolVersion, no `_elicit` method)

- [ ] **Step 4.3: Upgrade protocol version and add `_elicit()`**

In `scripts/emerge_daemon.py`, update the initialize response (line 1250):

```python
                    "protocolVersion": "2025-03-26",
                    "capabilities": {
                        "tools": {},
                        "resources": {"subscribe": False},
                        "prompts": {},
                        "logging": {},
                        "elicitation": {},
                    },
```

Add `_elicit()` method to `EmergeDaemon` class (insert after `_write_mcp_push`):

```python
    def _elicit(
        self,
        message: str,
        schema: dict,
        timeout: float = 60.0,
    ) -> dict | None:
        """Send elicitations/create to CC; block current thread until response.

        Must be called from a worker thread (not the main stdio loop).
        Returns the ``content`` dict from the response, or None on timeout.
        """
        import uuid
        elicit_id = f"elicit-{uuid.uuid4().hex[:8]}"
        event = threading.Event()
        self._elicit_events[elicit_id] = event
        self._write_mcp_push({
            "jsonrpc": "2.0",
            "id": elicit_id,
            "method": "elicitations/create",
            "params": {"message": message, "requestedSchema": schema},
        })
        fired = event.wait(timeout=timeout)
        if not fired:
            self._elicit_events.pop(elicit_id, None)
            self._elicit_results.pop(elicit_id, None)
            return None
        return self._elicit_results.pop(elicit_id, None)
```

- [ ] **Step 4.4: Run tests — verify all pass**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_initialize_declares_elicitation_capability tests/test_mcp_tools_integration.py::test_elicit_returns_result_when_response_arrives tests/test_mcp_tools_integration.py::test_elicit_returns_none_on_timeout -q
```

Expected: `3 passed`

- [ ] **Step 4.5: Run full suite**

```bash
python -m pytest tests/ -q --tb=short 2>&1 | tail -5
```

Expected: same pass count as after Task 3

- [ ] **Step 4.6: Commit**

```bash
git add scripts/emerge_daemon.py
git commit -m "feat: MCP protocol 2025-03-26, elicitation capability, _elicit() helper"
```

---

## Task 5: Wire ElicitRequest into icc_span_approve, icc_reconcile, icc_hub resolve

**Files:**
- Modify: `scripts/emerge_daemon.py` — three `call_tool` branches
- Modify: `tests/test_mcp_tools_integration.py`

- [ ] **Step 5.1: Write failing tests**

Add to `tests/test_mcp_tools_integration.py`:

```python
def test_span_approve_elicitation_confirmed(tmp_path):
    """icc_span_approve must call _elicit and proceed when confirmed=True."""
    from scripts.emerge_daemon import EmergeDaemon
    from unittest.mock import patch, MagicMock
    daemon = EmergeDaemon()

    # Set up a stable span with a skeleton file
    conn, mode, name = "testconn", "read", "fetch"
    import os
    os.environ["EMERGE_CONNECTOR_ROOT"] = str(tmp_path)
    pending_dir = tmp_path / conn / "pipelines" / mode / "_pending"
    pending_dir.mkdir(parents=True)
    (pending_dir / f"{name}.py").write_text("def run_read(m,a): return {}\ndef verify_read(m,a,r): return True\n")

    # Patch span tracker to return "stable"
    with patch.object(daemon._span_tracker, "get_policy_status", return_value="stable"):
        with patch.object(daemon, "_elicit", return_value={"confirmed": True}) as mock_elicit:
            result = daemon.call_tool("icc_span_approve", {"intent_signature": f"{conn}.{mode}.{name}"})

    assert result.get("approved") is True
    mock_elicit.assert_called_once()
    os.environ.pop("EMERGE_CONNECTOR_ROOT", None)


def test_span_approve_elicitation_cancelled(tmp_path):
    """icc_span_approve must return cancellation message when confirmed=False."""
    from scripts.emerge_daemon import EmergeDaemon
    from unittest.mock import patch
    daemon = EmergeDaemon()

    conn, mode, name = "testconn", "read", "fetch"
    import os
    os.environ["EMERGE_CONNECTOR_ROOT"] = str(tmp_path)
    pending_dir = tmp_path / conn / "pipelines" / mode / "_pending"
    pending_dir.mkdir(parents=True)
    (pending_dir / f"{name}.py").write_text("def run_read(m,a): return {}\ndef verify_read(m,a,r): return True\n")

    with patch.object(daemon._span_tracker, "get_policy_status", return_value="stable"):
        with patch.object(daemon, "_elicit", return_value={"confirmed": False}):
            result = daemon.call_tool("icc_span_approve", {"intent_signature": f"{conn}.{mode}.{name}"})

    assert result.get("approved") is not True
    assert "cancel" in str(result).lower()
    os.environ.pop("EMERGE_CONNECTOR_ROOT", None)


def test_span_approve_elicitation_timeout(tmp_path):
    """icc_span_approve must return error when _elicit times out (returns None)."""
    from scripts.emerge_daemon import EmergeDaemon
    from unittest.mock import patch
    daemon = EmergeDaemon()

    conn, mode, name = "testconn", "read", "fetch"
    import os
    os.environ["EMERGE_CONNECTOR_ROOT"] = str(tmp_path)
    pending_dir = tmp_path / conn / "pipelines" / mode / "_pending"
    pending_dir.mkdir(parents=True)
    (pending_dir / f"{name}.py").write_text("def run_read(m,a): return {}\ndef verify_read(m,a,r): return True\n")

    with patch.object(daemon._span_tracker, "get_policy_status", return_value="stable"):
        with patch.object(daemon, "_elicit", return_value=None):
            result = daemon.call_tool("icc_span_approve", {"intent_signature": f"{conn}.{mode}.{name}"})

    assert result.get("isError") or "timed out" in str(result).lower()
    os.environ.pop("EMERGE_CONNECTOR_ROOT", None)


def test_reconcile_elicitation_used_when_outcome_not_provided():
    """icc_reconcile with no outcome must call _elicit to ask the user."""
    from scripts.emerge_daemon import EmergeDaemon
    from unittest.mock import patch
    daemon = EmergeDaemon()

    # Add a delta first
    from scripts.state_tracker import load_tracker, save_tracker
    state_path = daemon._hook_state_path()
    tracker = load_tracker(state_path)
    tracker.add_delta("test message", "info", intent_signature="test:sig")
    save_tracker(state_path, tracker)
    delta_id = tracker.state["deltas"][-1]["id"]

    with patch.object(daemon, "_elicit", return_value={"outcome": "confirm"}) as mock_elicit:
        result = daemon.call_tool("icc_reconcile", {"delta_id": delta_id})

    assert result.get("outcome") == "confirm"
    mock_elicit.assert_called_once()


def test_hub_resolve_elicitation_used_when_resolution_not_provided():
    """icc_hub resolve without resolution arg must call _elicit."""
    from scripts.emerge_daemon import EmergeDaemon
    from scripts.hub_config import save_pending_conflicts
    from unittest.mock import patch
    daemon = EmergeDaemon()

    save_pending_conflicts({"conflicts": [
        {"conflict_id": "c1", "connector": "gmail", "file": "x.py", "status": "pending"}
    ]})

    with patch.object(daemon, "_elicit", return_value={"resolution": "ours"}) as mock_elicit:
        result = daemon.call_tool("icc_hub", {
            "action": "resolve", "conflict_id": "c1"
        })

    assert result.get("ok") is True
    mock_elicit.assert_called_once()
```

- [ ] **Step 5.2: Run failing tests**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_span_approve_elicitation_confirmed tests/test_mcp_tools_integration.py::test_span_approve_elicitation_cancelled tests/test_mcp_tools_integration.py::test_span_approve_elicitation_timeout tests/test_mcp_tools_integration.py::test_reconcile_elicitation_used_when_outcome_not_provided tests/test_mcp_tools_integration.py::test_hub_resolve_elicitation_used_when_resolution_not_provided -q
```

Expected: FAIL — `_elicit` not yet called, behavior unchanged

- [ ] **Step 5.3: Update `icc_span_approve` branch**

In `scripts/emerge_daemon.py`, after the `if not pending_py.exists():` guard (line ~855), before the atomic move, add an elicitation confirmation:

```python
            # Ask CC user to confirm before activating the pipeline
            elicit_resp = self._elicit(
                f"确认激活 pipeline `{intent_signature}`？\n"
                f"将从 _pending/ 移动到 {real_dir} 并启用桥接。",
                {
                    "type": "object",
                    "properties": {"confirmed": {"type": "boolean", "title": "激活"}},
                    "required": ["confirmed"],
                },
            )
            if elicit_resp is None:
                return self._tool_error(
                    "icc_span_approve: elicitation timed out — operation cancelled"
                )
            if not elicit_resp.get("confirmed"):
                return self._tool_ok_json({
                    "approved": False,
                    "cancelled": True,
                    "message": "icc_span_approve cancelled by user.",
                })
```

Insert this block immediately before line 868 (`fd, tmp_py = tempfile.mkstemp(...)`).

- [ ] **Step 5.4: Update `icc_reconcile` branch — make `outcome` optional, elicit when absent**

Replace the `icc_reconcile` block (lines 1023–1051) with:

```python
        if name == "icc_reconcile":
            delta_id = str(arguments.get("delta_id", "")).strip()
            outcome = str(arguments.get("outcome", "")).strip()
            intent_signature = str(arguments.get("intent_signature", "")).strip()
            if not delta_id:
                return self._tool_error("icc_reconcile: delta_id is required")
            if outcome not in ("confirm", "correct", "retract"):
                # outcome not supplied — ask via ElicitRequest
                elicit_resp = self._elicit(
                    f"请选择 delta `{delta_id}` 的处置结果：",
                    {
                        "type": "object",
                        "properties": {
                            "outcome": {
                                "type": "string",
                                "enum": ["confirm", "correct", "retract"],
                                "title": "处置结果",
                            }
                        },
                        "required": ["outcome"],
                    },
                )
                if elicit_resp is None:
                    return self._tool_error(
                        "icc_reconcile: elicitation timed out — operation cancelled"
                    )
                outcome = str(elicit_resp.get("outcome", "")).strip()
                if outcome not in ("confirm", "correct", "retract"):
                    return self._tool_error(
                        f"icc_reconcile: invalid outcome from elicitation: {outcome!r}"
                    )
            from scripts.state_tracker import load_tracker, save_tracker
            state_path = self._hook_state_path()
            tracker = load_tracker(state_path)
            tracker.reconcile_delta(delta_id, outcome)
            save_tracker(state_path, tracker)
            td = tracker.to_dict()
            goal_snapshot = self._goal_control.read_snapshot()
            if outcome == "correct" and intent_signature:
                self._increment_human_fix(intent_signature)
            return self._tool_ok_json({
                "delta_id": delta_id,
                "outcome": outcome,
                "intent_signature": intent_signature or None,
                "verification_state": td.get("verification_state", "unverified"),
                "goal": goal_snapshot.get("text", ""),
                "goal_source": goal_snapshot.get("source", "unset"),
                "goal_version": goal_snapshot.get("version", 0),
            })
```

- [ ] **Step 5.5: Update `icc_hub resolve` — elicit when `resolution` absent**

In `scripts/emerge_daemon.py`, in `_handle_icc_hub`, find the `action == "resolve"` block (~line 1214). Add elicitation when `resolution` is empty:

```python
        if action == "resolve":
            conflict_id = str(arguments.get("conflict_id", "")).strip()
            resolution = str(arguments.get("resolution", "")).strip()
            if not conflict_id:
                return self._tool_error("icc_hub resolve: 'conflict_id' is required")
            if resolution not in ("ours", "theirs", "skip"):
                # resolution not supplied — ask via ElicitRequest
                elicit_resp = self._elicit(
                    f"请选择冲突 `{conflict_id}` 的解决策略：",
                    {
                        "type": "object",
                        "properties": {
                            "resolution": {
                                "type": "string",
                                "enum": ["ours", "theirs", "skip"],
                                "title": "解决策略",
                            }
                        },
                        "required": ["resolution"],
                    },
                )
                if elicit_resp is None:
                    return self._tool_error(
                        "icc_hub resolve: elicitation timed out — operation cancelled"
                    )
                resolution = str(elicit_resp.get("resolution", "")).strip()
                if resolution not in ("ours", "theirs", "skip"):
                    return self._tool_error(
                        f"icc_hub resolve: invalid resolution from elicitation: {resolution!r}"
                    )
            # (rest of existing resolve logic unchanged)
```

- [ ] **Step 5.6: Run all five tests — verify pass**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_span_approve_elicitation_confirmed tests/test_mcp_tools_integration.py::test_span_approve_elicitation_cancelled tests/test_mcp_tools_integration.py::test_span_approve_elicitation_timeout tests/test_mcp_tools_integration.py::test_reconcile_elicitation_used_when_outcome_not_provided tests/test_mcp_tools_integration.py::test_hub_resolve_elicitation_used_when_resolution_not_provided -q
```

Expected: `5 passed`

- [ ] **Step 5.7: Run full suite — no regressions**

```bash
python -m pytest tests/ -q --tb=short 2>&1 | tail -5
```

- [ ] **Step 5.8: Commit**

```bash
git add scripts/emerge_daemon.py tests/test_mcp_tools_integration.py
git commit -m "feat: ElicitRequest in icc_span_approve, icc_reconcile, icc_hub resolve"
```

---

## Task 6: Cockpit SSE endpoint

**Files:**
- Modify: `scripts/repl_admin.py` — add SSE endpoint + broadcast helpers
- Create: `tests/test_cockpit_sse.py`

- [ ] **Step 6.1: Write failing tests**

```python
# tests/test_cockpit_sse.py
from __future__ import annotations
import json
import threading
import time
import urllib.request
from pathlib import Path
import pytest


def _start_cockpit(tmp_path) -> tuple[int, "threading.Thread"]:
    """Start cockpit HTTP server on a random port; return (port, thread)."""
    from scripts import repl_admin
    import os
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path)
    import socket
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    server = repl_admin._make_cockpit_server("127.0.0.1", port, state_root=tmp_path)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    time.sleep(0.1)  # let server bind
    return port, t, server


def test_sse_status_returns_online_event(tmp_path):
    """GET /api/sse/status must stream an online event immediately."""
    port, _, server = _start_cockpit(tmp_path)
    try:
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/sse/status", timeout=2)
        assert resp.headers["Content-Type"] == "text/event-stream"
        line = resp.readline().decode().strip()
        assert line.startswith("data:")
        data = json.loads(line[5:])
        assert data["status"] == "online"
        assert "pid" in data
    finally:
        server.shutdown()


def test_sse_broadcast_reaches_connected_client(tmp_path):
    """_sse_broadcast must push events to all connected SSE clients."""
    from scripts import repl_admin
    port, _, server = _start_cockpit(tmp_path)
    try:
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/sse/status", timeout=2)
        resp.readline()  # consume initial online event
        # Broadcast from server side
        repl_admin._sse_broadcast({"status": "test_event", "x": 42})
        time.sleep(0.1)
        line = resp.readline().decode().strip()
        assert line.startswith("data:")
        data = json.loads(line[5:])
        assert data["x"] == 42
    finally:
        server.shutdown()
```

- [ ] **Step 6.2: Run tests — verify FAIL**

```bash
python -m pytest tests/test_cockpit_sse.py -q 2>&1 | head -10
```

Expected: FAIL — `/api/sse/status` returns 404

- [ ] **Step 6.3: Add SSE globals and broadcast helper to `scripts/repl_admin.py`**

Add near the top of the file (after existing imports):

```python
import io as _io

_sse_clients: list[_io.RawIOBase] = []
_sse_lock = threading.Lock()


def _sse_broadcast(event: dict) -> None:
    """Push *event* to all connected SSE clients; silently drop dead connections."""
    data = f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode()
    with _sse_lock:
        dead = []
        for wfile in _sse_clients:
            try:
                wfile.write(data)
                wfile.flush()
            except OSError:
                dead.append(wfile)
        for d in dead:
            _sse_clients.remove(d)
```

- [ ] **Step 6.4: Add SSE route to the cockpit request handler**

In `scripts/repl_admin.py`, find the `do_GET` method of the cockpit HTTP request handler. Add the SSE route before the default 404 handler:

```python
        if self.path == "/api/sse/status":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            # Send initial state
            msg = json.dumps({
                "status": "online",
                "pid": os.getpid(),
                "ts_ms": int(time.time() * 1000),
            }, ensure_ascii=False)
            self.wfile.write(f"data: {msg}\n\n".encode())
            self.wfile.flush()
            # Register and block until client disconnects
            with _sse_lock:
                _sse_clients.append(self.wfile)
            try:
                while True:
                    time.sleep(25)
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
            except OSError:
                pass
            finally:
                with _sse_lock:
                    if self.wfile in _sse_clients:
                        _sse_clients.remove(self.wfile)
            return
```

Also add an `atexit` in the cockpit startup to broadcast offline on shutdown:

```python
import atexit as _atexit
_atexit.register(lambda: _sse_broadcast({"status": "offline"}))
```

- [ ] **Step 6.5: Run SSE tests**

```bash
python -m pytest tests/test_cockpit_sse.py -q
```

Expected: `2 passed`

- [ ] **Step 6.6: Run full suite**

```bash
python -m pytest tests/ -q --tb=short 2>&1 | tail -5
```

- [ ] **Step 6.7: Commit**

```bash
git add scripts/repl_admin.py tests/test_cockpit_sse.py
git commit -m "feat: cockpit SSE /api/sse/status — replaces cc-listening.json heartbeat"
```

---

## Task 7: Daemon EventRouter integration (replace PendingActionMonitor)

**Files:**
- Modify: `scripts/emerge_daemon.py` — add `start_event_router`, `_on_pending_actions`, `_on_local_events`; update `run_stdio`

- [ ] **Step 7.1: Write failing test**

Add to `tests/test_mcp_tools_integration.py`:

```python
def test_event_router_replaces_pending_monitor(tmp_path):
    """EventRouter must fire MCP push when pending-actions.json is created."""
    import threading, json, time
    from scripts.emerge_daemon import EmergeDaemon
    import os
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path)
    daemon = EmergeDaemon()

    pushed = []
    daemon._write_mcp_push = lambda p: pushed.append(p)
    daemon.start_event_router()
    time.sleep(0.1)

    pending = tmp_path / "pending-actions.json"
    pending.write_text(json.dumps({
        "submitted_at": int(time.time() * 1000),
        "actions": [{"type": "prompt", "prompt": "hello"}]
    }))

    # Wait for EventRouter to pick it up
    deadline = time.time() + 3.0
    while time.time() < deadline and not pushed:
        time.sleep(0.05)

    daemon.stop_event_router()
    os.environ.pop("EMERGE_STATE_ROOT", None)

    assert len(pushed) == 1
    assert pushed[0]["method"] == "notifications/claude/channel"
    assert pushed[0]["params"]["meta"]["source"] == "cockpit"
```

- [ ] **Step 7.2: Run test — verify FAIL**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_event_router_replaces_pending_monitor -q
```

Expected: `AttributeError: 'EmergeDaemon' object has no attribute 'start_event_router'`

- [ ] **Step 7.3: Add EventRouter methods to `EmergeDaemon`**

Add these methods to the `EmergeDaemon` class (after `stop_pending_monitor`):

```python
    def start_event_router(self) -> None:
        """Start EventRouter to watch pending-actions.json and local operator events."""
        from scripts.event_router import EventRouter
        from scripts.hub_config import sync_queue_path

        handlers = {
            self._state_root / "pending-actions.json": lambda _: self._on_pending_actions(),
        }
        # Watch local operator-events directory for EventBus JSONL changes
        event_root = Path.home() / ".emerge" / "operator-events"
        if event_root.exists():
            handlers[event_root] = lambda p: self._on_local_event_file(p)

        self._event_router = EventRouter(handlers)
        self._event_router.start()
        self._last_seen_pending_ts: int = 0

    def stop_event_router(self) -> None:
        if getattr(self, "_event_router", None) is not None:
            self._event_router.stop()

    def _on_pending_actions(self) -> None:
        """Called by EventRouter when pending-actions.json is created/modified."""
        pending_path = self._state_root / "pending-actions.json"
        if not pending_path.exists():
            return
        try:
            text = pending_path.read_text(encoding="utf-8")
        except OSError:
            return
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            try:
                pending_path.rename(self._state_root / "pending-actions.invalid.json")
            except OSError:
                pass
            return
        ts = int(data.get("submitted_at", 0))
        if ts <= self._last_seen_pending_ts:
            return
        actions = data.get("actions", [])
        self._write_mcp_push({
            "jsonrpc": "2.0",
            "method": "notifications/claude/channel",
            "params": {
                "serverName": "emerge",
                "content": _format_pending_actions_message(actions),
                "meta": {
                    "source": "cockpit",
                    "action_count": len(actions),
                    "action_types": list({a.get("type") for a in actions}),
                },
            },
        })
        try:
            processed = self._state_root / "pending-actions.processed.json"
            pending_path.rename(processed)
            self._last_seen_pending_ts = ts
        except OSError:
            pass

    def _on_local_event_file(self, path: Path) -> None:
        """Called by EventRouter when an operator events.jsonl file changes."""
        if not self._operator_monitor:
            return
        # Delegate to existing _poll_local logic on OperatorMonitor
        try:
            self._operator_monitor._poll_local()
        except Exception:
            pass
```

Also add `self._event_router = None` and `self._last_seen_pending_ts = 0` to `__init__` (after `self._pending_monitor`).

Update `run_stdio()` to start EventRouter instead of PendingActionMonitor:

```python
    daemon.start_event_router()
    atexit.register(daemon.stop_event_router)
```

(Replace the `start_pending_monitor` / `stop_pending_monitor` lines.)

- [ ] **Step 7.4: Run test**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_event_router_replaces_pending_monitor -q
```

Expected: PASS

- [ ] **Step 7.5: Run full suite**

```bash
python -m pytest tests/ -q --tb=short 2>&1 | tail -5
```

- [ ] **Step 7.6: Commit**

```bash
git add scripts/emerge_daemon.py tests/test_mcp_tools_integration.py
git commit -m "feat: daemon EventRouter — replaces PendingActionMonitor polling thread"
```

---

## Task 8: Cleanup — delete deprecated polling code

**Files:**
- Modify: `scripts/emerge_daemon.py` — delete `PendingActionMonitor` class, `start_pending_monitor`, `stop_pending_monitor`
- Modify: `scripts/repl_admin.py` — delete `cmd_wait_for_submit`, `_write_cc_listening`, `_cc_listening_path`, `_wait_for_submit_acquire`, `_wait_for_submit_release`, `_wait_for_submit_pid_path`
- Modify: `scripts/operator_monitor.py` — delete `_poll_local`; EventRouter now handles local events
- Modify: tests that referenced deleted code

- [ ] **Step 8.1: Run test suite before deletion to establish baseline**

```bash
python -m pytest tests/ -q 2>&1 | tail -3
```

Note the pass count.

- [ ] **Step 8.2: Delete `PendingActionMonitor` and its lifecycle methods from `emerge_daemon.py`**

Delete:
- `class PendingActionMonitor` (lines 2735–2803)
- `def start_pending_monitor` (lines 2650–2659)
- `def stop_pending_monitor` (lines 2661–2663)
- Remove `self._pending_monitor` from `__init__`

- [ ] **Step 8.3: Delete wait-for-submit and cc-listening from `repl_admin.py`**

Delete:
- `def _cc_listening_path()` (line 2041)
- `def _write_cc_listening()` (lines 2045–2060)
- `def _wait_for_submit_pid_path()` (line 1874)
- `def _wait_for_submit_acquire()` (lines 1878–1892)
- `def _wait_for_submit_release()` (lines 1894–1907 approx)
- `def cmd_wait_for_submit()` (lines 2065–2112)
- Remove `"wait-for-submit"` from the `choices` list in `main()` argparse

- [ ] **Step 8.4: Delete `_poll_local` from `operator_monitor.py`**

Delete `def _poll_local` (lines 60–105).

Update `OperatorMonitor.run()` — remove the `_poll_local` call:

```python
    def run(self) -> None:
        while not self._stop_event.wait(timeout=self._poll_interval_s):
            for machine_id, client in self._machines.items():
                try:
                    self._poll_machine(machine_id, client)
                except Exception:
                    pass
            # Local operator events are now handled by EventRouter in emerge_daemon
```

- [ ] **Step 8.5: Update tests that referenced deleted code**

In `tests/test_operator_monitor.py`, remove any tests for `_poll_local`.

In `tests/test_repl_admin.py`, remove any tests for `cmd_wait_for_submit` or `_write_cc_listening`.

- [ ] **Step 8.6: Run full test suite — verify clean**

```bash
python -m pytest tests/ -q --tb=short 2>&1 | tail -10
```

Expected: same or higher pass count than baseline (deleted-code tests are gone; no new failures).

- [ ] **Step 8.7: Commit cleanup**

```bash
git add scripts/emerge_daemon.py scripts/repl_admin.py scripts/operator_monitor.py tests/
git commit -m "refactor: delete PendingActionMonitor, wait-for-submit, cc-listening, _poll_local"
```

---

## Task 9: Update documentation

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`

- [ ] **Step 9.1: Update README architecture section**

In `README.md`, update the architecture diagram and component table:
- Replace "PendingActionMonitor: 2s polling thread" → "EventRouter: watchdog-based inotify/FSEvents"
- Replace "cc-listening.json heartbeat" → "SSE /api/sse/status"
- Add `elicitation: {}` to MCP capabilities row
- Note protocolVersion is now `2025-03-26`

- [ ] **Step 9.2: Update CLAUDE.md**

In the Architecture section, update:
- Remove `PendingActionMonitor` references
- Add EventRouter description
- Update Key Invariants: remove cc-listening.json heartbeat invariant; add EventRouter drain-on-start invariant

- [ ] **Step 9.3: Commit docs**

```bash
git add README.md CLAUDE.md
git commit -m "docs: update architecture docs for event-driven refactor"
```

---

## Final verification

```bash
python -m pytest tests/ -q
```

Expected: all tests pass. The following should no longer appear in `scripts/`:
- `PendingActionMonitor`
- `_write_cc_listening`
- `cmd_wait_for_submit`
- `time.sleep(0.5)` in cockpit wait loop
- `time.sleep(10)` in emerge_sync poll loop
