# Cockpit Svelte Rewrite — Design Spec

**Goal:** Replace the single 3200-line `scripts/cockpit_shell.html` with a Svelte + Vite + TypeScript app that lives in `scripts/admin/cockpit/`, while keeping all existing API contracts and the Python daemon serving mechanism unchanged.

**Architecture:** Vite builds the Svelte app to `scripts/admin/cockpit/dist/`. The Python `_CockpitHandler` is updated to serve `dist/index.html` at `GET /` and `dist/assets/*` for static assets. API routes are unchanged. Development uses Vite's dev server with an `/api` proxy to the daemon on port 8789.

**Tech Stack:** Svelte 5, TypeScript, Vite 6, Svelte scoped styles + CSS design tokens. No CSS framework.

---

## Scope

**In scope:** Full feature parity with the current `cockpit_shell.html` across all tabs:
- Overview (pipeline cards, queue, goal management)
- Monitors (runner card grid, sparklines, event feeds)
- Audit (exec/pipeline/span timeline)
- Session (session state, hook state, export, reset)
- State (delta/risk virtual list)
- Connector tabs (dynamic, per-connector pipelines + panels)
- Settings modal (thresholds)
- SSE live updates
- URL-based tab routing (popstate)

**Out of scope:** New features. This is a pure rewrite — no new functionality.

---

## Directory Structure

```
scripts/admin/cockpit/
  src/
    App.svelte                    # top-level layout, tab router
    components/
      shared/
        TabBar.svelte             # tab strip with connector tabs
        StatusDot.svelte          # green/gray dot indicator
        Badge.svelte              # event type badge (pattern/operator/online/event)
        SettingsModal.svelte      # thresholds settings modal
        GoalBar.svelte            # goal submission bar + history dropdown
        ThresholdsBar.svelte      # thresholds display + edit button
      overview/
        OverviewTab.svelte        # pipeline groups + queue panel
        PipelineCard.svelte       # single pipeline card (status, metrics, actions)
        PipelineGroup.svelte      # grouped pipeline cards by connector
        QueuePanel.svelte         # queued actions list
      monitors/
        MonitorsTab.svelte        # card grid + team status bar
        RunnerCard.svelte         # single runner card
        Sparkline.svelte          # 10-bucket activity bar chart
        EventFeed.svelte          # expandable event list
      audit/
        AuditTab.svelte           # timeline of exec/pipeline/span events
      session/
        SessionTab.svelte         # session state + hook state + export/reset
      state/
        StateTab.svelte           # virtual scrolling delta/risk list
      connector/
        ConnectorTab.svelte       # dynamic per-connector tab with panel selector
    stores/
      policy.ts                   # pipelines, queue, thresholds — polling + SSE
      monitors.ts                 # runners, expanded set, events cache
      session.ts                  # session, hook state
      goal.ts                     # active goal, history
      ui.ts                       # currentTab, connectorPanelByTab, sseConnected
    lib/
      api.ts                      # all fetch calls with TypeScript return types
      sse.ts                      # EventSource wrapper as Svelte readable store
      format.ts                   # escHtml, formatAge, renderMarkdown utilities
      router.ts                   # URL tab routing (pushState / popstate)
    styles/
      tokens.css                  # CSS custom properties: colors, spacing, radii
      global.css                  # resets, body, .tab, .modal-overlay, scrollbar
    main.ts                       # Svelte mount point
  vite.config.ts
  tsconfig.json
  package.json
  dist/                           # build output — served by Python daemon
```

---

## Component Responsibilities

### `App.svelte`
- Reads `$ui.currentTab` from store
- Conditionally renders the active tab component
- Mounts `SettingsModal`, `GoalBar`, `ThresholdsBar` (always present)
- Initializes SSE on mount, starts polling

### `stores/ui.ts`
```typescript
export const currentTab = writable<string>('overview');
export const sseConnected = writable<boolean>(false);
export const connectorPanelByTab = writable<Record<string, string>>({});
```

### `stores/policy.ts`
- Wraps `/api/policy`, `/api/assets` responses
- Exposes: `pipelines`, `connectors`, `thresholds`, `queue`, `allPipelines`
- `refresh()` called on SSE update and 5s poll

### `stores/monitors.ts`
- Wraps `/api/control-plane/monitors` + `/api/control-plane/runner-events`
- Exposes: `runners`, `expandedRunners` (Set), `recentEventsCache`
- `refreshFeed(profile)` called on SSE `monitors_updated`

### `lib/api.ts`
Single file with typed fetch wrappers for all 25+ endpoints. Example:
```typescript
export async function fetchPolicy(): Promise<PolicyResponse> { ... }
export async function fetchRunnerEvents(profile: string, limit = 20): Promise<RunnerEventsResponse> { ... }
export async function submitGoal(text: string): Promise<void> { ... }
```

### `lib/sse.ts`
```typescript
export function createSSEStore(url: string): Readable<SSEEvent | null>
// Auto-reconnects on error (3s backoff), exposes sseConnected writable
```

### `lib/router.ts`
- `initRouter()`: reads `?tab=` from URL on load, listens for popstate
- `navigate(tab, panel?)`: pushState + updates `$currentTab`
- Mirrors current `syncUrlWithViewState` / `applyViewStateFromUrl` logic

---

## Python Serving Changes

File: `scripts/admin/cockpit.py`

### Change 1: `_shell_path` → `_dist_dir`
```python
# Before
_shell_path: Path = Path(__file__).parent.parent / "cockpit_shell.html"

# After
_dist_dir: Path = Path(__file__).parent / "cockpit" / "dist"
```

### Change 2: `_serve_shell` serves `dist/index.html`
```python
def _serve_shell(self) -> None:
    index = self._dist_dir / "index.html"
    if not index.exists():
        # fallback message if not built yet
        body = b"<html><body><p>Run: cd scripts/admin/cockpit && npm run build</p></body></html>"
        ...
    body = index.read_bytes()
    ...
```

### Change 3: Add static asset route
```python
elif path.startswith("/assets/"):
    self._serve_static(path)
```

```python
def _serve_static(self, path: str) -> None:
    # Serve files from dist/assets/, deny path traversal
    safe = path.lstrip("/")
    if ".." in safe:
        self._err(404); return
    file_path = self._dist_dir / safe
    if not file_path.exists() or not file_path.is_file():
        self._err(404); return
    ext = file_path.suffix
    content_types = {".js": "application/javascript", ".css": "text/css",
                     ".svg": "image/svg+xml", ".png": "image/png"}
    body = file_path.read_bytes()
    self.send_response(200)
    self.send_header("Content-Type", content_types.get(ext, "application/octet-stream"))
    self.send_header("Content-Length", str(len(body)))
    self.end_headers()
    self.wfile.write(body)
```

---

## Vite Config

```typescript
// vite.config.ts
import { defineConfig } from 'vite'
import { svelte } from '@sveltejs/vite-plugin-svelte'

export default defineConfig({
  plugins: [svelte()],
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://localhost:8789',
      '/runner': 'http://localhost:8789',
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
```

---

## CSS Design Tokens

```css
/* src/styles/tokens.css */
:root {
  --color-bg:           #0d1117;
  --color-surface:      #161b22;
  --color-surface-2:    #21262d;
  --color-border:       #30363d;
  --color-border-dim:   #21262d;
  --color-text:         #e6edf3;
  --color-text-muted:   #c9d1d9;
  --color-text-dim:     #8b949e;
  --color-text-faint:   #484f58;
  --color-green:        #3fb950;
  --color-blue:         #58a6ff;
  --color-orange:       #f0883e;
  --color-red:          #f85149;
  --color-green-border: #238636;
  --color-green-bg:     #1c2813;
}
```

---

## Implementation Order

| Step | Deliverable | Cockpit status |
|---|---|---|
| 1 | Scaffold: Vite + Svelte + TS + proxy, empty App.svelte | cockpit_shell.html unchanged |
| 2 | `lib/api.ts` + `lib/sse.ts` + `lib/router.ts` + `styles/tokens.css` | no UI yet |
| 3 | `stores/` — all 4 stores with types, no components | no UI yet |
| 4 | Shared components: TabBar, StatusDot, Badge, SettingsModal, GoalBar, ThresholdsBar | partial UI |
| 5 | Monitors tab (already fully spec'd and tested) | Monitors works |
| 6 | Overview tab (pipeline cards, queue, goal) | most-used tab works |
| 7 | Audit + Session + State tabs | all non-connector tabs work |
| 8 | Connector tab (dynamic) | all tabs work |
| 9 | Python: add `/assets/*` route + switch to `dist/index.html` | Svelte cockpit live |
| 10 | Smoke test all tabs, delete `cockpit_shell.html` | migration complete |

---

## What Does NOT Change

- All `/api/*` route handlers in `cockpit.py` — unchanged
- All `/api/control-plane/*` routes — unchanged
- SSE endpoint `/api/sse/status` — unchanged
- `runner-monitor-state.json` format — unchanged
- `cmd_assets`, `cmd_submit_actions`, all Python business logic — unchanged
- `cockpit.py` `_serve_component` (connector HTML panels) — unchanged
- `.gitignore` for `node_modules/` and `dist/` (add `scripts/admin/cockpit/node_modules/` and `scripts/admin/cockpit/dist/`)

---

## Testing Strategy

- Each store has a corresponding `*.test.ts` (Vitest) testing the fetch/transform logic with mocked `fetch`
- Component tests are out of scope — visual verification in browser is sufficient for this internal tool
- Python `_serve_static` gets a unit test in `tests/test_cockpit_static.py`
- After Step 9, manual smoke test: all tabs render, SSE updates work, settings modal saves, session export works
