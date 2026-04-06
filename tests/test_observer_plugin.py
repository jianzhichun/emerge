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
