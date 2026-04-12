---
name: operator-monitor-debug
description: Use when the operator monitoring pipeline appears broken: EventBus has no events, PatternDetector is not firing, elicitation dialog never appears, or OperatorMonitor is silent. Guides systematic diagnosis of the full pipeline.
---

# Debugging the Operator Monitor Pipeline

## Checklist

### 1. Is the remote runner running and reachable?

```bash
curl http://<runner-host>:8787/health
# Expected: {"ok": true, "uptime_s": N}
```

### 2. Is EMERGE_OPERATOR_MONITOR enabled?

Check that `EMERGE_OPERATOR_MONITOR=1` is set in the daemon environment.
The daemon does NOT start `OperatorMonitor` unless this var is set.

### 3. Are events reaching the EventBus?

```bash
# On the runner machine (or via icc_exec targeting that profile):
cat ~/.emerge/operator-events/<machine_id>/events.jsonl | tail -20
```

If the file is empty or missing: the `ObserverPlugin` listener is not firing events.
- Check that `ObserverPlugin.start()` was called (look for it in the runner log via `GET /logs?n=50`)
- On macOS, verify the process has Accessibility permission (`System Preferences → Privacy → Accessibility`)
- On Windows, check that the process has UIAutomation access and is not running in a low-integrity context

### 4. Is PatternDetector seeing the events?

Replay events manually via `icc_exec`:

```python
import json
from pathlib import Path
from scripts.pattern_detector import PatternDetector

events_path = Path.home() / ".emerge/operator-events/<machine_id>/events.jsonl"
events = [json.loads(l) for l in events_path.read_text().splitlines() if l.strip()]
summaries = PatternDetector().ingest(events[-50:])
print(f"Summaries: {summaries}")
```

If `summaries` is empty but events exist: thresholds not met yet.
- Frequency detector fires at ≥3 same-type events in a 20-minute window
- Check that `session_role` is `"operator"`, not `"monitor_sub"` — monitor_sub events are filtered

### 5. Is OperatorMonitor polling?

Set `EMERGE_MONITOR_POLL_S=5` to ensure polling is active.
Check `EMERGE_MONITOR_MACHINES` matches the profile names configured in the runner map.

### 6. Is the pattern alert reaching CC?

Pattern alerts are delivered via `watch_patterns.py` (Monitor tool, persistent).
When a pattern is detected, the daemon writes `pattern-alerts.json` to the state root.
The Monitor script polls this file, prints a formatted alert to stdout, and renames it
to `.processed.json`. CC streams stdout into the conversation automatically.

Check:
1. Is `watch_patterns.py` running as a persistent Monitor? (launched by `/emerge:cockpit` step 4)
2. Does `pattern-alerts.json` exist in the state root? If so, the daemon wrote it but the Monitor hasn't consumed it.
3. Check Monitor logs for the formatted alert output.

### 7. Common fixes

| Symptom | Fix |
|---------|-----|
| EventBus empty | Verify `ObserverPlugin.start()` called; check OS accessibility permissions |
| PatternDetector never fires | Lower `FREQ_THRESHOLD` or check event `session_role` field |
| Elicitation never appears | Verify daemon has elicitation capability in MCP handshake; check thread is non-main |
| Pattern alert not delivered | Verify `watch_patterns.py` Monitor is running; check `pattern-alerts.json` exists in state root |
| OperatorMonitor not starting | Confirm `EMERGE_OPERATOR_MONITOR=1` in daemon env, not runner env |
