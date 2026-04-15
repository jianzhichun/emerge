# Monitors Tab Redesign — Implementation Spec

**Goal:** Replace the current table-based Monitors tab with a professional card grid that reflects the real "one runner = one operator machine" model, including per-runner activity sparklines, live event feeds, and team status.

**Architecture:** New backend endpoint reads `events-{profile}.jsonl` per runner, computing activity buckets and today's stats. Frontend renders responsive card grid; SSE `monitors_updated` event triggers silent re-render (no loading flash). Expanded event feeds persist across re-renders via a JS Set.

**Tech Stack:** Pure HTML/JS in `cockpit_shell.html` (no new dependencies); new Python function in `scripts/admin/control_plane.py`; new route in `scripts/admin/cockpit.py` `_CockpitHandler`.

---

## Backend

### New function: `cmd_control_plane_runner_events(profile, limit)`

File: `scripts/admin/control_plane.py`

Reads `~/.emerge/repl/events-{profile}.jsonl` and returns:

```python
{
  "ok": True,
  "events": [...],        # last `limit` events, newest first
  "activity": [3, 6, 10, 8, 14, 16, 11, 7, 5, 3],  # 10 buckets × 6 min = last hour
  "today_events": 12,     # total events from today (UTC midnight)
  "today_alerts": 3,      # pattern_alert events from today
}
```

- `profile` must match `^[a-zA-Z0-9_.-]+$` (max 64 chars) — return `{"ok": False, "error": "invalid profile"}` otherwise
- If file does not exist, return `{"ok": True, "events": [], "activity": [0]*10, "today_events": 0, "today_alerts": 0}`
- Activity buckets: divide last 3600s into 10 equal 360s windows; count events per window (by `ts_ms`). Index 0 = oldest, index 9 = most recent.
- Read last `limit * 3` lines (sliding read to avoid loading huge files) — parse JSON, skip malformed lines
- `today_events` = events where `ts_ms >= UTC midnight today`
- `today_alerts` = above filtered to `type == "pattern_alert"`
- Error on OSError → return `{"ok": True, "events": [], ...}` (silent fallback)

### New route in `_CockpitHandler.do_GET`

File: `scripts/admin/cockpit.py`

```
GET /api/control-plane/runner-events?profile=<name>&limit=20
```

Parse `profile` and `limit` from query string; call `cmd_control_plane_runner_events`; return JSON. Default limit: 20, max: 100.

---

## Frontend

### State variables added

```js
let _expandedRunners = new Set();  // profiles currently showing full event feed
```

### Functions

**`renderMonitorsTab()`** — replace current implementation:
1. Skip "Loading…" flash if `_monitorsHasContent` (existing behavior)
2. Fetch `/api/control-plane/monitors`
3. Render team status bar + card grid via `_renderRunnerCards(runners)`
4. Set `_monitorsHasContent = true`
5. For each expanded profile in `_expandedRunners`: fetch events and inject into card

**`_renderTeamStatusBar(runners)`** — returns HTML string:
- Green dot + "Agents team active" + runner count + "updated just now"
- If no runners: gray dot + "No runners connected"

**`_renderRunnerCards(runners)`** — returns HTML string:
- `display:grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap:16px`
- Maps each runner to `_renderRunnerCard(runner)`

**`_renderRunnerCard(runner)`** — returns HTML string for one card:
- Header: status dot (glowing green if connected) + profile name (monospace, bold) + connected-time (right-aligned)
- Sparkline placeholder: `<div id="sparkline-{profile}">…</div>` — filled async
- Body: 3-col grid: Machine | Last Event | Today (events · alerts count)
- Alert badge: last alert stage+intent or "no alerts" in gray
- Recent preview: last 2 events from `_recentEventsCache[profile]` (if cached), else "—"
- Footer: expand/collapse button `onclick="_toggleRunnerFeed('{profile}')"`
- Event feed div: `<div id="feed-{profile}" style="display:none">Loading…</div>`

**`_toggleRunnerFeed(profile)`**:
- If in `_expandedRunners`: remove, hide `#feed-{profile}`
- Else: add, show div, call `_loadRunnerFeed(profile)`

**`_loadRunnerFeed(profile)`**:
- `GET /api/control-plane/runner-events?profile={profile}&limit=20`
- Render into `#feed-{profile}` using `_renderEventFeed(events)`
- Render sparkline into `#sparkline-{profile}` using `_renderSparkline(activity)`
- Cache events in `_recentEventsCache[profile]` (last 2 for preview)

**`_renderEventFeed(events)`** — returns HTML string:
- Scrollable container (`max-height: 200px; overflow-y: auto`)
- Each event row: `[age] [type badge] [content]`
- Type → badge color:
  - `pattern_alert` → orange (`#f0883e` bg `#2d1b00`)
  - `operator_message` → blue (`#58a6ff` bg `#0d2233`)
  - `runner_online` → green (`#3fb950` bg `#1c2813`)
  - everything else → gray (`#8b949e` bg `#161b22`)
- Content: for `pattern_alert` show `intent_signature · stage`; for `operator_message` show text in quotes; for `runner_event` show any `type` sub-field or empty

**`_renderSparkline(activity)`** — returns HTML string:
- 10 bars, `height = (count / max) * 16px`, min height 1px
- Color: if bar contains a `pattern_alert` bucket → red tint (`#f8514955`), else green tint proportional to height
- Label: "1h activity" in gray

**`_recentEventsCache`** — `{}` object (profile → last 2 events), populated by `_loadRunnerFeed`, used by `_renderRunnerCard` on re-render to avoid re-fetching for preview.

### Re-render behavior on `monitors_updated` SSE

1. `renderMonitorsTab()` called
2. Cards re-render with fresh runner data
3. For each profile in `_expandedRunners`: `_loadRunnerFeed(profile)` called again to refresh event feed + sparkline

### Empty state

When `runners.length === 0`:
```html
<div style="text-align:center; padding:40px; color:#484f58">
  <div>No runners connected.</div>
  <div style="font-size:11px; margin-top:8px">
    Run the install script on the target machine — it connects automatically.
  </div>
</div>
```

No "Add Runner" panel. The install URL is generated via CLI:
`python3 scripts/repl_admin.py runner-install-url --pretty`

---

## What Does NOT Change

- `_startMonitorsPolling` / `_stopMonitorsPolling` (10s interval, unchanged)
- `_monitorsHasContent` / `_monitorsHasContent = false` on tab leave (unchanged)
- `_pollForNewRunner` / `_addRunnerPollInterval` — **remove** (no longer needed, runners self-register)
- `_addRunnerKnownProfiles` — **remove**
- SSE `monitors_updated` handler (unchanged)
- `/api/control-plane/monitors` endpoint (unchanged)
- `/api/control-plane/runner-profiles` endpoint — **remove** (no profile selector anymore)

---

## Test

File: `tests/test_runner_self_install.py` or new `tests/test_monitors_tab.py`

```python
def test_runner_events_empty_profile():
    result = cmd_control_plane_runner_events(profile="", limit=20)
    assert result["ok"] is False
    assert "invalid" in result["error"]

def test_runner_events_missing_file(tmp_path, monkeypatch):
    monkeypatch.setenv("EMERGE_REPL_ROOT", str(tmp_path))
    result = cmd_control_plane_runner_events(profile="myrunner", limit=20)
    assert result["ok"] is True
    assert result["events"] == []
    assert len(result["activity"]) == 10
    assert result["today_events"] == 0

def test_runner_events_returns_newest_first(tmp_path, monkeypatch):
    import json, time
    monkeypatch.setenv("EMERGE_REPL_ROOT", str(tmp_path))
    path = tmp_path / "events-myrunner.jsonl"
    now = int(time.time() * 1000)
    path.write_text(
        json.dumps({"type": "runner_event", "ts_ms": now - 60000}) + "\n" +
        json.dumps({"type": "pattern_alert", "ts_ms": now - 1000}) + "\n",
        encoding="utf-8"
    )
    result = cmd_control_plane_runner_events(profile="myrunner", limit=20)
    assert result["ok"] is True
    assert result["events"][0]["type"] == "pattern_alert"  # newest first
    assert result["today_alerts"] == 1

def test_runner_events_activity_buckets(tmp_path, monkeypatch):
    import json, time
    monkeypatch.setenv("EMERGE_REPL_ROOT", str(tmp_path))
    now = int(time.time() * 1000)
    path = tmp_path / "events-myrunner.jsonl"
    # Write 3 events in the last bucket (last 6 minutes)
    lines = "\n".join(
        json.dumps({"type": "runner_event", "ts_ms": now - i * 60000})
        for i in range(3)
    ) + "\n"
    path.write_text(lines, encoding="utf-8")
    result = cmd_control_plane_runner_events(profile="myrunner", limit=20)
    assert sum(result["activity"]) == 3
    assert result["activity"][-1] >= 1  # most recent bucket has events
```
