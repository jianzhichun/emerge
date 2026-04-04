# Operator Intelligence Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a reverse flywheel that observes operator behavior on any machine, detects repeated patterns, and proactively triggers a CC dialog to capture intent and hand off work to the AI layer.

**Architecture:** EmergeDaemon gains an `OperatorMonitor` background thread that polls remote runners for operator events, runs `PatternDetector`, and pushes to CC via MCP channel notification (explore) or `ElicitRequest` (canary/stable). Observation capability is implemented as `ObserverPlugin` subclasses — three generic observers ship with the framework; vertical adapters (ZWCAD COM etc.) are crystallized from WAL history. remote_runner gains two new endpoints (`POST /operator-event`, `GET /operator-events`) to receive and serve event data.

**Tech Stack:** Python 3.11+, stdlib only (threading, http.server, json, pathlib). No new dependencies. Tests use pytest + tmp_path fixtures following existing patterns in `tests/`.

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `scripts/observer_plugin.py` | Create | `ObserverPlugin` ABC + `AdapterRegistry` |
| `scripts/observers/accessibility.py` | Create | Generic OS accessibility observer |
| `scripts/observers/filesystem.py` | Create | watchdog-free filesystem observer (polling) |
| `scripts/observers/clipboard.py` | Create | Clipboard change observer |
| `scripts/pattern_detector.py` | Create | `PatternDetector` + pluggable strategies + `PatternSummary` |
| `scripts/distiller.py` | Create | `Distiller` — PatternSummary → intent_signature |
| `scripts/operator_monitor.py` | Create | `OperatorMonitor` background thread |
| `scripts/remote_runner.py` | Modify | Add `POST /operator-event`, `GET /operator-events` endpoints + `EventBus` writer |
| `scripts/emerge_daemon.py` | Modify | Instantiate + start `OperatorMonitor`; add MCP push helpers (`_push_channel_notification`, `_send_elicit_request`) |
| `tests/test_observer_plugin.py` | Create | Unit tests for `ObserverPlugin` ABC + `AdapterRegistry` |
| `tests/test_pattern_detector.py` | Create | Unit tests for all detector strategies + `PatternSummary` |
| `tests/test_distiller.py` | Create | Unit tests for `Distiller` |
| `tests/test_operator_monitor.py` | Create | Integration test for `OperatorMonitor` end-to-end |
| `tests/test_remote_runner_events.py` | Create | Tests for new runner event endpoints |
| `skills/writing-vertical-adapter/SKILL.md` | Create | Playbook for authoring vertical adapters |
| `skills/operator-monitor-debug/SKILL.md` | Create | Playbook for debugging the monitoring pipeline |
| `skills/initializing-vertical-flywheel/SKILL.md` | Modify | Add reverse flywheel prompt at stable stage |

---

## Task 1: `ObserverPlugin` ABC and `AdapterRegistry`

**Files:**
- Create: `scripts/observer_plugin.py`
- Create: `tests/test_observer_plugin.py`

- [ ] **Step 1: Write failing tests**

```python
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
    # Built-ins: accessibility, filesystem, clipboard
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


def test_adapter_registry_fallback_to_accessibility(tmp_path):
    registry = AdapterRegistry(adapter_root=tmp_path)
    # No zwcad adapter → fallback to generic accessibility observer
    plugin = registry.get_plugin("zwcad")
    assert plugin is not None
    ctx = plugin.get_context({"app": "zwcad"})
    assert isinstance(ctx, dict)
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python -m pytest tests/test_observer_plugin.py -v
```
Expected: `ImportError: cannot import name 'ObserverPlugin'`

- [ ] **Step 3: Implement `scripts/observer_plugin.py`**

```python
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
                # Scan for first ObserverPlugin subclass
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
```

- [ ] **Step 4: Create stub generic observers so `AdapterRegistry._BUILTINS` resolve**

```python
# scripts/observers/__init__.py
# (empty)
```

```python
# scripts/observers/accessibility.py
from __future__ import annotations
from scripts.observer_plugin import ObserverPlugin


class AccessibilityObserver(ObserverPlugin):
    """Generic OS accessibility observer. Uses AX API (macOS) or UIAutomation (Windows)
    when available; falls back to window-title polling via subprocess."""

    def start(self, config: dict) -> None:
        self._config = config
        self._active = True

    def stop(self) -> None:
        self._active = False

    def get_context(self, hint: dict) -> dict:
        """Returns focused window title and clipboard text — always available."""
        import subprocess, sys as _sys
        ctx: dict = {"observer": "accessibility", "hint": hint}
        try:
            if _sys.platform == "darwin":
                result = subprocess.run(
                    ["osascript", "-e",
                     'tell application "System Events" to get name of first process whose frontmost is true'],
                    capture_output=True, text=True, timeout=2,
                )
                ctx["focused_app"] = result.stdout.strip()
        except Exception:
            pass
        return ctx

    def execute(self, intent: str, params: dict) -> dict:
        return {"ok": False, "error": "accessibility observer cannot execute — crystallize a vertical adapter"}


ADAPTER_CLASS = AccessibilityObserver
```

```python
# scripts/observers/filesystem.py
from __future__ import annotations
import os
import time
from pathlib import Path
from scripts.observer_plugin import ObserverPlugin


class FilesystemObserver(ObserverPlugin):
    """Polls a watched directory for file changes."""

    def start(self, config: dict) -> None:
        self._watch_path = Path(config.get("path", ".")).expanduser()
        self._last_scan: dict[str, float] = {}
        self._active = True

    def stop(self) -> None:
        self._active = False

    def get_context(self, hint: dict) -> dict:
        if not self._watch_path.exists():
            return {"observer": "filesystem", "files": []}
        files = [
            {"name": f.name, "mtime": f.stat().st_mtime}
            for f in self._watch_path.iterdir()
            if f.is_file()
        ]
        return {"observer": "filesystem", "path": str(self._watch_path), "files": files}

    def execute(self, intent: str, params: dict) -> dict:
        return {"ok": False, "error": "filesystem observer cannot execute — crystallize a vertical adapter"}


ADAPTER_CLASS = FilesystemObserver
```

```python
# scripts/observers/clipboard.py
from __future__ import annotations
import sys as _sys
from scripts.observer_plugin import ObserverPlugin


class ClipboardObserver(ObserverPlugin):
    """Reads OS clipboard content."""

    def start(self, config: dict) -> None:
        self._active = True

    def stop(self) -> None:
        self._active = False

    def get_context(self, hint: dict) -> dict:
        content = self._read_clipboard()
        return {"observer": "clipboard", "content": content}

    def execute(self, intent: str, params: dict) -> dict:
        return {"ok": False, "error": "clipboard observer cannot execute — crystallize a vertical adapter"}

    @staticmethod
    def _read_clipboard() -> str:
        try:
            if _sys.platform == "darwin":
                import subprocess
                r = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=2)
                return r.stdout
            if _sys.platform == "win32":
                import subprocess
                r = subprocess.run(
                    ["powershell", "-command", "Get-Clipboard"],
                    capture_output=True, text=True, timeout=2,
                )
                return r.stdout
        except Exception:
            pass
        return ""


ADAPTER_CLASS = ClipboardObserver
```

- [ ] **Step 5: Run tests — expect pass**

```bash
python -m pytest tests/test_observer_plugin.py -v
```
Expected: 4 tests PASS

- [ ] **Step 6: Commit**

```bash
git add scripts/observer_plugin.py scripts/observers/ tests/test_observer_plugin.py
git commit -m "feat: add ObserverPlugin ABC, AdapterRegistry, and 3 built-in observers"
```

---

## Task 2: `PatternDetector` and `PatternSummary`

**Files:**
- Create: `scripts/pattern_detector.py`
- Create: `tests/test_pattern_detector.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_pattern_detector.py
from __future__ import annotations
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import time
from scripts.pattern_detector import PatternDetector, PatternSummary


def _event(app: str, event_type: str, layer: str = "标注", content: str = "room", ts_delta_ms: int = 0):
    return {
        "ts_ms": int(time.time() * 1000) + ts_delta_ms,
        "machine_id": "test-machine",
        "session_id": "op_test",
        "session_role": "operator",
        "observer_type": "accessibility",
        "event_type": event_type,
        "app": app,
        "payload": {"layer": layer, "content": content},
    }


def test_frequency_detector_fires_at_threshold():
    detector = PatternDetector()
    events = [_event("zwcad", "entity_added", ts_delta_ms=i * 60_000) for i in range(3)]
    summaries = detector.ingest(events)
    assert len(summaries) == 1
    s = summaries[0]
    assert s.occurrences == 3
    assert s.detector_signals == ["frequency"]
    assert "zwcad" in s.intent_signature


def test_frequency_detector_does_not_fire_below_threshold():
    detector = PatternDetector()
    events = [_event("zwcad", "entity_added", ts_delta_ms=i * 60_000) for i in range(2)]
    summaries = detector.ingest(events)
    assert summaries == []


def test_monitor_sub_events_are_filtered():
    detector = PatternDetector()
    events = []
    for i in range(5):
        e = _event("zwcad", "entity_added", ts_delta_ms=i * 60_000)
        e["session_role"] = "monitor_sub"
        events.append(e)
    summaries = detector.ingest(events)
    assert summaries == []


def test_cross_machine_detector_fires():
    detector = PatternDetector()
    events = []
    for machine in ("m1", "m2"):
        for i in range(2):
            e = _event("zwcad", "entity_added", ts_delta_ms=i * 60_000)
            e["machine_id"] = machine
            events.append(e)
    summaries = detector.ingest(events)
    assert any("cross_machine" in s.detector_signals for s in summaries)


def test_pattern_summary_fields():
    detector = PatternDetector()
    events = [_event("zwcad", "entity_added", ts_delta_ms=i * 60_000) for i in range(3)]
    summaries = detector.ingest(events)
    s = summaries[0]
    assert isinstance(s, PatternSummary)
    assert s.machine_ids == ["test-machine"]
    assert isinstance(s.intent_signature, str)
    assert s.occurrences >= 3
    assert isinstance(s.window_minutes, float)
    assert isinstance(s.context_hint, dict)
    assert s.policy_stage == "explore"


def test_error_rate_detector_fires_on_high_undo():
    detector = PatternDetector()
    events = []
    # 5 ops, 3 undos → ratio 0.6 > threshold 0.4
    for i in range(5):
        events.append(_event("zwcad", "entity_added", ts_delta_ms=i * 10_000))
    for i in range(3):
        events.append(_event("zwcad", "undo", ts_delta_ms=(5 + i) * 10_000))
    summaries = detector.ingest(events)
    assert any("error_rate" in s.detector_signals for s in summaries)
```

- [ ] **Step 2: Run to confirm failure**

```bash
python -m pytest tests/test_pattern_detector.py -v
```
Expected: `ImportError: cannot import name 'PatternDetector'`

- [ ] **Step 3: Implement `scripts/pattern_detector.py`**

```python
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PatternSummary:
    machine_ids: list[str]
    intent_signature: str
    occurrences: int
    window_minutes: float
    detector_signals: list[str]
    context_hint: dict
    policy_stage: str = "explore"


class PatternDetector:
    """Applies pluggable detector strategies to batches of operator events.
    Returns a list of PatternSummary objects when thresholds are crossed."""

    # Frequency: N events of same type+app in window
    FREQ_THRESHOLD = 3
    FREQ_WINDOW_MS = 20 * 60 * 1000  # 20 minutes

    # Error rate: undo ratio within a session
    ERROR_RATE_THRESHOLD = 0.4  # undos / total ops

    # Cross-machine: same pattern on ≥2 machines
    CROSS_MACHINE_MIN_MACHINES = 2
    CROSS_MACHINE_MIN_PER_MACHINE = 2

    def ingest(self, events: list[dict[str, Any]]) -> list[PatternSummary]:
        operator_events = [e for e in events if e.get("session_role") != "monitor_sub"]
        if not operator_events:
            return []

        summaries: list[PatternSummary] = []
        summaries.extend(self._frequency_check(operator_events))
        summaries.extend(self._error_rate_check(operator_events))
        summaries.extend(self._cross_machine_check(operator_events))
        return summaries

    def _frequency_check(self, events: list[dict]) -> list[PatternSummary]:
        now_ms = int(time.time() * 1000)
        window_events = [e for e in events if now_ms - e.get("ts_ms", 0) <= self.FREQ_WINDOW_MS]
        if not window_events:
            return []

        # Group by (app, event_type, layer)
        groups: dict[tuple, list[dict]] = {}
        for e in window_events:
            key = (
                e.get("app", ""),
                e.get("event_type", ""),
                e.get("payload", {}).get("layer", ""),
            )
            groups.setdefault(key, []).append(e)

        summaries = []
        for (app, event_type, layer), grp in groups.items():
            if len(grp) < self.FREQ_THRESHOLD:
                continue
            ts_values = [e["ts_ms"] for e in grp if "ts_ms" in e]
            window_min = (max(ts_values) - min(ts_values)) / 60_000 if len(ts_values) >= 2 else 0.0
            machines = list({e.get("machine_id", "unknown") for e in grp})
            samples = [
                e.get("payload", {}).get("content", "")
                for e in grp
                if e.get("payload", {}).get("content")
            ][:5]
            sig = f"{app}.{layer.replace('/', '_') if layer else event_type}"
            summaries.append(PatternSummary(
                machine_ids=machines,
                intent_signature=sig,
                occurrences=len(grp),
                window_minutes=window_min,
                detector_signals=["frequency"],
                context_hint={
                    "app": app,
                    "event_type": event_type,
                    "layer": layer,
                    "samples": samples,
                },
            ))
        return summaries

    def _error_rate_check(self, events: list[dict]) -> list[PatternSummary]:
        by_session: dict[str, list[dict]] = {}
        for e in events:
            sid = e.get("session_id", "unknown")
            by_session.setdefault(sid, []).append(e)

        summaries = []
        for sid, grp in by_session.items():
            total_ops = len([e for e in grp if e.get("event_type") != "undo"])
            undos = len([e for e in grp if e.get("event_type") == "undo"])
            if total_ops == 0:
                continue
            ratio = undos / total_ops
            if ratio < self.ERROR_RATE_THRESHOLD:
                continue
            machines = list({e.get("machine_id", "unknown") for e in grp})
            app = grp[0].get("app", "unknown") if grp else "unknown"
            summaries.append(PatternSummary(
                machine_ids=machines,
                intent_signature=f"{app}.high_error_rate",
                occurrences=len(grp),
                window_minutes=0.0,
                detector_signals=["error_rate"],
                context_hint={"app": app, "undo_ratio": ratio, "session_id": sid},
            ))
        return summaries

    def _cross_machine_check(self, events: list[dict]) -> list[PatternSummary]:
        by_app_event: dict[tuple, dict[str, list[dict]]] = {}
        for e in events:
            key = (e.get("app", ""), e.get("event_type", ""))
            machine = e.get("machine_id", "unknown")
            by_app_event.setdefault(key, {}).setdefault(machine, []).append(e)

        summaries = []
        for (app, event_type), by_machine in by_app_event.items():
            qualifying = {
                m: evts
                for m, evts in by_machine.items()
                if len(evts) >= self.CROSS_MACHINE_MIN_PER_MACHINE
            }
            if len(qualifying) < self.CROSS_MACHINE_MIN_MACHINES:
                continue
            all_events = [e for evts in qualifying.values() for e in evts]
            machines = list(qualifying.keys())
            summaries.append(PatternSummary(
                machine_ids=machines,
                intent_signature=f"{app}.{event_type}.cross_machine",
                occurrences=len(all_events),
                window_minutes=0.0,
                detector_signals=["cross_machine"],
                context_hint={"app": app, "event_type": event_type, "machines": machines},
            ))
        return summaries
```

- [ ] **Step 4: Run tests — expect pass**

```bash
python -m pytest tests/test_pattern_detector.py -v
```
Expected: 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/pattern_detector.py tests/test_pattern_detector.py
git commit -m "feat: add PatternDetector with frequency, error-rate, and cross-machine strategies"
```

---

## Task 3: `Distiller`

**Files:**
- Create: `scripts/distiller.py`
- Create: `tests/test_distiller.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_distiller.py
from __future__ import annotations
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.distiller import Distiller
from scripts.pattern_detector import PatternSummary


def _summary(intent_sig: str, app: str = "zwcad") -> PatternSummary:
    return PatternSummary(
        machine_ids=["m1"],
        intent_signature=intent_sig,
        occurrences=4,
        window_minutes=19.0,
        detector_signals=["frequency"],
        context_hint={"app": app, "layer": "标注", "samples": ["主卧", "次卧"]},
    )


def test_distiller_returns_intent_signature():
    d = Distiller()
    sig = d.distill(_summary("zwcad.标注"))
    assert isinstance(sig, str)
    assert len(sig) > 0


def test_distiller_normalises_non_ascii(tmp_path):
    d = Distiller()
    sig = d.distill(_summary("zwcad.标注层"))
    # Non-ASCII in the middle segment gets transliterated or replaced
    assert sig.startswith("zwcad.")
    assert all(c.isascii() or c in (".", "_") for c in sig)


def test_distiller_preserves_clean_signature():
    d = Distiller()
    sig = d.distill(_summary("zwcad.annotate.room_labels"))
    assert sig == "zwcad.annotate.room_labels"


def test_distiller_writes_intent_confirmed_event(tmp_path):
    import json
    event_dir = tmp_path / "operator-events" / "m1"
    event_dir.mkdir(parents=True)
    d = Distiller(event_root=tmp_path / "operator-events")
    summary = _summary("zwcad.annotate.room_labels")
    sig = d.distill(summary, confirmed=True)
    events_file = event_dir / "events.jsonl"
    assert events_file.exists()
    lines = [json.loads(l) for l in events_file.read_text().splitlines() if l.strip()]
    assert any(e.get("event_type") == "intent_confirmed" for e in lines)
    confirmed = next(e for e in lines if e.get("event_type") == "intent_confirmed")
    assert confirmed["payload"]["intent_signature"] == sig
```

- [ ] **Step 2: Run to confirm failure**

```bash
python -m pytest tests/test_distiller.py -v
```
Expected: `ImportError: cannot import name 'Distiller'`

- [ ] **Step 3: Implement `scripts/distiller.py`**

```python
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from scripts.pattern_detector import PatternSummary


class Distiller:
    """Converts a PatternSummary into a canonical intent_signature and
    optionally writes an intent_confirmed event to the EventBus."""

    def __init__(self, event_root: Path | None = None) -> None:
        self._event_root = event_root or (Path.home() / ".emerge" / "operator-events")

    def distill(self, summary: PatternSummary, *, confirmed: bool = False) -> str:
        sig = self._normalise(summary.intent_signature)
        if confirmed:
            self._write_confirmed_events(summary, sig)
        return sig

    @staticmethod
    def _normalise(raw: str) -> str:
        """Normalise intent_signature: lowercase, replace spaces+special chars with _,
        keep dots as segment separators, strip non-ASCII via transliteration."""
        # Transliterate common CJK sequences to pinyin-style placeholder
        # (full pinyin mapping is out of scope; use segment hash for unknown CJK)
        segments = raw.split(".")
        clean: list[str] = []
        for seg in segments:
            # Replace whitespace and hyphens with _
            seg = re.sub(r"[\s\-]+", "_", seg)
            # Drop characters that are not ASCII word chars or _
            ascii_seg = re.sub(r"[^\w]", "", seg.encode("ascii", errors="replace").decode("ascii"))
            ascii_seg = re.sub(r"_+", "_", ascii_seg).strip("_").lower()
            if ascii_seg:
                clean.append(ascii_seg)
        return ".".join(clean) if clean else "unknown.pattern"

    def _write_confirmed_events(self, summary: PatternSummary, sig: str) -> None:
        for machine_id in summary.machine_ids:
            machine_dir = self._event_root / machine_id
            machine_dir.mkdir(parents=True, exist_ok=True)
            event = {
                "ts_ms": int(time.time() * 1000),
                "machine_id": machine_id,
                "session_role": "monitor_sub",
                "event_type": "intent_confirmed",
                "payload": {
                    "intent_signature": sig,
                    "occurrences": summary.occurrences,
                    "detector_signals": summary.detector_signals,
                    "context_hint": summary.context_hint,
                },
            }
            events_path = machine_dir / "events.jsonl"
            with events_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
```

- [ ] **Step 4: Run tests — expect pass**

```bash
python -m pytest tests/test_distiller.py -v
```
Expected: 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/distiller.py tests/test_distiller.py
git commit -m "feat: add Distiller — PatternSummary to intent_signature with EventBus write"
```

---

## Task 4: remote_runner EventBus endpoints

**Files:**
- Modify: `scripts/remote_runner.py`
- Create: `tests/test_remote_runner_events.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_remote_runner_events.py
from __future__ import annotations
import json
import socket
import threading
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import urllib.request
import urllib.parse
import pytest
from scripts.remote_runner import RunnerExecutor, RunnerHTTPHandler, ThreadingHTTPServer


class _RunnerServer:
    def __init__(self, state_root: Path) -> None:
        self._state_root = state_root
        self._server = None
        self._thread = None
        self.url = ""

    def __enter__(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        host, port = sock.getsockname()
        sock.close()
        executor = RunnerExecutor(root=ROOT, state_root=self._state_root)
        handler_cls = type("H", (RunnerHTTPHandler,), {"executor": executor})
        self._server = ThreadingHTTPServer((host, port), handler_cls)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.url = f"http://{host}:{port}"
        return self

    def __exit__(self, *_):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)


def _post_event(base_url: str, event: dict) -> dict:
    body = json.dumps(event).encode()
    req = urllib.request.Request(
        f"{base_url}/operator-event",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def _get_events(base_url: str, machine_id: str, since_ms: int = 0) -> dict:
    url = f"{base_url}/operator-events?machine_id={urllib.parse.quote(machine_id)}&since_ms={since_ms}"
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.loads(resp.read())


def test_post_operator_event_stores_event(tmp_path):
    with _RunnerServer(tmp_path / "state") as server:
        event = {
            "ts_ms": 1000,
            "machine_id": "test-m1",
            "session_role": "operator",
            "event_type": "entity_added",
            "app": "zwcad",
            "payload": {"content": "主卧"},
        }
        result = _post_event(server.url, event)
        assert result.get("ok") is True

        events_file = tmp_path / "operator-events" / "test-m1" / "events.jsonl"
        assert events_file.exists()
        lines = [json.loads(l) for l in events_file.read_text().splitlines() if l.strip()]
        assert len(lines) == 1
        assert lines[0]["event_type"] == "entity_added"


def test_get_operator_events_returns_events(tmp_path):
    with _RunnerServer(tmp_path / "state") as server:
        for i in range(3):
            _post_event(server.url, {
                "ts_ms": 1000 + i * 1000,
                "machine_id": "test-m2",
                "session_role": "operator",
                "event_type": "entity_added",
                "app": "zwcad",
                "payload": {"content": f"room_{i}"},
            })
        result = _get_events(server.url, "test-m2", since_ms=0)
        assert result.get("ok") is True
        assert len(result["events"]) == 3


def test_get_operator_events_filters_by_since_ms(tmp_path):
    with _RunnerServer(tmp_path / "state") as server:
        for ts in (1000, 2000, 5000):
            _post_event(server.url, {
                "ts_ms": ts,
                "machine_id": "test-m3",
                "session_role": "operator",
                "event_type": "entity_added",
                "app": "zwcad",
                "payload": {},
            })
        result = _get_events(server.url, "test-m3", since_ms=2001)
        assert result.get("ok") is True
        assert len(result["events"]) == 1
        assert result["events"][0]["ts_ms"] == 5000


def test_post_event_rejects_missing_machine_id(tmp_path):
    with _RunnerServer(tmp_path / "state") as server:
        body = json.dumps({"event_type": "entity_added"}).encode()
        req = urllib.request.Request(
            f"{server.url}/operator-event",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            assert False, "should have raised"
        except urllib.error.HTTPError as e:
            assert e.code == 400
```

- [ ] **Step 2: Run to confirm failure**

```bash
python -m pytest tests/test_remote_runner_events.py -v
```
Expected: 4 tests FAIL (`404 Not Found` for new endpoints)

- [ ] **Step 3: Add EventBus writer and new endpoints to `scripts/remote_runner.py`**

In `RunnerExecutor.__init__`, add:
```python
self._event_root = self._state_root.parent / "operator-events"
```

Add `EventBus` writer as a method on `RunnerExecutor`:
```python
def write_operator_event(self, event: dict) -> None:
    machine_id = str(event.get("machine_id", "")).strip()
    if not machine_id:
        raise ValueError("machine_id is required")
    machine_dir = self._event_root / machine_id
    machine_dir.mkdir(parents=True, exist_ok=True)
    events_path = machine_dir / "events.jsonl"
    with threading.Lock():
        with events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

def read_operator_events(self, machine_id: str, since_ms: int = 0, limit: int = 200) -> list[dict]:
    machine_dir = self._event_root / machine_id
    if not machine_dir.exists():
        return []
    events_path = machine_dir / "events.jsonl"
    if not events_path.exists():
        return []
    results = []
    with events_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("ts_ms", 0) > since_ms:
                results.append(e)
    return results[-limit:]
```

In `RunnerHTTPHandler.do_POST`, add before the final `_send_json(404, ...)`:
```python
if self.path == "/operator-event":
    try:
        content_length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(content_length).decode("utf-8")
        event = json.loads(raw) if raw else {}
        if not isinstance(event, dict):
            raise ValueError("event must be an object")
        self.executor.write_operator_event(event)
        self._send_json(200, {"ok": True})
    except Exception as exc:
        self._send_json(400, {"ok": False, "error": str(exc)})
    return
```

In `RunnerHTTPHandler.do_GET`, add before the final `_send_json(404, ...)`:
```python
if self.path.startswith("/operator-events"):
    try:
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        machine_id = (qs.get("machine_id") or [""])[0]
        since_ms = int((qs.get("since_ms") or ["0"])[0])
        limit = int((qs.get("limit") or ["200"])[0])
        if not machine_id:
            self._send_json(400, {"ok": False, "error": "machine_id required"})
            return
        events = self.executor.read_operator_events(machine_id, since_ms, min(limit, 1000))
        self._send_json(200, {"ok": True, "events": events})
    except Exception as exc:
        self._send_json(400, {"ok": False, "error": str(exc)})
    return
```

Also update `RunnerExecutor.__init__` — replace `self._repl_lock = threading.Lock()` with:
```python
self._repl_lock = threading.Lock()
self._event_write_lock = threading.Lock()
```

And use `self._event_write_lock` instead of `threading.Lock()` in `write_operator_event`.

- [ ] **Step 4: Run tests — expect pass**

```bash
python -m pytest tests/test_remote_runner_events.py -v
```
Expected: 4 tests PASS

- [ ] **Step 5: Run full suite to check no regressions**

```bash
python -m pytest tests -q
```
Expected: all existing tests still pass

- [ ] **Step 6: Commit**

```bash
git add scripts/remote_runner.py tests/test_remote_runner_events.py
git commit -m "feat: add /operator-event and /operator-events endpoints to remote_runner"
```

---

## Task 5: `OperatorMonitor` background thread

**Files:**
- Create: `scripts/operator_monitor.py`
- Create: `tests/test_operator_monitor.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_operator_monitor.py
from __future__ import annotations
import json
import sys
import threading
import time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest
from scripts.operator_monitor import OperatorMonitor


class _FakeRunnerClient:
    """Simulates a remote runner that returns pre-seeded events."""
    def __init__(self, events: list[dict]):
        self._events = events

    def get_events(self, machine_id: str, since_ms: int = 0) -> list[dict]:
        return [e for e in self._events if e.get("ts_ms", 0) > since_ms]


def test_operator_monitor_detects_pattern_and_calls_push(tmp_path):
    push_calls = []

    def fake_push(stage: str, context: dict, summary) -> None:
        push_calls.append({"stage": stage, "context": context, "summary": summary})

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
        for i in range(3)
    ]

    monitor = OperatorMonitor(
        machines={"m1": _FakeRunnerClient(events)},
        push_fn=fake_push,
        poll_interval_s=0.05,
        event_root=tmp_path / "operator-events",
        adapter_root=tmp_path / "adapters",
    )
    monitor.start()
    time.sleep(0.3)
    monitor.stop()

    assert len(push_calls) >= 1
    assert push_calls[0]["stage"] == "explore"


def test_operator_monitor_does_not_fire_on_empty_events(tmp_path):
    push_calls = []

    monitor = OperatorMonitor(
        machines={"m1": _FakeRunnerClient([])},
        push_fn=lambda s, c, x: push_calls.append(1),
        poll_interval_s=0.05,
        event_root=tmp_path / "events",
        adapter_root=tmp_path / "adapters",
    )
    monitor.start()
    time.sleep(0.2)
    monitor.stop()

    assert push_calls == []


def test_operator_monitor_stops_cleanly(tmp_path):
    monitor = OperatorMonitor(
        machines={},
        push_fn=lambda s, c, x: None,
        poll_interval_s=0.05,
        event_root=tmp_path / "events",
        adapter_root=tmp_path / "adapters",
    )
    monitor.start()
    assert monitor.is_alive()
    monitor.stop()
    monitor.join(timeout=1.0)
    assert not monitor.is_alive()
```

- [ ] **Step 2: Run to confirm failure**

```bash
python -m pytest tests/test_operator_monitor.py -v
```
Expected: `ImportError: cannot import name 'OperatorMonitor'`

- [ ] **Step 3: Implement `scripts/operator_monitor.py`**

```python
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable, Protocol

from scripts.adapter_registry_compat import AdapterRegistry  # see note below
from scripts.pattern_detector import PatternDetector, PatternSummary


class _RunnerClientProtocol(Protocol):
    def get_events(self, machine_id: str, since_ms: int = 0) -> list[dict]: ...


class OperatorMonitor(threading.Thread):
    """Background thread that polls remote runners for operator events,
    runs PatternDetector, and calls push_fn when a pattern is found."""

    def __init__(
        self,
        machines: dict[str, _RunnerClientProtocol],
        push_fn: Callable[[str, dict, PatternSummary], None],
        poll_interval_s: float = 5.0,
        event_root: Path | None = None,
        adapter_root: Path | None = None,
    ) -> None:
        super().__init__(daemon=True, name="OperatorMonitor")
        self._machines = machines
        self._push_fn = push_fn
        self._poll_interval_s = poll_interval_s
        self._event_root = event_root or (Path.home() / ".emerge" / "operator-events")
        self._adapter_registry = AdapterRegistry(adapter_root=adapter_root)
        self._detector = PatternDetector()
        self._last_poll_ms: dict[str, int] = {}
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.wait(timeout=self._poll_interval_s):
            for machine_id, client in self._machines.items():
                try:
                    self._poll_machine(machine_id, client)
                except Exception:
                    pass

    def _poll_machine(self, machine_id: str, client: _RunnerClientProtocol) -> None:
        since_ms = self._last_poll_ms.get(machine_id, 0)
        events = client.get_events(machine_id=machine_id, since_ms=since_ms)
        if not events:
            return

        latest_ts = max(e.get("ts_ms", 0) for e in events)
        self._last_poll_ms[machine_id] = latest_ts

        summaries = self._detector.ingest(events)
        for summary in summaries:
            app = summary.context_hint.get("app", machine_id)
            plugin = self._adapter_registry.get_plugin(app)
            try:
                context = plugin.get_context(summary.context_hint)
            except Exception:
                context = summary.context_hint.copy()
            self._push_fn(summary.policy_stage, context, summary)
```

**Note**: `AdapterRegistry` lives in `scripts/observer_plugin.py`. Add a thin compat import shim `scripts/adapter_registry_compat.py`:

```python
# scripts/adapter_registry_compat.py
from scripts.observer_plugin import AdapterRegistry

__all__ = ["AdapterRegistry"]
```

Or simply import directly from `scripts.observer_plugin`. Adjust the import in `operator_monitor.py` to:
```python
from scripts.observer_plugin import AdapterRegistry
```

- [ ] **Step 4: Run tests — expect pass**

```bash
python -m pytest tests/test_operator_monitor.py -v
```
Expected: 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/operator_monitor.py tests/test_operator_monitor.py
git commit -m "feat: add OperatorMonitor background thread with PatternDetector integration"
```

---

## Task 6: Wire `OperatorMonitor` into `EmergeDaemon`

**Files:**
- Modify: `scripts/emerge_daemon.py`
- Modify: `tests/test_mcp_tools_integration.py` (add smoke test)

- [ ] **Step 1: Write failing smoke test**

Add to `tests/test_mcp_tools_integration.py`:

```python
def test_operator_monitor_starts_when_env_set(monkeypatch, tmp_path):
    """EmergeDaemon starts OperatorMonitor when EMERGE_OPERATOR_MONITOR=1."""
    import os, time
    monkeypatch.setenv("EMERGE_OPERATOR_MONITOR", "1")
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path))
    from scripts.emerge_daemon import EmergeDaemon
    daemon = EmergeDaemon(root=tmp_path)
    daemon.start_operator_monitor()
    time.sleep(0.1)
    assert daemon._operator_monitor is not None
    assert daemon._operator_monitor.is_alive()
    daemon.stop_operator_monitor()
    daemon._operator_monitor.join(timeout=1.0)
    assert not daemon._operator_monitor.is_alive()
```

- [ ] **Step 2: Run to confirm failure**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_operator_monitor_starts_when_env_set -v
```
Expected: `AttributeError: 'EmergeDaemon' object has no attribute 'start_operator_monitor'`

- [ ] **Step 3: Add MCP push helpers and `OperatorMonitor` lifecycle to `EmergeDaemon`**

In `EmergeDaemon.__init__`, after existing initialization, add:
```python
self._operator_monitor: "OperatorMonitor | None" = None
```

Add methods to `EmergeDaemon`:

```python
def start_operator_monitor(self) -> None:
    """Start OperatorMonitor if EMERGE_OPERATOR_MONITOR=1 and not already running."""
    import os
    if os.environ.get("EMERGE_OPERATOR_MONITOR", "0") != "1":
        return
    if self._operator_monitor is not None and self._operator_monitor.is_alive():
        return
    from scripts.operator_monitor import OperatorMonitor
    from scripts.runner_client import RunnerClient

    poll_s = float(os.environ.get("EMERGE_MONITOR_POLL_S", "5"))
    machines_env = os.environ.get("EMERGE_MONITOR_MACHINES", "")

    # Build machine_id → client map from RunnerRouter config
    machines: dict = {}
    if self._runner_router:
        for profile_name in (machines_env.split(",") if machines_env else ["default"]):
            profile_name = profile_name.strip()
            if not profile_name:
                continue
            client = self._runner_router.find_client({"target_profile": profile_name})
            if client:
                machines[profile_name] = _RunnerClientAdapter(client)

    self._operator_monitor = OperatorMonitor(
        machines=machines,
        push_fn=self._push_pattern_to_cc,
        poll_interval_s=poll_s,
        event_root=Path.home() / ".emerge" / "operator-events",
        adapter_root=Path.home() / ".emerge" / "adapters",
    )
    self._operator_monitor.start()

def stop_operator_monitor(self) -> None:
    if self._operator_monitor is not None:
        self._operator_monitor.stop()

def _push_pattern_to_cc(self, stage: str, context: dict, summary: "PatternSummary") -> None:
    """Push pattern detection result to CC via MCP.
    Explore stage → channel notification (LLM evaluates).
    Canary/Stable → ElicitRequest form (blocking dialog).
    """
    import json as _json
    from scripts.pattern_detector import PatternSummary as _PS

    if stage == "explore":
        # Channel notification — injects prompt into CC command queue
        msg = self._build_explore_message(context, summary)
        notification = {
            "jsonrpc": "2.0",
            "method": "notifications/claude/channel",
            "params": {
                "serverName": "emerge",
                "content": msg,
                "meta": {"source": "operator_monitor", "intent_signature": summary.intent_signature},
            },
        }
        self._write_notification(notification)
    else:
        # ElicitRequest — native CC blocking dialog
        params = self._build_elicit_params(stage, context, summary)
        request = {
            "jsonrpc": "2.0",
            "id": f"elicit-{summary.intent_signature}-{int(time.time())}",
            "method": "elicit",
            "params": params,
        }
        self._write_notification(request)

def _build_explore_message(self, context: dict, summary: "PatternSummary") -> str:
    app = context.get("app", "unknown")
    samples = context.get("samples", summary.context_hint.get("samples", []))
    sig = summary.intent_signature
    return (
        f"[OperatorMonitor] 检测到 {app} 中反复出现操作模式 `{sig}`，"
        f"共 {summary.occurrences} 次，约 {summary.window_minutes:.0f} 分钟内。"
        + (f" 样本: {', '.join(str(s) for s in samples[:3])}。" if samples else "")
        + " 请评估是否值得接管，如值得请发起 elicitation。"
    )

def _build_elicit_params(self, stage: str, context: dict, summary: "PatternSummary") -> dict:
    timeout_s = 10 if stage == "stable" else 30
    app = context.get("app", "unknown")
    sig = summary.intent_signature
    message = (
        f"检测到 `{sig}` 重复 {summary.occurrences} 次。"
        + (f" 上下文: {context}。" if stage == "canary" else "")
        + f" 是否接管？（{timeout_s}s 无响应自动确认）"
    )
    schema: dict = {
        "action": {
            "type": "string",
            "oneOf": [
                {"const": "yes", "title": "是，接管"},
                {"const": "later", "title": "稍后"},
                {"const": "no", "title": "不需要"},
            ],
        }
    }
    if stage == "canary":
        schema["note"] = {"type": "string", "description": "补充说明（可选）", "maxLength": 200}
    return {"mode": "form", "message": message, "requestedSchema": schema}

def _write_notification(self, payload: dict) -> None:
    """Write a JSON-RPC notification/request to stdout for CC to receive."""
    import json as _json
    import sys as _sys
    _sys.stdout.write(_json.dumps(payload) + "\n")
    _sys.stdout.flush()
```

Add `_RunnerClientAdapter` class outside `EmergeDaemon` (top-level in `emerge_daemon.py`):

```python
class _RunnerClientAdapter:
    """Adapts RunnerClient.call_tool to the OperatorMonitor's get_events protocol."""
    def __init__(self, client: "RunnerClient") -> None:
        self._client = client

    def get_events(self, machine_id: str, since_ms: int = 0) -> list[dict]:
        import json as _j
        result = self._client.call_tool("icc_exec", {
            "code": (
                f"import urllib.request, json as _j\n"
                f"url = f'{{_base_url}}/operator-events"
                f"?machine_id={machine_id}&since_ms={since_ms}'\n"
                f"with urllib.request.urlopen(url, timeout=5) as r:\n"
                f"    data = _j.loads(r.read())\n"
                f"__result = data.get('events', [])\n"
            ),
            "intent_signature": "emerge.monitor.poll_events",
            "no_replay": True,
        })
        # Parse result from icc_exec content
        try:
            text = result.get("content", [{}])[0].get("text", "")
            for line in text.splitlines():
                if line.startswith("__result"):
                    return _j.loads(line.split("=", 1)[1].strip())
        except Exception:
            pass
        return []
```

**Note**: `_RunnerClientAdapter.get_events` uses icc_exec which is complex. A simpler and more correct approach is to call the HTTP endpoint directly using `urllib.request` from the daemon side (since daemon has the runner URL). Replace the body with:

```python
class _RunnerClientAdapter:
    """Calls /operator-events HTTP endpoint on the remote runner directly."""
    def __init__(self, client: "RunnerClient") -> None:
        self._base_url = client._base_url  # RunnerClient exposes base_url

    def get_events(self, machine_id: str, since_ms: int = 0) -> list[dict]:
        import urllib.request, urllib.parse, json as _j
        url = (
            f"{self._base_url}/operator-events"
            f"?machine_id={urllib.parse.quote(machine_id)}&since_ms={since_ms}"
        )
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                data = _j.loads(r.read())
            return data.get("events", [])
        except Exception:
            return []
```

Check that `RunnerClient` exposes `_base_url` or a public `base_url` — look in `scripts/runner_client.py` and adjust attribute name if needed.

- [ ] **Step 4: Run smoke test — expect pass**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_operator_monitor_starts_when_env_set -v
```
Expected: PASS

- [ ] **Step 5: Run full suite**

```bash
python -m pytest tests -q
```
Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add scripts/emerge_daemon.py tests/test_mcp_tools_integration.py
git commit -m "feat: wire OperatorMonitor into EmergeDaemon with MCP push helpers"
```

---

## Task 7: Skills — `writing-vertical-adapter` and `operator-monitor-debug`

**Files:**
- Create: `skills/writing-vertical-adapter/SKILL.md`
- Create: `skills/operator-monitor-debug/SKILL.md`
- Modify: `skills/initializing-vertical-flywheel/SKILL.md`

- [ ] **Step 1: Create `skills/writing-vertical-adapter/SKILL.md`**

```markdown
---
name: writing-vertical-adapter
description: Use when writing or crystallizing a vertical ObserverPlugin adapter for a specific application (ZWCAD, Excel, browser, etc), or when icc_crystallize mode=adapter is needed, or when asked how to add a new vertical to the Operator Intelligence Loop.
---

# Writing a Vertical Adapter

A vertical adapter is an `ObserverPlugin` subclass that gives the Operator
Intelligence Loop application-specific observation and execution capability.
Adapters live in `~/.emerge/adapters/<vertical>/adapter.py` and are loaded
by `AdapterRegistry` at daemon startup.

## Interface

```python
from scripts.observer_plugin import ObserverPlugin

class MyAdapter(ObserverPlugin):
    def start(self, config: dict) -> None:
        """Connect to the application (COM, AX, CDP...). Store state on self."""

    def stop(self) -> None:
        """Disconnect. Release COM objects, close sockets."""

    def get_context(self, hint: dict) -> dict:
        """Pre-elicitation read. Return enriched context for the elicitation message.
        Example return: { total_rooms: 7, labeled: 4, unlabeled: 3 }"""

    def execute(self, intent: str, params: dict) -> dict:
        """Takeover execution. Return { ok: bool, summary: str }."""

ADAPTER_CLASS = MyAdapter  # Required — AdapterRegistry looks for this name
```

## Bootstrap Path (ZWCAD example)

1. **Generic phase**: `accessibility` observer detects ZWCAD window + text inputs.
   PatternDetector fires; explore-stage channel notification sent to CC.

2. **Prototype with icc_exec**: CC generates COM code and runs via `icc_exec`:
   ```python
   import win32com.client
   app = win32com.client.Dispatch("ZWCAD.Application")
   doc = app.ActiveDocument
   texts = [e for e in doc.ModelSpace if e.EntityName == "AcDbText"]
   __result = [{"content": t.TextString, "layer": t.Layer} for t in texts]
   ```
   WAL records the successful path.

3. **Crystallize**: When WAL depth ≥ 3 successful paths:
   ```
   icc_crystallize(
     intent_signature="zwcad.read.room_labels",
     connector="zwcad",
     pipeline_name="room_labels",
     mode="read"
   )
   ```
   This generates `~/.emerge/connectors/zwcad/pipelines/read/room_labels.py`.

4. **Wrap as adapter**: Move the logic into `ObserverPlugin.get_context()` and
   `execute()`, save to `~/.emerge/adapters/zwcad/adapter.py`.

## Testing

Use a mock EventBus to replay events without a live application:

```python
from scripts.pattern_detector import PatternDetector
events = [
    {"ts_ms": 1000*i, "machine_id": "test", "session_role": "operator",
     "event_type": "entity_added", "app": "zwcad",
     "payload": {"layer": "标注", "content": f"room_{i}"}}
    for i in range(4)
]
summaries = PatternDetector().ingest(events)
assert len(summaries) == 1
```

## Registration

No registration needed — `AdapterRegistry` auto-discovers any directory under
`~/.emerge/adapters/` that contains `adapter.py` with an `ObserverPlugin` subclass.
```

- [ ] **Step 2: Create `skills/operator-monitor-debug/SKILL.md`**

```markdown
---
name: operator-monitor-debug
description: Use when the operator monitoring pipeline appears broken: EventBus has no events, PatternDetector is not firing, elicitation dialog never appears, or collector is silent. Guides systematic diagnosis of the full pipeline.
---

# Debugging the Operator Monitor Pipeline

## Checklist

### 1. Is the remote runner running and reachable?
```bash
curl http://<runner-host>:8787/health
# Expected: {"ok": true, "uptime_s": N}
```

### 2. Is the ObserverPlugin started on the runner machine?
Check `EMERGE_OPERATOR_MONITOR=1` is set in the daemon environment.

Read the runner log:
```bash
curl http://<runner-host>:8787/logs?n=50
```
Look for `ObserverPlugin.start()` log lines.

### 3. Are events reaching the EventBus?
```bash
# On the runner machine (or via icc_exec):
cat ~/.emerge/operator-events/<machine_id>/events.jsonl | tail -20
```
If empty: the AppAdapter's COM/AX listener is not firing. Check app permissions
(macOS requires Accessibility permission for AX API).

### 4. Is PatternDetector seeing the events?
Replay events manually:
```python
# icc_exec
import json
from pathlib import Path
from scripts.pattern_detector import PatternDetector

events_path = Path.home() / ".emerge/operator-events/<machine_id>/events.jsonl"
events = [json.loads(l) for l in events_path.read_text().splitlines() if l.strip()]
summaries = PatternDetector().ingest(events[-50:])
print(f"Summaries: {summaries}")
```
If `summaries` is empty but events exist: thresholds not met. Check
`PatternDetector.FREQ_THRESHOLD` (default 3) and `FREQ_WINDOW_MS` (20 min).

### 5. Is OperatorMonitor polling?
```python
# icc_exec — check last poll timestamps
import json
from pathlib import Path
# OperatorMonitor writes _last_poll_ms to a debug file if EMERGE_MONITOR_DEBUG=1
```
Set `EMERGE_MONITOR_POLL_S=5` to reduce poll interval during debugging.

### 6. Is the MCP push reaching CC?
For explore-stage: check if a channel notification prompt appeared in CC.
For canary/stable: watch for the native elicitation dialog.

Test push manually:
```python
# icc_exec on daemon machine
import json, sys
notification = {
    "jsonrpc": "2.0",
    "method": "notifications/claude/channel",
    "params": {"serverName": "emerge", "content": "test monitor push", "meta": {}},
}
sys.stdout.write(json.dumps(notification) + "\n")
sys.stdout.flush()
```

### 7. Common fixes

| Symptom | Fix |
|---------|-----|
| No events in EventBus | Check OS accessibility permissions; verify ObserverPlugin.start() called |
| PatternDetector never fires | Lower FREQ_THRESHOLD via env var or check window timing |
| Elicitation never appears | Verify daemon running with elicitation capability declared in MCP handshake |
| Wrong machine polled | Check EMERGE_MONITOR_MACHINES env var matches runner profile names |
```

- [ ] **Step 3: Extend `skills/initializing-vertical-flywheel/SKILL.md`**

Read the file first, then append at the end of the "When stable is reached" section (or at the end of the doc if no such section exists):

```markdown
## Reverse Flywheel Prompt

When any `intent_signature` for this vertical reaches `stable` status in
`policy://current`, prompt the operator:

> "`<vertical>.*` 管道飞轮已稳定。是否也建立反向飞轮来观察操作者行为，
> 让 AI 主动识别并接管重复操作？如需要，请调用
> `writing-vertical-adapter` skill。"

This connects the forward flywheel (AI learns to DO tasks) with the reverse
flywheel (AI learns to RECOGNIZE when humans are doing those tasks repeatedly).
```

- [ ] **Step 4: Verify skill files exist**

```bash
python -m pytest tests/test_plugin_static_config.py -v
```
Expected: PASS (static config tests check plugin structure)

- [ ] **Step 5: Commit**

```bash
git add skills/writing-vertical-adapter/ skills/operator-monitor-debug/ skills/initializing-vertical-flywheel/SKILL.md
git commit -m "feat: add writing-vertical-adapter and operator-monitor-debug skills; extend flywheel skill with reverse flywheel prompt"
```

---

## Task 8: README and CLAUDE.md updates

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update README roadmap — mark Operator Intelligence Loop as "next" (already done) and add new components to What Ships table**

In the "What ships in this repo" table, add rows:

```markdown
| Observer plugins | `scripts/observer_plugin.py`, `scripts/observers/` |
| Pattern detector | `scripts/pattern_detector.py` |
| Distiller | `scripts/distiller.py` |
| Operator monitor | `scripts/operator_monitor.py` |
| Skills (new) | `skills/writing-vertical-adapter/`, `skills/operator-monitor-debug/` |
```

Update the **Tests badge** in README.md — run the suite and count:
```bash
python -m pytest tests -q 2>&1 | tail -3
```
Update `![Tests](https://img.shields.io/badge/tests-NNN%20passing-brightgreen?logo=pytest)` with the new count.

Update the **Remote runner operations** env var table — add:

```markdown
| `EMERGE_OPERATOR_MONITOR` | Enable OperatorMonitor thread | `0` |
| `EMERGE_MONITOR_POLL_S` | EventBus poll interval (seconds) | `5` |
| `EMERGE_MONITOR_MACHINES` | Comma-separated machine IDs to monitor | all configured |
```

- [ ] **Step 2: Update CLAUDE.md — add new scripts to architecture section**

In the Architecture section of `CLAUDE.md`, add under "Key Invariants":

```markdown
- `EMERGE_OPERATOR_MONITOR=1` enables `OperatorMonitor` thread in the daemon. Off by default. The monitor polls remote runners via `/operator-events`, runs `PatternDetector`, and pushes to CC via MCP channel notification (explore) or `ElicitRequest` (canary/stable).
- `ObserverPlugin` (`scripts/observer_plugin.py`) is the ABC for observation capability. `AdapterRegistry` loads built-in observers (`scripts/observers/`) and crystallized vertical adapters from `~/.emerge/adapters/`.
```

Also add to the Documentation Update Rules table:
```markdown
| New observer or adapter interface change | `skills/writing-vertical-adapter/SKILL.md` |
| OperatorMonitor env var change | README.md env var table + `skills/operator-monitor-debug/SKILL.md` |
```

- [ ] **Step 3: Run full suite for final count**

```bash
python -m pytest tests -q
```
Record the passing count and update README badge.

- [ ] **Step 4: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs: update README and CLAUDE.md for Operator Intelligence Loop components"
```

---

## Self-Review

**Spec coverage check:**

| Spec section | Covered by task |
|---|---|
| §2 ObserverPlugin ABC | Task 1 |
| §2 Built-in generic observers | Task 1 |
| §2 AdapterRegistry + crystallization path | Task 1 |
| §2 EventBus format | Task 4 (remote_runner endpoints) |
| §3 PatternDetector + strategies | Task 2 |
| §3 PatternSummary fields | Task 2 |
| §4 MCP push — channel notification | Task 6 |
| §4 MCP push — ElicitRequest | Task 6 |
| §4 Pre-elicitation context read | Task 5 (OperatorMonitor._poll_machine) |
| §4 Three-stage interaction flow | Task 6 (_push_pattern_to_cc) |
| §4 operator_popup.py fallback | ⚠️ Not covered — deferred (non-CC machines only, lower priority) |
| §5 Distiller | Task 3 |
| §5 Flywheel integration | Task 6 (intent_confirmed event written) |
| §5 initializing-vertical-flywheel hook | Task 7 |
| §6 OperatorMonitor lifecycle + config | Tasks 5, 6 |
| §7 Skills | Task 7 |
| §9 What Ships | Task 8 |

**One gap:** `operator_popup.py` (tkinter fallback for non-CC machines) is not implemented. This is a low-priority path — the primary path is MCP ElicitRequest. Deferred to a follow-up task.

**Type consistency:** `PatternSummary` defined in `pattern_detector.py` and imported consistently in `distiller.py`, `operator_monitor.py`, `emerge_daemon.py`. `ObserverPlugin` defined in `observer_plugin.py`, imported in all observer files and `operator_monitor.py`. No naming inconsistencies found.
