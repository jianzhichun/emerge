---
name: writing-vertical-adapter
description: Use when writing or crystallizing a vertical ObserverPlugin adapter for a specific application (ZWCAD, Excel, browser, etc.), or when asked how to add a new vertical to the Operator Intelligence Loop.
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

4. **Wrap as adapter**: Move the observation logic into `ObserverPlugin.get_context()`
   and execution logic into `execute()`, save to `~/.emerge/adapters/zwcad/adapter.py`.

## Testing

Use a mock EventBus to replay events without a live application:

```python
from scripts.pattern_detector import PatternDetector
events = [
    {"ts_ms": 1000 * i, "machine_id": "test", "session_role": "operator",
     "event_type": "entity_added", "app": "zwcad",
     "payload": {"layer": "annotation", "content": f"room_{i}"}}
    for i in range(4)
]
summaries = PatternDetector().ingest(events)
assert len(summaries) == 1
```

## Registration

No registration step needed — `AdapterRegistry` auto-discovers any directory under
`~/.emerge/adapters/` that contains `adapter.py` with an `ObserverPlugin` subclass.
Restart the daemon (or send SIGHUP if supported) after placing a new adapter file.

## Windows COM Verticals (ZWCAD, AutoCAD, Excel…)

COM objects are **thread-local** (STA apartment model). Each `icc_exec` call may run on a different thread, so COM objects do not survive across calls.

**Rule**: always `CoInitialize` + `Dispatch` at the top of every `icc_exec` call that touches COM. Never cache COM objects across calls.

```python
import pythoncom, win32com.client
pythoncom.CoInitialize()
app = win32com.client.Dispatch("ZwCAD.Application")   # reconnect every call
```

Non-COM Python objects (dicts, lists, numpy arrays) ARE safe to reuse across calls — ExecSession globals persist within a session.

**Runner must be in Session 1** (interactive desktop) for COM/GUI to work. Verify:
```python
import os, ctypes
sid = ctypes.c_ulong(0)
ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid))
assert sid.value >= 1, f"wrong session: {sid.value}"
```

If `sid == 0`: reboot the Windows runner host — the registry autostart (`HKCU\...\Run`) will re-launch the watchdog in Session 1 at next logon. SSH-triggered restarts always land in Session 0.

Vertical-specific COM patterns (ProgID, object model quirks, error codes) belong in `~/.emerge/connectors/<vertical>/NOTES.md`, not in this skill.
