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


def test_event_router_watchdog_fires_on_atomic_rename(tmp_path):
    """on_moved must fire callback when a file is atomically renamed into place."""
    import os
    from scripts.event_router import EventRouter
    try:
        import watchdog  # noqa: F401
    except ImportError:
        pytest.skip("watchdog not installed — polling fallback doesn't catch rename")
    fired = []
    target = tmp_path / "test-events.jsonl"
    router = EventRouter({target: lambda p: fired.append(p)})
    router.start()
    # Small delay to let watchdog register
    time.sleep(0.2)
    # Atomic write
    tmp = tmp_path / "test-events.jsonl.tmp"
    tmp.write_text('{"type": "test", "ts_ms": 1}')
    os.rename(str(tmp), str(target))
    # Wait for event
    deadline = time.time() + 3.0
    while time.time() < deadline and not fired:
        time.sleep(0.05)
    router.stop()
    assert len(fired) >= 1, "on_moved callback never fired for atomic rename"
