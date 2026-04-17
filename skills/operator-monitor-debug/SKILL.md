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

### 2. Is OperatorMonitor running?

`OperatorMonitor` auto-starts when a runner is configured (`runner-map.json` has
at least one entry) **OR** when `EMERGE_OPERATOR_MONITOR=1` is set in the daemon
environment. Check which condition applies:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" runner-status --pretty
# If runners are listed → OperatorMonitor should have auto-started.
# If no runners are listed and EMERGE_OPERATOR_MONITOR=1 is absent → it won't start.
```

### 3. Are events reaching the EventBus?

```bash
# On the runner machine (or via icc_exec targeting that profile):
cat ~/.emerge/operator-events/<machine_id>/events.jsonl | tail -20
```

If the file is empty or missing: the event producer is not firing events.
- Check that the relevant producer path is active (`POST /operator-event`, pipeline `start()` hook with `event_bus.emit_event`, or `_write_operator_event` from `icc_exec`)
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

Pattern alerts are delivered via `watch_emerge.py --runner-profile <profile>` (Monitor
tool, persistent). When a pattern fires, `DaemonHTTPServer._on_runner_event` writes a
`pattern_alert` entry directly to `events-{profile}.jsonl`. The Monitor script tails
this file and prints formatted alerts to stdout; CC streams stdout into the conversation.

Check:
1. Is `watch_emerge.py --runner-profile <profile>` running as a persistent Monitor?
   (launched by `/emerge:cockpit` step 4 via `watch_emerge.py --runner-profile`)
2. Does `~/.emerge/state/events/events-{profile}.jsonl` contain recent `pattern_alert` entries?
   ```bash
   grep '"type": "pattern_alert"' ~/.emerge/state/events/events-<profile>.jsonl | tail -5
   ```
3. If entries exist but CC didn't see them: the Monitor may have stopped — restart it.

### 7. Common fixes

| Symptom | Fix |
|---------|-----|
| EventBus empty | Verify event producer path (`/operator-event`, pipeline `start()`, or `icc_exec`) and OS accessibility permissions |
| PatternDetector never fires | Lower `FREQ_THRESHOLD` or check event `session_role` field |
| Elicitation never appears | Verify daemon has elicitation capability in MCP handshake; check thread is non-main |
| Pattern alert not delivered | Verify `watch_emerge.py --runner-profile <p>` Monitor is running; check `events-{profile}.jsonl` for `pattern_alert` entries |
| OperatorMonitor not starting | Confirm runner is configured (`runner-status --pretty`) OR `EMERGE_OPERATOR_MONITOR=1` in daemon env |
