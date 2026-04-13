# tests/test_observer_plugin.py
from __future__ import annotations
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.observer_plugin import ObserverPlugin, AdapterRegistry
import pytest


class _StubObserver(ObserverPlugin):
    def start(self, config: dict) -> None:
        self.started = True
        self.config = config

    def stop(self) -> None:
        self.stopped = True

    def get_context(self, hint: dict) -> dict:
        return {"stub": True, "hint": hint}

    def execute(self, intent: str, params: dict) -> dict:
        return {"ok": True, "intent": intent}


def test_observer_plugin_interface():
    obs = _StubObserver()
    obs.start({"key": "val"})
    assert obs.started
    assert obs.config == {"key": "val"}
    ctx = obs.get_context({"app": "zwcad"})
    assert ctx["stub"] is True
    result = obs.execute("zwcad.annotate", {})
    assert result["ok"] is True
    obs.stop()
    assert obs.stopped


def test_adapter_registry_loads_builtin_observers(tmp_path):
    registry = AdapterRegistry(adapter_root=tmp_path)
    plugins = registry.list_plugins()
    names = [p["name"] for p in plugins]
    assert "accessibility" in names
    assert "filesystem" in names
    assert "clipboard" in names


def test_adapter_registry_loads_custom_adapter(tmp_path):
    adapter_dir = tmp_path / "zwcad"
    adapter_dir.mkdir()
    (adapter_dir / "adapter.py").write_text(
        "from scripts.observer_plugin import ObserverPlugin\n"
        "class ZWCADAdapter(ObserverPlugin):\n"
        "    def start(self, config):\n"
        "        self.running = True\n"
        "    def stop(self): pass\n"
        "    def get_context(self, hint): return {'vertical': 'zwcad'}\n"
        "    def execute(self, intent, params): return {'ok': True}\n"
        "ADAPTER_CLASS = ZWCADAdapter\n",
        encoding="utf-8",
    )
    registry = AdapterRegistry(adapter_root=tmp_path)
    plugin = registry.get_plugin("zwcad")
    assert plugin is not None
    plugin.start({})
    assert plugin.running is True
    ctx = plugin.get_context({})
    assert ctx["vertical"] == "zwcad"


def test_adapter_registry_fallback_to_generic(tmp_path):
    # No zwcad adapter → fallback to _GenericFallback
    registry = AdapterRegistry(adapter_root=tmp_path)
    plugin = registry.get_plugin("zwcad")
    assert plugin is not None
    ctx = plugin.get_context({"app": "zwcad"})
    assert isinstance(ctx, dict)


def test_adapter_registry_rejects_path_traversal_name(tmp_path):
    """get_plugin must reject names with path traversal characters."""
    from scripts.observer_plugin import AdapterRegistry, _GenericFallback
    registry = AdapterRegistry(adapter_root=tmp_path)

    # These names must all return the generic fallback, not attempt to load a file
    for evil_name in ["../evil", "../../etc/passwd", "..", "foo/../bar", "/abs/path"]:
        plugin = registry.get_plugin(evil_name)
        assert isinstance(plugin, _GenericFallback), (
            f"Expected GenericFallback for {evil_name!r}, got {type(plugin).__name__}"
        )


def test_adapter_registry_rejects_uppercase_name(tmp_path):
    """Names with uppercase letters are rejected (OperatorMonitor may pass raw app names)."""
    from scripts.observer_plugin import AdapterRegistry, _GenericFallback
    registry = AdapterRegistry(adapter_root=tmp_path)

    # "ZWCAD" from an event should not load arbitrary files
    plugin = registry.get_plugin("ZWCAD")
    assert isinstance(plugin, _GenericFallback)


def test_adapter_registry_accepts_valid_names(tmp_path):
    """Valid lowercase names still work normally (return fallback when no adapter file)."""
    from scripts.observer_plugin import AdapterRegistry, _GenericFallback
    registry = AdapterRegistry(adapter_root=tmp_path)

    for good_name in ["zwcad", "hypermesh", "cloud-server", "my_app2"]:
        plugin = registry.get_plugin(good_name)
        assert plugin is not None, f"Expected a plugin for {good_name!r}"


# ---------------------------------------------------------------------------
# Gap B: emit_event on ObserverPlugin base class
# ---------------------------------------------------------------------------

def test_emit_event_writes_to_eventbus(tmp_path, monkeypatch):
    """ObserverPlugin.emit_event() appends an event to events.jsonl in event_root/<machine_id>/."""
    import json
    import socket
    import time

    class _Adapter(_StubObserver):
        pass

    adapter = _Adapter()
    machine_id = socket.gethostname()

    # Redirect home to tmp_path so we don't pollute the real ~/.emerge
    emerge_home = tmp_path / ".emerge"
    monkeypatch.setattr(
        "pathlib.Path.home",
        staticmethod(lambda: tmp_path),
    )

    adapter.emit_event({
        "event_type": "test_event",
        "intent_signature": "zwcad.write.apply-change",
        "session_role": "operator",
    })

    event_path = tmp_path / ".emerge" / "operator-events" / machine_id / "events.jsonl"
    assert event_path.exists(), "emit_event must create events.jsonl"
    lines = [l for l in event_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1

    event = json.loads(lines[0])
    assert event["event_type"] == "test_event"
    assert event["intent_signature"] == "zwcad.write.apply-change"
    assert "ts_ms" in event
    assert "machine_id" in event


def test_emit_event_prepopulates_ts_ms_and_machine_id(tmp_path, monkeypatch):
    """emit_event injects ts_ms and machine_id automatically."""
    import json
    import socket

    class _Adapter(_StubObserver):
        pass

    monkeypatch.setattr("pathlib.Path.home", staticmethod(lambda: tmp_path))

    adapter = _Adapter()
    adapter.emit_event({"event_type": "ping"})

    machine_id = socket.gethostname()
    event_path = tmp_path / ".emerge" / "operator-events" / machine_id / "events.jsonl"
    event = json.loads(event_path.read_text(encoding="utf-8").strip())
    assert event["machine_id"] == machine_id
    assert isinstance(event["ts_ms"], int)


def test_emit_event_does_not_override_caller_ts_ms(tmp_path, monkeypatch):
    """emit_event respects ts_ms if the caller already provides it."""
    import json

    class _Adapter(_StubObserver):
        pass

    monkeypatch.setattr("pathlib.Path.home", staticmethod(lambda: tmp_path))

    adapter = _Adapter()
    adapter.emit_event({"event_type": "ping", "ts_ms": 999_000})

    import socket
    machine_id = socket.gethostname()
    event_path = tmp_path / ".emerge" / "operator-events" / machine_id / "events.jsonl"
    event = json.loads(event_path.read_text(encoding="utf-8").strip())
    # Caller-provided ts_ms must win (update() overwrites the default)
    assert event["ts_ms"] == 999_000


# ---------------------------------------------------------------------------
# Gap D: vertical adapter stubs (loaded via AdapterRegistry)
# ---------------------------------------------------------------------------

def _make_adapter_dir(tmp_path: Path, connector: str) -> Path:
    """Write a minimal vertical adapter to tmp_path/<connector>/adapter.py and return the dir."""
    adapter_code = (
        "from scripts.observer_plugin import ObserverPlugin\n"
        f"CONNECTOR = '{connector}'\n"
        "class _Adapter(ObserverPlugin):\n"
        "    def start(self, config): self._config = config\n"
        "    def stop(self): pass\n"
        "    def get_context(self, hint):\n"
        f"        return {{'observer': '{connector}', **hint}}\n"
        "    def execute(self, intent, params):\n"
        "        parts = intent.split('.')\n"
        "        if len(parts) < 3 or parts[0] != CONNECTOR:\n"
        f"            return {{'ok': False, 'error': 'wrong connector'}}\n"
        "        return {'ok': True, 'summary': f'dispatched {intent}'}\n"
        "ADAPTER_CLASS = _Adapter\n"
    )
    d = tmp_path / connector
    d.mkdir(parents=True, exist_ok=True)
    (d / "adapter.py").write_text(adapter_code, encoding="utf-8")
    return d


@pytest.mark.parametrize("connector", ["hypermesh", "zwcad", "cloud-server"])
def test_vertical_adapter_loads_and_executes(tmp_path, connector):
    """Vertical adapters for each connector load via AdapterRegistry and execute correctly."""
    _make_adapter_dir(tmp_path, connector)
    registry = AdapterRegistry(adapter_root=tmp_path)
    plugin = registry.get_plugin(connector)
    assert plugin is not None

    plugin.start({})
    ctx = plugin.get_context({"app": connector})
    assert ctx["observer"] == connector

    # Valid intent for the connector
    result = plugin.execute(f"{connector}.write.apply-change", {})
    assert result["ok"] is True

    # Wrong connector must return ok=False
    result_bad = plugin.execute("other.write.apply-change", {})
    assert result_bad["ok"] is False


@pytest.mark.parametrize("connector", ["hypermesh", "zwcad", "cloud-server"])
def test_vertical_adapter_execute_wrong_intent_returns_error(tmp_path, connector):
    """execute() with wrong connector prefix returns ok=False."""
    _make_adapter_dir(tmp_path, connector)
    registry = AdapterRegistry(adapter_root=tmp_path)
    plugin = registry.get_plugin(connector)
    result = plugin.execute("wrong.write.something", {})
    assert result["ok"] is False
