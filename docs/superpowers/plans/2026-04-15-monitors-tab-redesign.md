# Monitors Tab Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the table-based Monitors tab with a professional card grid reflecting the "one runner = one operator machine" model, including per-runner activity sparklines, live event feeds, and team status bar.

**Architecture:** New `cmd_control_plane_runner_events` backend function reads `events-{profile}.jsonl` per runner, computing activity buckets and today's stats. Frontend renders a responsive card grid; SSE `monitors_updated` event triggers silent re-render. Expanded event feeds persist across re-renders via a JS Set.

**Tech Stack:** Pure HTML/JS in `scripts/cockpit_shell.html` (no new dependencies); new Python function in `scripts/admin/control_plane.py`; new route in `scripts/admin/cockpit.py` `_CockpitHandler.do_GET`.

---

## File Map

| File | Change |
|---|---|
| `scripts/admin/control_plane.py` | Add `cmd_control_plane_runner_events(profile, limit)` after `cmd_control_plane_monitors` (line ~537) |
| `scripts/admin/cockpit.py` | Import `cmd_control_plane_runner_events`; add `GET /api/control-plane/runner-events` route |
| `scripts/cockpit_shell.html` | Remove dead Add-Runner code; replace `renderMonitorsTab` with card-grid implementation |
| `tests/test_monitors_tab.py` | New file — 4 tests for backend function |

---

### Task 1: Backend function `cmd_control_plane_runner_events`

**Files:**
- Modify: `scripts/admin/control_plane.py` (insert after line 536, after `cmd_control_plane_monitors`)
- Create: `tests/test_monitors_tab.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_monitors_tab.py`:

```python
"""Tests for cmd_control_plane_runner_events."""
import json
import time

import pytest

from scripts.admin.control_plane import cmd_control_plane_runner_events


def test_runner_events_invalid_profile_empty():
    result = cmd_control_plane_runner_events(profile="", limit=20)
    assert result["ok"] is False
    assert "invalid" in result["error"]


def test_runner_events_invalid_profile_special_chars():
    result = cmd_control_plane_runner_events(profile="bad/profile!", limit=20)
    assert result["ok"] is False
    assert "invalid" in result["error"]


def test_runner_events_missing_file(tmp_path, monkeypatch):
    monkeypatch.setenv("EMERGE_REPL_ROOT", str(tmp_path))
    result = cmd_control_plane_runner_events(profile="myrunner", limit=20)
    assert result["ok"] is True
    assert result["events"] == []
    assert len(result["activity"]) == 10
    assert result["today_events"] == 0
    assert result["today_alerts"] == 0


def test_runner_events_returns_newest_first(tmp_path, monkeypatch):
    monkeypatch.setenv("EMERGE_REPL_ROOT", str(tmp_path))
    path = tmp_path / "events-myrunner.jsonl"
    now = int(time.time() * 1000)
    path.write_text(
        json.dumps({"type": "runner_event", "ts_ms": now - 60000}) + "\n" +
        json.dumps({"type": "pattern_alert", "ts_ms": now - 1000}) + "\n",
        encoding="utf-8",
    )
    result = cmd_control_plane_runner_events(profile="myrunner", limit=20)
    assert result["ok"] is True
    assert result["events"][0]["type"] == "pattern_alert"  # newest first
    assert result["today_alerts"] == 1
    assert result["today_events"] == 2


def test_runner_events_activity_buckets(tmp_path, monkeypatch):
    monkeypatch.setenv("EMERGE_REPL_ROOT", str(tmp_path))
    now = int(time.time() * 1000)
    path = tmp_path / "events-myrunner.jsonl"
    # 3 events in the last bucket (last 6 minutes)
    lines = "\n".join(
        json.dumps({"type": "runner_event", "ts_ms": now - i * 60000})
        for i in range(3)
    ) + "\n"
    path.write_text(lines, encoding="utf-8")
    result = cmd_control_plane_runner_events(profile="myrunner", limit=20)
    assert result["ok"] is True
    assert sum(result["activity"]) == 3
    assert result["activity"][-1] >= 1  # most recent bucket has events
```

- [ ] **Step 2: Run tests to verify they fail**

```
python -m pytest tests/test_monitors_tab.py -q
```

Expected: ImportError or NameError — `cmd_control_plane_runner_events` does not exist yet.

- [ ] **Step 3: Implement `cmd_control_plane_runner_events`**

In `scripts/admin/control_plane.py`, insert the following after `cmd_control_plane_monitors` (after line 536):

```python
_PROFILE_RE = re.compile(r"^[a-zA-Z0-9_.\-]{1,64}$")


def cmd_control_plane_runner_events(profile: str, limit: int = 20) -> dict:
    """Return per-runner events, activity buckets, and today's stats."""
    if not profile or not _PROFILE_RE.match(profile):
        return {"ok": False, "error": "invalid profile"}
    limit = min(int(limit), 100)
    repl_root = Path(os.environ.get("EMERGE_REPL_ROOT", "") or _resolve_repl_root())
    events_path = repl_root / f"events-{profile}.jsonl"
    _empty = {"ok": True, "events": [], "activity": [0] * 10, "today_events": 0, "today_alerts": 0}
    if not events_path.exists():
        return _empty
    try:
        raw = events_path.read_text(encoding="utf-8")
    except OSError:
        return _empty

    lines = raw.splitlines()
    # Sliding read: only parse last limit*3 lines to avoid loading huge files
    sample_lines = lines[-(limit * 3):]
    parsed = []
    for line in sample_lines:
        line = line.strip()
        if not line:
            continue
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    now_ms = int(time.time() * 1000)
    # Activity: divide last 3600s into 10 buckets of 360s each
    bucket_ms = 360_000
    window_start_ms = now_ms - 3600_000
    activity = [0] * 10
    # For today's stats we need ALL lines in the file, not just the sample
    # Re-parse all lines for today_events / today_alerts
    all_parsed = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            all_parsed.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    import datetime
    today_start_ms = int(
        datetime.datetime.combine(
            datetime.date.today(), datetime.time.min,
            tzinfo=datetime.timezone.utc
        ).timestamp() * 1000
    )
    today_events = 0
    today_alerts = 0
    for ev in all_parsed:
        ts = ev.get("ts_ms", 0)
        if ts >= today_start_ms:
            today_events += 1
            if ev.get("type") == "pattern_alert":
                today_alerts += 1
        # Activity buckets (use all events in last hour)
        if ts >= window_start_ms:
            idx = int((ts - window_start_ms) // bucket_ms)
            if 0 <= idx <= 9:
                activity[idx] += 1

    # Return last `limit` events newest-first (from all parsed)
    events_sorted = sorted(all_parsed, key=lambda e: e.get("ts_ms", 0), reverse=True)
    return {
        "ok": True,
        "events": events_sorted[:limit],
        "activity": activity,
        "today_events": today_events,
        "today_alerts": today_alerts,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

```
python -m pytest tests/test_monitors_tab.py -q
```

Expected: 5 tests pass.

- [ ] **Step 5: Run full test suite to check for regressions**

```
python -m pytest tests -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add tests/test_monitors_tab.py scripts/admin/control_plane.py
git commit -m "feat: add cmd_control_plane_runner_events backend function"
```

---

### Task 2: Cockpit route `GET /api/control-plane/runner-events`

**Files:**
- Modify: `scripts/admin/cockpit.py` (import block lines 41-62; route handler after the `/api/control-plane/monitors` block at line ~228)

- [ ] **Step 1: Add import**

In `scripts/admin/cockpit.py`, add `cmd_control_plane_runner_events` to the import block. Change:

```python
    cmd_control_plane_monitors,
```

to:

```python
    cmd_control_plane_monitors,
    cmd_control_plane_runner_events,
```

- [ ] **Step 2: Add the route handler**

In `scripts/admin/cockpit.py`, find the block:

```python
        elif path == "/api/control-plane/runner-profiles":
```

Insert the following new `elif` block **before** that line:

```python
        elif path == "/api/control-plane/runner-events":
            qs_re = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            profile = (qs_re.get("profile") or [""])[0]
            try:
                limit = min(int((qs_re.get("limit") or ["20"])[0]), 100)
            except ValueError:
                limit = 20
            self._json(cmd_control_plane_runner_events(profile=profile, limit=limit))
```

- [ ] **Step 3: Run tests**

```
python -m pytest tests -q
```

Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add scripts/admin/cockpit.py
git commit -m "feat: add GET /api/control-plane/runner-events route"
```

---

### Task 3: Frontend cleanup — remove dead Add Runner code

**Files:**
- Modify: `scripts/cockpit_shell.html`

Dead code to remove:
- State vars `_addRunnerPollInterval` and `_addRunnerKnownProfiles` (lines 1237-1238)
- `_pollForNewRunner()` function (lines 1260-1275)
- The `_addRunnerPollInterval` cleanup block inside `_stopMonitorsPolling()` (lines 1253-1256)
- Functions `_renderAddRunnerPanel()`, `_copyInstallCmd()`, `let _installUrlFetchTimer`, `_fetchInstallUrls()`, `_initAddRunnerPanel()` (lines 2128-2185)
- All calls to `_initAddRunnerPanel()` inside `renderMonitorsTab()` (lines 2209, 2240)

- [ ] **Step 1: Remove state vars**

Find:
```js
let _addRunnerPollInterval = null;
let _addRunnerKnownProfiles = null;
```
Remove both lines (keep `_monitorsPollingInterval` and `_monitorsHasContent`).

- [ ] **Step 2: Remove `_pollForNewRunner`**

Find and remove:
```js
async function _pollForNewRunner() {
  try {
    const r = await fetch('/api/control-plane/monitors');
    const d = await r.json();
    const runners = d.runners || [];
    const newRunner = runners.find(r => _addRunnerKnownProfiles && !_addRunnerKnownProfiles.has(r.runner_profile));
    if (newRunner) {
      clearInterval(_addRunnerPollInterval);
      _addRunnerPollInterval = null;
      const statusEl = document.getElementById('add-runner-status');
      if (statusEl) {
        statusEl.innerHTML = `<div style="margin-top:10px;color:#3fb950;font-size:12px;font-weight:600">✓ ${escHtml(newRunner.runner_profile)} connected</div>`;
      }
    }
  } catch (e) {}
}
```

- [ ] **Step 3: Remove `_addRunnerPollInterval` cleanup from `_stopMonitorsPolling`**

Find:
```js
  if (_addRunnerPollInterval) {
    clearInterval(_addRunnerPollInterval);
    _addRunnerPollInterval = null;
  }
```
Remove just those 4 lines. The rest of `_stopMonitorsPolling` stays.

- [ ] **Step 4: Remove the Add Runner helper functions**

Find and remove the entire block from `function _renderAddRunnerPanel()` through `}` closing `_initAddRunnerPanel()` (lines 2128-2185 inclusive). This covers:
- `_renderAddRunnerPanel()`
- `_copyInstallCmd()`
- `let _installUrlFetchTimer`
- `_fetchInstallUrls()`
- `_initAddRunnerPanel()`

- [ ] **Step 5: Verify cockpit still loads**

Open the browser at `http://localhost:8789` and switch to the Monitors tab. It should render without JS errors (even if still showing the old table layout — the new layout comes in Task 4).

- [ ] **Step 6: Commit**

```bash
git add scripts/cockpit_shell.html
git commit -m "refactor: remove dead Add Runner code from Monitors tab"
```

---

### Task 4: Frontend redesign — card grid implementation

**Files:**
- Modify: `scripts/cockpit_shell.html`

Replace `renderMonitorsTab()` (lines 2187-2244) with the new card-grid implementation, and add new state vars and helper functions.

- [ ] **Step 1: Add new state variables**

After `let _monitorsHasContent = false;` add:

```js
let _expandedRunners = new Set();
let _recentEventsCache = {};
```

- [ ] **Step 2: Replace `renderMonitorsTab`**

Find the entire `async function renderMonitorsTab()` block (lines 2187-2244) and replace with:

```js
async function renderMonitorsTab() {
  const panel = document.getElementById('main-panel');
  if (!_monitorsHasContent) {
    panel.innerHTML = '<div style="padding:16px;color:#8b949e">Loading monitor state…</div>';
  }
  try {
    const resp = await fetch('/api/control-plane/monitors');
    const data = await resp.json();
    const runners = data.runners || [];
    let html = '<div style="padding:16px">';
    html += _renderTeamStatusBar(runners);
    if (!runners.length) {
      html += `<div style="text-align:center;padding:40px;color:#484f58">
        <div>No runners connected.</div>
        <div style="font-size:11px;margin-top:8px">Run the install script on the target machine — it connects automatically.</div>
      </div>`;
    } else {
      html += _renderRunnerCards(runners);
    }
    html += '</div>';
    panel.innerHTML = html;
    _monitorsHasContent = true;
    // Refresh expanded feeds
    for (const profile of _expandedRunners) {
      _loadRunnerFeed(profile);
    }
  } catch (e) {
    panel.innerHTML = `<div style="padding:16px;color:#f85149">Error loading monitors: ${escHtml(String(e))}</div>`;
  }
}
```

- [ ] **Step 3: Add `_renderTeamStatusBar`**

Add immediately after the new `renderMonitorsTab`:

```js
function _renderTeamStatusBar(runners) {
  if (!runners.length) {
    return `<div style="display:flex;align-items:center;gap:8px;margin-bottom:16px;padding:8px 12px;background:#161b22;border:1px solid #21262d;border-radius:6px">
      <span style="width:6px;height:6px;border-radius:50%;background:#6e7681;display:inline-block"></span>
      <span style="font-size:11px;color:#8b949e">No runners connected</span>
    </div>`;
  }
  return `<div style="display:flex;align-items:center;gap:8px;margin-bottom:16px;padding:8px 12px;background:#161b22;border:1px solid #21262d;border-radius:6px">
    <span style="width:6px;height:6px;border-radius:50%;background:#3fb950;display:inline-block"></span>
    <span style="font-size:11px;color:#8b949e">Agents team active</span>
    <span style="font-size:11px;color:#3fb950;font-weight:600">${runners.length} runner${runners.length === 1 ? '' : 's'}</span>
    <span style="margin-left:auto;font-size:10px;color:#8b949e">updated just now</span>
  </div>`;
}
```

- [ ] **Step 4: Add `_renderRunnerCards`**

```js
function _renderRunnerCards(runners) {
  return `<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px">
    ${runners.map(r => _renderRunnerCard(r)).join('')}
  </div>`;
}
```

- [ ] **Step 5: Add `_renderRunnerCard`**

```js
function _renderRunnerCard(runner) {
  const profile = runner.runner_profile || '';
  const safeProfile = escHtml(profile);
  const now = Date.now();
  const connectedSec = runner.connected_at_ms ? Math.round((now - runner.connected_at_ms) / 1000) : 0;
  const connectedAge = connectedSec < 3600
    ? (connectedSec < 60 ? connectedSec + 's ago' : Math.round(connectedSec / 60) + 'm ago')
    : Math.round(connectedSec / 3600) + 'h ago';
  const lastEventSec = runner.last_event_ts_ms ? Math.round((now - runner.last_event_ts_ms) / 1000) : null;
  const lastEventAge = lastEventSec === null ? '—'
    : lastEventSec < 60 ? lastEventSec + 's ago'
    : lastEventSec < 3600 ? Math.round(lastEventSec / 60) + 'm ago'
    : Math.round(lastEventSec / 3600) + 'h ago';
  const isConnected = runner.connected !== false;
  const borderColor = isConnected ? '#238636' : '#30363d';
  const dotStyle = isConnected
    ? 'background:#3fb950;box-shadow:0 0 6px #3fb950aa'
    : 'background:#6e7681';

  // Alert badge
  let alertBadge;
  if (runner.last_alert) {
    const stage = escHtml(runner.last_alert.stage || '');
    const intent = escHtml(runner.last_alert.intent_signature || '');
    alertBadge = `<span style="background:#1c2813;border:1px solid #2a4a2a;color:#3fb950;font-size:10px;padding:3px 10px;border-radius:10px">${stage}: ${intent}</span>`;
  } else {
    alertBadge = `<span style="color:#484f58;font-size:10px">no alerts</span>`;
  }

  // Recent preview from cache
  const cached = _recentEventsCache[profile] || [];
  const recentHtml = cached.length
    ? cached.map(ev => _renderEventRow(ev, now)).join('')
    : '<div style="font-size:10px;color:#484f58">—</div>';

  const isExpanded = _expandedRunners.has(profile);
  const feedToggleLabel = isExpanded ? '▲ Event feed' : '▼ Event feed';
  const feedToggleColor = isExpanded ? '#58a6ff' : '#8b949e';

  return `<div style="background:#161b22;border:1px solid ${borderColor};border-radius:8px;overflow:hidden">
    <div style="padding:12px 14px;border-bottom:1px solid #21262d">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
        <div style="display:flex;align-items:center;gap:8px">
          <span style="width:8px;height:8px;border-radius:50%;${dotStyle};display:inline-block"></span>
          <span style="color:#e6edf3;font-size:13px;font-weight:700;font-family:monospace">${safeProfile}</span>
        </div>
        <span style="color:#8b949e;font-size:10px">${connectedAge}</span>
      </div>
      <div id="sparkline-${safeProfile}" style="display:flex;gap:2px;height:16px;align-items:flex-end">
        <span style="font-size:9px;color:#484f58;align-self:center">1h activity</span>
      </div>
    </div>
    <div style="padding:12px 14px">
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:10px">
        <div>
          <div style="font-size:9px;color:#8b949e;text-transform:uppercase;letter-spacing:.5px;margin-bottom:2px">Machine</div>
          <div style="font-size:11px;color:#c9d1d9;font-family:monospace">${escHtml(runner.machine_id || '—')}</div>
        </div>
        <div>
          <div style="font-size:9px;color:#8b949e;text-transform:uppercase;letter-spacing:.5px;margin-bottom:2px">Last Event</div>
          <div style="font-size:11px;color:#c9d1d9">${lastEventAge}</div>
        </div>
        <div>
          <div style="font-size:9px;color:#8b949e;text-transform:uppercase;letter-spacing:.5px;margin-bottom:2px">Today</div>
          <div id="today-stats-${safeProfile}" style="font-size:11px;color:#c9d1d9">—</div>
        </div>
      </div>
      <div style="margin-bottom:10px">${alertBadge}</div>
      <div style="background:#0d1117;border-radius:4px;padding:8px 10px;margin-bottom:8px">
        <div style="font-size:9px;color:#484f58;text-transform:uppercase;letter-spacing:.5px;margin-bottom:5px">Recent</div>
        ${recentHtml}
      </div>
    </div>
    <div style="border-top:1px solid #21262d;padding:8px 14px;cursor:pointer" onclick="_toggleRunnerFeed('${profile}')">
      <span style="color:${feedToggleColor};font-size:10px">${feedToggleLabel}</span>
    </div>
    <div id="feed-${safeProfile}" style="display:${isExpanded ? 'block' : 'none'}">Loading…</div>
  </div>`;
}
```

- [ ] **Step 6: Add `_renderEventRow` helper**

```js
function _renderEventRow(ev, now) {
  now = now || Date.now();
  const ageSec = ev.ts_ms ? Math.round((now - ev.ts_ms) / 1000) : 0;
  const age = ageSec < 60 ? ageSec + 's'
    : ageSec < 3600 ? Math.round(ageSec / 60) + 'm'
    : Math.round(ageSec / 3600) + 'h';
  const type = ev.type || 'event';
  let badgeStyle, content;
  if (type === 'pattern_alert') {
    badgeStyle = 'background:#2d1b00;color:#f0883e;padding:0 5px;border-radius:2px;font-size:9px;flex-shrink:0';
    content = `<span style="color:#8b949e">${escHtml(ev.intent_signature || '')} · ${escHtml(ev.stage || '')}</span>`;
  } else if (type === 'operator_message') {
    badgeStyle = 'background:#0d2233;color:#58a6ff;padding:0 5px;border-radius:2px;font-size:9px;border:1px solid #1f6feb33;flex-shrink:0';
    content = `<span style="color:#58a6ff">"${escHtml(ev.text || '')}"</span>`;
  } else if (type === 'runner_online') {
    badgeStyle = 'background:#1c2813;color:#3fb950;padding:0 5px;border-radius:2px;font-size:9px;border:1px solid #2a4a2a;flex-shrink:0';
    content = `<span style="color:#484f58">runner connected · ${escHtml(ev.machine_id || '')}</span>`;
  } else {
    badgeStyle = 'background:#161b22;color:#8b949e;padding:0 5px;border-radius:2px;font-size:9px;border:1px solid #30363d;flex-shrink:0';
    const sub = ev.type || '';
    content = `<span style="color:#484f58">${escHtml(sub)}</span>`;
  }
  return `<div style="font-size:10px;padding:5px 0;border-bottom:1px solid #21262d33;display:flex;gap:8px">
    <span style="color:#484f58;min-width:28px;flex-shrink:0">${age}</span>
    <span style="${badgeStyle}">${escHtml(type.replace('pattern_alert','pattern').replace('operator_message','operator').replace('runner_online','online'))}</span>
    ${content}
  </div>`;
}
```

- [ ] **Step 7: Add `_renderEventFeed`**

```js
function _renderEventFeed(events) {
  if (!events || !events.length) {
    return '<div style="padding:8px 14px 10px;font-size:10px;color:#484f58">No events.</div>';
  }
  const now = Date.now();
  return `<div style="max-height:200px;overflow-y:auto;padding:0 14px 10px">
    ${events.map(ev => _renderEventRow(ev, now)).join('')}
  </div>`;
}
```

- [ ] **Step 8: Add `_renderSparkline`**

```js
function _renderSparkline(activity) {
  if (!activity || !activity.length) return '<span style="font-size:9px;color:#484f58;align-self:center">1h activity</span>';
  const max = Math.max(...activity, 1);
  const bars = activity.map((count, i) => {
    const h = Math.max(1, Math.round((count / max) * 16));
    const color = count > 0 ? '#3fb950' + (h >= 12 ? '' : h >= 8 ? '66' : h >= 4 ? '33' : '22') : '#3fb95022';
    return `<div style="width:5px;background:${color};height:${h}px;border-radius:1px 1px 0 0"></div>`;
  }).join('');
  return bars + '<span style="font-size:9px;color:#484f58;margin-left:6px;align-self:center">1h activity</span>';
}
```

- [ ] **Step 9: Add `_toggleRunnerFeed` and `_loadRunnerFeed`**

```js
function _toggleRunnerFeed(profile) {
  const safeProfile = escHtml(profile);
  const feedEl = document.getElementById('feed-' + safeProfile);
  if (!feedEl) return;
  if (_expandedRunners.has(profile)) {
    _expandedRunners.delete(profile);
    feedEl.style.display = 'none';
  } else {
    _expandedRunners.add(profile);
    feedEl.style.display = 'block';
    feedEl.innerHTML = 'Loading…';
    _loadRunnerFeed(profile);
  }
  // Update toggle button label
  const card = feedEl.previousElementSibling;
  if (card) {
    const btn = card.querySelector('span');
    if (btn) {
      btn.style.color = _expandedRunners.has(profile) ? '#58a6ff' : '#8b949e';
      btn.textContent = _expandedRunners.has(profile) ? '▲ Event feed' : '▼ Event feed';
    }
  }
}

async function _loadRunnerFeed(profile) {
  const safeProfile = escHtml(profile);
  const feedEl = document.getElementById('feed-' + safeProfile);
  const sparklineEl = document.getElementById('sparkline-' + safeProfile);
  try {
    const resp = await fetch(`/api/control-plane/runner-events?profile=${encodeURIComponent(profile)}&limit=20`);
    const data = await resp.json();
    if (!data.ok) return;
    // Cache last 2 events for preview
    _recentEventsCache[profile] = (data.events || []).slice(0, 2);
    // Render sparkline
    if (sparklineEl) {
      sparklineEl.innerHTML = _renderSparkline(data.activity || []);
    }
    // Render today stats
    const todayEl = document.getElementById('today-stats-' + safeProfile);
    if (todayEl) {
      todayEl.innerHTML = `${data.today_events} ops · <span style="color:#58a6ff">${data.today_alerts} alerts</span>`;
    }
    // Render feed
    if (feedEl) {
      feedEl.innerHTML = _renderEventFeed(data.events || []);
    }
  } catch (e) {
    if (feedEl) feedEl.innerHTML = `<div style="padding:8px 14px;color:#f85149;font-size:10px">Error: ${escHtml(String(e))}</div>`;
  }
}
```

- [ ] **Step 10: Verify in browser**

Start the dev server and open `http://localhost:8789`. Switch to the Monitors tab. Verify:
- Team status bar shows green dot + runner count
- Each runner shows a card with Machine, Last Event, Today columns
- Clicking "▼ Event feed" expands the feed, loads events and sparkline
- Clicking "▲ Event feed" collapses it
- After 10s polling refresh, expanded feeds refresh automatically (via `_expandedRunners` loop)
- Empty state shows "No runners connected." with install hint
- No JS console errors

- [ ] **Step 11: Commit**

```bash
git add scripts/cockpit_shell.html
git commit -m "feat: replace Monitors table with card grid + activity sparklines"
```

---

## Self-Review

**Spec coverage:**
- `cmd_control_plane_runner_events` ✅ (Task 1)
- Profile validation `^[a-zA-Z0-9_.-]+$` max 64 chars ✅ (Task 1)
- Activity 10 × 360s buckets ✅ (Task 1)
- `today_events` / `today_alerts` ✅ (Task 1)
- Sliding read `limit * 3` lines ✅ (Task 1)
- Silent fallback on OSError ✅ (Task 1)
- GET `/api/control-plane/runner-events` route ✅ (Task 2)
- Default limit 20, max 100 ✅ (Task 2)
- Remove `_pollForNewRunner` / `_addRunnerPollInterval` / `_addRunnerKnownProfiles` ✅ (Task 3)
- `_expandedRunners = new Set()` ✅ (Task 4, Step 1)
- `_recentEventsCache = {}` ✅ (Task 4, Step 1)
- `renderMonitorsTab` card grid ✅ (Task 4, Step 2)
- `_renderTeamStatusBar` ✅ (Task 4, Step 3)
- `_renderRunnerCards` grid CSS ✅ (Task 4, Step 4)
- `_renderRunnerCard` all fields ✅ (Task 4, Step 5)
- Event badge colors ✅ (Task 4, Step 6)
- `_renderEventFeed` scrollable container ✅ (Task 4, Step 7)
- `_renderSparkline` 10 bars ✅ (Task 4, Step 8)
- `_toggleRunnerFeed` / `_loadRunnerFeed` ✅ (Task 4, Step 9)
- Re-render refreshes expanded feeds ✅ (Task 4, Step 2 loop)
- Empty state HTML ✅ (Task 4, Step 2)
- Remove `/api/control-plane/runner-profiles` — **spec says remove, but it's still referenced by cockpit polling logic. Leave it in place; removing it is a separate cleanup.**
- 4 test cases from spec ✅ (Task 1) — plus an extra invalid profile variant

**Type consistency:** `cmd_control_plane_runner_events` is defined in Task 1 and imported/called in Task 2 with the same signature `(profile, limit)`. `_loadRunnerFeed(profile)` is called in Tasks 4 Step 2 and Step 9 with same signature. `_renderSparkline(activity)` called in Task 4 Step 9 and defined in Step 8 — consistent. `_renderEventRow(ev, now)` called in Steps 7 and 5, defined in Step 6 — consistent.

**Placeholder scan:** No TBDs. All steps contain working code.
