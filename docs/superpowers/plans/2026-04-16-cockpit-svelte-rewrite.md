# Cockpit Svelte Rewrite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate Cockpit UI from the monolithic `scripts/cockpit_shell.html` to a Svelte + Vite + TypeScript app under `scripts/admin/cockpit/`, while preserving all existing backend APIs and runtime behavior.

**Architecture:** Vite builds UI assets to `scripts/admin/cockpit/dist/`. `scripts/admin/cockpit.py` continues to own all `/api/*` and `/api/control-plane/*` routes, and switches only static serving (`GET /` + `/assets/*`) from `cockpit_shell.html` to `dist/index.html` and its bundled assets.

**Tech Stack:** Svelte 5, TypeScript, Vite 6, Vitest (store/lib tests), Python stdlib static serving for daemon.

---

## File Map

| File | Change |
|---|---|
| `scripts/admin/cockpit/src/*` | New Svelte source tree (app, tabs, shared components, stores, api/router/sse libs) |
| `scripts/admin/cockpit/package.json` | New frontend package manifest and scripts |
| `scripts/admin/cockpit/tsconfig.json` | New TypeScript config |
| `scripts/admin/cockpit/vite.config.ts` | New Vite config with `/api` and `/runner` proxy |
| `scripts/admin/cockpit/index.html` | Vite entry HTML |
| `scripts/admin/cockpit.py` | Serve `dist/index.html` and `/assets/*` with path traversal guard |
| `tests/test_cockpit_static.py` | New tests for static serving (`/`, `/assets/*`, traversal denial) |
| `README.md` | Update Cockpit implementation note and dev workflow commands |
| `CLAUDE.md` | Align architecture/invariants wording for Svelte build output serving |

---

## Task 1: Scaffold Svelte app

**Files:**
- Create: `scripts/admin/cockpit/package.json`
- Create: `scripts/admin/cockpit/tsconfig.json`
- Create: `scripts/admin/cockpit/vite.config.ts`
- Create: `scripts/admin/cockpit/index.html`
- Create: `scripts/admin/cockpit/src/main.ts`
- Create: `scripts/admin/cockpit/src/App.svelte`
- Create: `scripts/admin/cockpit/src/styles/{tokens.css,global.css}`

- [ ] **Step 1: Create package/toolchain files**
  - Add scripts:
    - `dev`: `vite`
    - `build`: `vite build`
    - `preview`: `vite preview`
    - `test`: `vitest run`
    - `typecheck`: `tsc --noEmit`

- [ ] **Step 2: Create minimal app shell**
  - `App.svelte` renders title + empty tab container.
  - `main.ts` mounts app and imports global styles.

- [ ] **Step 3: Verify build and typecheck**
```bash
cd scripts/admin/cockpit
npm install
npm run typecheck
npm run build
```

- [ ] **Step 4: Commit**
```bash
git add scripts/admin/cockpit
git commit -m "feat: scaffold Svelte cockpit app with Vite and TypeScript"
```

---

## Task 2: Implement typed API + SSE + router libs

**Files:**
- Create: `scripts/admin/cockpit/src/lib/api.ts`
- Create: `scripts/admin/cockpit/src/lib/sse.ts`
- Create: `scripts/admin/cockpit/src/lib/router.ts`
- Create: `scripts/admin/cockpit/src/lib/format.ts`
- Create: `scripts/admin/cockpit/src/lib/types.ts`
- Create: `scripts/admin/cockpit/src/lib/api.test.ts`

- [ ] **Step 1: Add shared response/request types**
  - Include policy, monitors, runner-events, session, state, queue, goal payload types.
  - Keep optional fields aligned with current Python outputs.

- [ ] **Step 2: Implement fetch wrappers**
  - Keep endpoint paths unchanged.
  - Centralize error handling (`ok:false`, thrown network errors).

- [ ] **Step 3: Implement SSE helper**
  - Auto-reconnect with backoff.
  - Expose connected/disconnected status.

- [ ] **Step 4: Implement URL router helper**
  - Parse `?tab=` and optional panel/session query params.
  - Provide `navigate(...)` and `applyFromUrl(...)`.

- [ ] **Step 5: Add unit tests for `api.ts`**
  - Mock `fetch`, verify endpoint paths and basic payload adaptation.

- [ ] **Step 6: Verify**
```bash
cd scripts/admin/cockpit
npm run test
npm run typecheck
```

- [ ] **Step 7: Commit**
```bash
git add scripts/admin/cockpit/src/lib
git commit -m "feat: add typed cockpit api/sse/router libraries"
```

---

## Task 3: Create app-level stores and shell layout

**Files:**
- Create: `scripts/admin/cockpit/src/stores/{ui,policy,monitors,session,goal,state}.ts`
- Create: `scripts/admin/cockpit/src/components/shared/{TabBar,StatusDot,Badge}.svelte`
- Update: `scripts/admin/cockpit/src/App.svelte`

- [ ] **Step 1: Build Svelte stores**
  - `ui`: current tab, modal state, connector panel map.
  - `policy`: pipelines/connectors/thresholds/queue.
  - `monitors`: runners, expanded feeds, recent cache.
  - `session`: session + hook state.
  - `goal`: active goal + history.
  - `state`: deltas/risks list state.

- [ ] **Step 2: Build shared UI components**
  - Preserve current semantics (colors/status naming).

- [ ] **Step 3: Wire app shell**
  - Top tab bar
  - Shared goal/threshold area
  - Single active-tab outlet

- [ ] **Step 4: Verify**
```bash
cd scripts/admin/cockpit
npm run typecheck
npm run build
```

- [ ] **Step 5: Commit**
```bash
git add scripts/admin/cockpit/src
git commit -m "feat: add cockpit stores and shared shell components"
```

---

## Task 4: Migrate Monitors tab first (high value / low coupling)

**Files:**
- Create: `scripts/admin/cockpit/src/components/monitors/{MonitorsTab,RunnerCard,Sparkline,EventFeed}.svelte`
- Update: `scripts/admin/cockpit/src/App.svelte`
- Add/Update tests: `scripts/admin/cockpit/src/lib/api.test.ts` (runner-events behavior)

- [ ] **Step 1: Port current card-grid Monitors UI**
  - Keep current event badge vocabulary (`pattern_alert`, `operator_message`, `runner_online`, fallback).
  - Keep expanded feed persistence by profile.

- [ ] **Step 2: Hook SSE refresh**
  - On `monitors_updated`, refresh monitors store and expanded feeds.

- [ ] **Step 3: Manual verification**
  - Load Monitors tab, verify:
    - team status bar
    - card grid
    - sparkline
    - expand/collapse feed
    - periodic + SSE refresh behavior

- [ ] **Step 4: Commit**
```bash
git add scripts/admin/cockpit/src/components/monitors scripts/admin/cockpit/src/App.svelte
git commit -m "feat: migrate monitors tab to Svelte card-grid implementation"
```

---

## Task 5: Migrate Overview tab + settings modal + queue panel

**Files:**
- Create: `scripts/admin/cockpit/src/components/overview/{OverviewTab,PipelineCard,PipelineGroup,QueuePanel}.svelte`
- Create: `scripts/admin/cockpit/src/components/shared/{SettingsModal,GoalBar,ThresholdsBar}.svelte`
- Update: `scripts/admin/cockpit/src/App.svelte`

- [ ] **Step 1: Port pipeline cards and queue rendering**
  - Keep group-by-connector and status badge semantics.
  - Keep action submission behavior unchanged.

- [ ] **Step 2: Port settings modal + threshold update flow**
  - Keep payload schema and optimistic feedback behavior.

- [ ] **Step 3: Verify**
  - Queue actions submit correctly.
  - Goal submit/read path still works.
  - Threshold update reflected in UI.

- [ ] **Step 4: Commit**
```bash
git add scripts/admin/cockpit/src/components/overview scripts/admin/cockpit/src/components/shared scripts/admin/cockpit/src/App.svelte
git commit -m "feat: migrate overview tab and settings modal to Svelte"
```

---

## Task 6: Migrate Audit + Session + State + Connector tabs

**Files:**
- Create: `scripts/admin/cockpit/src/components/audit/AuditTab.svelte`
- Create: `scripts/admin/cockpit/src/components/session/SessionTab.svelte`
- Create: `scripts/admin/cockpit/src/components/state/StateTab.svelte`
- Create: `scripts/admin/cockpit/src/components/connector/ConnectorTab.svelte`
- Update: `scripts/admin/cockpit/src/App.svelte`

- [ ] **Step 1: Port Audit timeline**
  - Preserve event rendering order and severity coloring.

- [ ] **Step 2: Port Session controls**
  - Preserve export/reset behavior and session selector behavior.

- [ ] **Step 3: Port State tab**
  - Preserve risk status rendering and large-list behavior.

- [ ] **Step 4: Port connector dynamic tabs**
  - Keep panel selection and existing `/api/component` usage.

- [ ] **Step 5: Commit**
```bash
git add scripts/admin/cockpit/src/components scripts/admin/cockpit/src/App.svelte
git commit -m "feat: migrate remaining cockpit tabs to Svelte"
```

---

## Task 7: Python static serving integration

**Files:**
- Modify: `scripts/admin/cockpit.py`
- Create: `tests/test_cockpit_static.py`

- [ ] **Step 1: Replace shell serving source**
  - Serve `scripts/admin/cockpit/dist/index.html` for `GET /`.
  - Add friendly fallback page if dist is missing (build instruction).

- [ ] **Step 2: Add `/assets/*` static route**
  - Path traversal guard.
  - Content type mapping for `.js`, `.css`, `.svg`, `.png`, `.ico`, `.map`.

- [ ] **Step 3: Add tests**
  - Index served when dist exists.
  - Asset served with correct content type.
  - Traversal path returns 404.
  - Missing dist returns fallback HTML.

- [ ] **Step 4: Verify**
```bash
python3 -m pytest tests/test_cockpit_static.py -q
python3 -m pytest tests/test_daemon_http.py tests/test_repl_admin.py -q
```

- [ ] **Step 5: Commit**
```bash
git add scripts/admin/cockpit.py tests/test_cockpit_static.py
git commit -m "feat: serve Svelte cockpit dist assets from daemon control plane"
```

---

## Task 8: End-to-end smoke and cleanup

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`
- Delete: `scripts/cockpit_shell.html` (only after successful smoke)

- [ ] **Step 1: Build frontend and run full Python tests**
```bash
cd scripts/admin/cockpit && npm run build
cd /Users/apple/Documents/workspace/emerge && python3 -m pytest tests -q
```

- [ ] **Step 2: Manual smoke test**
  - Start daemon
  - Open Cockpit at `http://localhost:8789`
  - Validate all tabs + session routing + SSE refresh + settings modal

- [ ] **Step 3: Remove legacy file**
  - Delete `scripts/cockpit_shell.html`
  - Ensure no runtime references remain.

- [ ] **Step 4: Docs sync**
  - Update README Cockpit component description.
  - Update CLAUDE architecture/invariants where serving path changed.

- [ ] **Step 5: Commit**
```bash
git add README.md CLAUDE.md scripts/cockpit_shell.html
git commit -m "refactor: remove legacy cockpit_shell.html after Svelte migration"
```

---

## Verification Checklist

- [ ] `scripts/admin/cockpit` can `npm run typecheck`, `npm run test`, `npm run build`
- [ ] `GET /` serves Svelte app in daemon mode
- [ ] `/assets/*` static files load correctly
- [ ] All `/api/*` endpoints remain backward compatible
- [ ] Monitors SSE refresh behavior unchanged
- [ ] Session-scoped routing behavior unchanged
- [ ] Full Python test suite passes
- [ ] Docs (`README.md`, `CLAUDE.md`) match new architecture

---

## Risks and Mitigations

- [ ] **Risk:** API drift between JS and Python payloads  
      **Mitigation:** Centralize typed DTOs in `lib/types.ts` and add narrow adapter functions in `lib/api.ts`.

- [ ] **Risk:** Dist not built in local/dev sessions  
      **Mitigation:** Provide fallback HTML message in `_serve_shell`; document build command.

- [ ] **Risk:** SSE reconnect storms  
      **Mitigation:** Cap reconnection retry with fixed 3s backoff and ensure one EventSource instance at a time.

- [ ] **Risk:** Feature parity regression due to large rewrite  
      **Mitigation:** Migrate tab-by-tab with commit checkpoints and smoke each tab before proceeding.
