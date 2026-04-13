from __future__ import annotations

import importlib.util
import json
import logging
import sys
import time
from abc import ABC, abstractmethod
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_log = logging.getLogger(__name__)


class ObserverPlugin(ABC):
    """Base class for all operator behavior observers."""

    @abstractmethod
    def start(self, config: dict) -> None:
        """Begin monitoring. Called once on activation."""

    @abstractmethod
    def stop(self) -> None:
        """Stop monitoring and release resources."""

    @abstractmethod
    def get_context(self, hint: dict) -> dict:
        """Pre-elicitation context read. Returns enriched context dict."""

    @abstractmethod
    def execute(self, intent: str, params: dict) -> dict:
        """Takeover execution after operator confirms. Returns {ok, summary, ...}."""

    def emit_event(self, event: dict) -> None:
        """Write an operator event to the local EventBus.

        Vertical adapters call this to record domain-specific events so that
        PatternDetector can observe recurring human operations. The event dict
        should include at minimum: event_type, intent_signature, and any context
        fields useful for pattern detection. ts_ms and machine_id are injected
        automatically if absent.
        """
        try:
            import socket as _socket
            machine_id = _socket.gethostname()
            event_dir = Path.home() / ".emerge" / "operator-events" / machine_id
            event_dir.mkdir(parents=True, exist_ok=True)
            event_path = event_dir / "events.jsonl"
            full_event = {
                "ts_ms": int(time.time() * 1000),
                "machine_id": machine_id,
            }
            full_event.update(event)
            with event_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(full_event, ensure_ascii=True) + "\n")
        except Exception:
            pass  # non-fatal


class _GenericFallback(ObserverPlugin):
    """Minimal observer used as fallback when no vertical adapter is available."""

    def start(self, config: dict) -> None:
        self._config = config

    def stop(self) -> None:
        pass

    def get_context(self, hint: dict) -> dict:
        return {"observer": "generic", "hint": hint}

    def execute(self, intent: str, params: dict) -> dict:
        return {"ok": False, "error": "generic observer cannot execute — crystallize a vertical adapter first"}


class AdapterRegistry:
    """Loads ObserverPlugin instances: built-in generic observers + crystallized vertical adapters."""

    _BUILTINS = ("accessibility", "filesystem", "clipboard")

    def __init__(self, adapter_root: Path | None = None) -> None:
        self._adapter_root = adapter_root or (Path.home() / ".emerge" / "adapters")
        self._cache: dict[str, ObserverPlugin] = {}

    def list_plugins(self) -> list[dict]:
        names: list[str] = list(self._BUILTINS)
        if self._adapter_root.exists():
            for d in self._adapter_root.iterdir():
                if d.is_dir() and (d / "adapter.py").exists():
                    if d.name not in names:
                        names.append(d.name)
        return [{"name": n} for n in names]

    _SAFE_NAME_RE = __import__("re").compile(r"^[a-z0-9][a-z0-9_-]*$")

    def get_plugin(self, name: str) -> ObserverPlugin:
        if name in self._cache:
            return self._cache[name]

        # Reject names that could escape the adapter/observer directories.
        if not self._SAFE_NAME_RE.match(name):
            fallback = _GenericFallback()
            self._cache[name] = fallback
            return fallback

        # Try crystallized adapter first — validate resolved path stays inside adapter_root
        adapter_path = self._adapter_root / name / "adapter.py"
        try:
            adapter_path.resolve().relative_to(self._adapter_root.resolve())
            adapter_exists = adapter_path.exists()
        except ValueError:
            adapter_exists = False
        if adapter_exists:
            plugin = self._load_from_file(name, adapter_path)
            if plugin is not None:
                self._cache[name] = plugin
                return plugin

        # Try built-in observer — validate resolved path stays inside observers dir
        observers_root = ROOT / "scripts" / "observers"
        builtin_path = observers_root / f"{name}.py"
        try:
            builtin_path.resolve().relative_to(observers_root.resolve())
            builtin_exists = builtin_path.exists()
        except ValueError:
            builtin_exists = False
        if builtin_exists:
            plugin = self._load_from_file(name, builtin_path)
            if plugin is not None:
                self._cache[name] = plugin
                return plugin

        # Fallback: generic observer
        fallback = _GenericFallback()
        self._cache[name] = fallback
        return fallback

    @staticmethod
    def _load_from_file(name: str, path: Path) -> ObserverPlugin | None:
        try:
            spec = importlib.util.spec_from_file_location(f"_adapter_{name}", path)
            if spec is None or spec.loader is None:
                return None
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            cls = getattr(mod, "ADAPTER_CLASS", None)
            if cls is None:
                for attr in vars(mod).values():
                    if (
                        isinstance(attr, type)
                        and issubclass(attr, ObserverPlugin)
                        and attr is not ObserverPlugin
                    ):
                        cls = attr
                        break
            if cls is None:
                return None
            return cls()
        except Exception as exc:
            _log.warning("AdapterRegistry: failed to load adapter %r from %s: %s", name, path, exc)
            return None
