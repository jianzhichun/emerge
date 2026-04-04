from __future__ import annotations

import importlib.util
import sys
from abc import ABC, abstractmethod
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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

    def get_plugin(self, name: str) -> ObserverPlugin:
        if name in self._cache:
            return self._cache[name]

        # Try crystallized adapter first
        adapter_path = self._adapter_root / name / "adapter.py"
        if adapter_path.exists():
            plugin = self._load_from_file(name, adapter_path)
            if plugin is not None:
                self._cache[name] = plugin
                return plugin

        # Try built-in observer
        builtin_path = ROOT / "scripts" / "observers" / f"{name}.py"
        if builtin_path.exists():
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
        except Exception:
            return None
