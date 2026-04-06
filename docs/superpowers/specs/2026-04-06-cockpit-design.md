# Emerge Cockpit — Design Spec

**Date:** 2026-04-06  
**Replaces:** `/policy` command  
**Command:** `/cockpit`

---

## Overview

The cockpit is the emerge operator's control center. It replaces `/policy` with a browser-based dashboard that lets operators view flywheel state, take actions, and interact with all connector assets — without leaving the workflow.

The cockpit has two layers:

- **Fixed shell** — zero-token, pre-built HTML/JS that handles all structured operations
- **CC dynamic components** — CC generates HTML panels after reading emerge assets; these can be crystallized to eliminate future token cost

All user actions (pipeline changes, NOTES edits, scenario runs) accumulate in an **action queue** and are submitted together, triggering an ElicitRequest that dispatches CC subagents to execute them.

---

## Architecture

### Data flow

```
Browser → repl_admin serve → pending-actions.json → emerge_daemon PendingActionMonitor → ElicitRequest → CC subagent(s)
                ↑
         GET /api/policy (5s poll — live refresh)
         GET /api/assets (connector assets: notes, scenarios, crystallized components)
         GET /api/components (crystallized cockpit components per connector)
         POST /api/submit (write pending-actions.json atomically)
```

### Components to add / modify

| File | Change |
|---|---|
| `scripts/repl_admin.py` | Add `serve` subcommand: HTTP server with API endpoints |
| `scripts/emerge_daemon.py` | Add `PendingActionMonitor` thread |
| `commands/policy.md` | Rename to `commands/cockpit.md`, call `serve --open` |
| `scripts/cockpit_shell.html` | Fixed shell template served at `GET /` |

### New directory: `~/.emerge/connectors/<name>/cockpit/`

Stores crystallized CC-generated components per connector:

```
~/.emerge/connectors/cloud-server/cockpit/
  scenarios-editor.html     # crystallized scenario arg editor
  scenarios-editor.context.md   # why this component exists, when to regenerate
~/.emerge/connectors/hypermesh/cockpit/
  (empty until CC generates and user crystallizes something)
```

---

## Fixed Shell

The shell is a single-page app served by `repl_admin serve`. It has two tabs.

### Tab 1: By Status

Groups pipelines by lifecycle state. Renders three sections: Critical (consecutive_failures ≥ 1), Canary, Stable. Explore pipelines are collapsed by default.

Each pipeline card shows:
- Key, status badge, rollout_pct
- success_rate, verify_rate, human_fix_rate, consecutive_failures
- Action dropdown: Promote / Demote / Reset failures / Delete
- Selecting an action adds it to the queue; card dims and shows queued state

### Tab 2: By Connector

Groups pipelines and assets by connector. For each connector:

- **Header**: connector name, pipeline count, status summary
- **NOTES panel** (if `NOTES.md` exists): read via `connector://<name>/notes` MCP resource. Inline display with "Edit" and "Add comment" buttons. Comment box can be pre-filled by CC (see Dynamic Components section).
- **Scenarios panel** (if `scenarios/*.yaml` exists): list of scenario cards with name, description, required args, step count. "Add to queue" button opens an arg-fill form.
- **Crystallized component slots**: any `.html` files in `connectors/<name>/cockpit/` are loaded into iframes below the NOTES panel.
- **Pipeline list**: same cards as By Status view, scoped to this connector.

### Action Queue (right panel, always visible)

Each queued item shows:
- Action type (colour-coded: red for delete, green for promote, blue for scenario run, yellow for NOTES edit)
- Target key / connector
- Underlying command (e.g. `pipeline-set status=stable`)
- ✕ to remove from queue

At the bottom:
- "Submit to CC (N)" button — POSTs all actions to `/api/submit`
- "Clear queue" link

### Thresholds bar (top of shell)

Shows current threshold values. "Edit Thresholds" button opens a modal with sliders and numeric inputs. Changes queue as a `threshold-set` action type.

---

## pending-actions.json Schema

Written atomically to `~/.emerge/repl/pending-actions.json`.

```json
{
  "submitted_at": 1775444065,
  "actions": [
    {
      "type": "pipeline-delete",
      "key": "pipeline::mock.read.does-not-exist"
    },
    {
      "type": "pipeline-set",
      "key": "pipeline::hypermesh.read.state",
      "fields": { "status": "stable", "rollout_pct": 100 }
    },
    {
      "type": "notes-edit",
      "connector": "hypermesh",
      "content": "Updated NOTES.md full text here"
    },
    {
      "type": "notes-comment",
      "connector": "hypermesh",
      "comment": "2026-04-06: avoid automesh on HM 2025, use hm_batchmesh2"
    },
    {
      "type": "scenario-run",
      "connector": "cloud-server",
      "scenario": "health-check",
      "args": { "env_url": "https://...", "cs_token": "..." }
    },
    {
      "type": "crystallize-component",
      "connector": "cloud-server",
      "filename": "scenarios-editor.html",
      "html": "...",
      "context": "Scenario arg editor for cloud-server. Regenerate if scenarios/*.yaml changes."
    }
  ]
}
```

---

## PendingActionMonitor (emerge_daemon.py)

A new thread in the daemon, analogous to `OperatorMonitor`.

- Polls `~/.emerge/repl/pending-actions.json` every 2 seconds
- Detects new file (by `submitted_at` timestamp)
- Fires `ElicitRequest` to CC with the action list as structured context
- Marks the file as processed (renames to `pending-actions.processed.json`)
- Does NOT execute actions itself — CC receives the request and spawns subagents

Enabled by default (unlike `OperatorMonitor` which requires `EMERGE_OPERATOR_MONITOR=1`), since the cockpit is the primary interaction surface.

---

## CC Dynamic Components

When `/cockpit` is invoked, CC:

1. Reads `GET /api/policy` for flywheel state
2. Reads `GET /api/assets` for each connector's notes, scenario files, existing crystallized components
3. Decides which connectors need a dynamic panel (based on asset richness and absence of up-to-date crystallized components)
4. Generates HTML component(s) — interactive forms with `data-action` attributes that the shell's JS picks up and routes to the action queue
5. Injects via `POST /api/inject-component` — shell renders them in the connector's component slot

### Crystallization

Any CC-generated component can be crystallized:
- A "Crystallize" button appears at the top of every injected component
- Clicking queues a `crystallize-component` action
- When submitted and executed by CC subagent: saves HTML to `connectors/<name>/cockpit/<filename>.html` and `<filename>.context.md`
- Next time `/cockpit` opens, that component loads directly from disk — no CC inference needed

### When to regenerate a crystallized component

The `context.md` contains a human/CC-readable description of what would make the component stale. CC checks this on startup and decides whether to serve the existing crystallized version or regenerate.

---

## /cockpit Command

`commands/cockpit.md` replaces `commands/policy.md`.

Steps:
1. Run `repl_admin.py serve --open --port 0`
2. Print `Dashboard: http://localhost:<PORT>`
3. Print text summary (same as old `/policy` output) for terminal-only contexts
4. CC reads policy state + connector assets
5. CC generates dynamic components for connectors that have no crystallized components (or stale ones)
6. CC injects components via `/api/inject-component`

---

## API Endpoints (repl_admin.py serve)

| Endpoint | Description |
|---|---|
| `GET /` | Serve `cockpit_shell.html` |
| `GET /api/policy` | Returns `cmd_policy_status()` JSON |
| `GET /api/assets` | Returns per-connector: notes content, scenario list (name/description/args/steps), crystallized component filenames |
| `GET /api/components/<connector>/<file>` | Serve a crystallized component HTML file |
| `POST /api/submit` | Accept action list, atomically write `pending-actions.json` |
| `POST /api/inject-component` | Accept CC-generated HTML for a connector slot (in-memory, not crystallized) |
| `GET /api/status` | Server health + pending action count |

---

## Invariants

- `pending-actions.json` uses atomic write (temp + rename). Never write directly.
- `crystallize-component` action is executed by CC subagent, not the shell JS.
- NOTES.md edits go through the action queue and are executed by CC subagent — never direct file writes from the browser.
- Scenario `args` containing secrets (tokens, passwords) are never written to `pending-actions.json` in plaintext. The shell prompts the user to paste secrets directly into CC's terminal response after the ElicitRequest fires.
- CC-generated component HTML must not contain `<script src="...">` external URLs — inline JS only.
- The `PendingActionMonitor` thread is disabled if `EMERGE_COCKPIT_DISABLE=1`.

---

## Out of Scope

- Scenario execution engine in the daemon (CC subagents handle scenario execution via `icc_exec`)
- `connector://<name>/scenarios` MCP resource (scenarios are not formalized into emerge core)
- Multi-user / shared state (single-operator assumption)
- Real-time WebSocket push from daemon to browser (polling is sufficient)
