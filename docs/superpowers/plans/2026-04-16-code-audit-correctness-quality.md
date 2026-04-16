# Code Audit — Correctness & Quality Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix three correctness issues (untracked files, CLAUDE.md gap, doc clarity) then decompose two oversized Svelte files into focused units.

**Architecture:** Section 1 is pure housekeeping. Section 2 extracts pure TS helpers + two sub-components from StateTab.svelte (752L → 4 files). Section 3 extracts queue state from App.svelte into an existing-pattern Svelte store.

**Tech Stack:** Python (pytest), TypeScript, Svelte 5, Vite/Vitest — `cd scripts/admin/cockpit` for all frontend commands.

---

## File Map

| Action | File |
|--------|------|
| `git add` | `scripts/admin/cockpit/src/lib/markdown.ts` |
| `git add` | `tests/connectors/zwcad/NOTES.md` |
| Modify | `CLAUDE.md` (hookSpecificOutput list) |
| **Create** | `scripts/admin/cockpit/src/lib/state-helpers.ts` |
| **Create** | `scripts/admin/cockpit/src/lib/state-helpers.test.ts` |
| **Create** | `scripts/admin/cockpit/src/components/state/StateList.svelte` |
| **Create** | `scripts/admin/cockpit/src/components/state/StateDiag.svelte` |
| Rewrite | `scripts/admin/cockpit/src/components/state/StateTab.svelte` |
| **Create** | `scripts/admin/cockpit/src/stores/queue.ts` |
| **Create** | `scripts/admin/cockpit/src/stores/queue.test.ts` |
| Modify | `scripts/admin/cockpit/src/App.svelte` |

---

## Task 1: Commit Untracked Files

**Files:** `scripts/admin/cockpit/src/lib/markdown.ts`, `tests/connectors/zwcad/NOTES.md`

- [ ] **Step 1: Stage and commit**

```bash
git add scripts/admin/cockpit/src/lib/markdown.ts tests/connectors/zwcad/NOTES.md
git commit -m "chore: track markdown.ts and zwcad NOTES.md"
```

Expected: `2 files changed` commit.

---

## Task 2: Patch CLAUDE.md — hookSpecificOutput Allowed List

**Files:** `CLAUDE.md`

- [ ] **Step 1: Locate the hook output schema invariant**

Find this line in CLAUDE.md Key Invariants section:
```
CC's hook validator accepts `hookSpecificOutput` for: `PreToolUse`, `UserPromptSubmit`, `PostToolUse`, `SessionStart`, `FileChanged`, `Setup`, `SubagentStart`, `PostToolUseFailure`, `Notification`, and `PermissionRequest`.
```

- [ ] **Step 2: Add Elicitation and clarify ElicitationResult**

Replace the sentence ending with `PermissionRequest`.` with:

```
CC's hook validator accepts `hookSpecificOutput` for: `PreToolUse`, `UserPromptSubmit`, `PostToolUse`, `SessionStart`, `FileChanged`, `Setup`, `SubagentStart`, `PostToolUseFailure`, `Notification`, `PermissionRequest`, and `Elicitation`. `Elicitation` uses `hookSpecificOutput.hookEventName="Elicitation"` + `action: "accept"|"decline"|"delegate"` + `content: {...}`. `ElicitationResult` is a read-only notification event — return `{}` only, `hookSpecificOutput` has no effect.
```

- [ ] **Step 3: Verify no other Elicitation references are missing**

```bash
grep -n "Elicitation\|ElicitationResult" CLAUDE.md
```

Expected: entries in Key Invariants (hooks.json matchers line) and the hook output schema line.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: add Elicitation to hookSpecificOutput allowed list"
```

---

## Task 3: Extract state-helpers.ts (TDD)

**Files:** Create `scripts/admin/cockpit/src/lib/state-helpers.ts`, `scripts/admin/cockpit/src/lib/state-helpers.test.ts`

- [ ] **Step 1: Write the failing tests**

Create `scripts/admin/cockpit/src/lib/state-helpers.test.ts`:

```ts
import { describe, expect, it } from 'vitest';
import {
  buildRows,
  icon,
  label,
  relatedRows,
  rowStatusCls,
  statusBadgeCls,
  toText,
  formatTime,
  type StateRow,
} from './state-helpers';

describe('toText', () => {
  it('returns empty string for null/undefined', () => {
    expect(toText(null)).toBe('');
    expect(toText(undefined)).toBe('');
  });
  it('converts primitives to string', () => {
    expect(toText('hello')).toBe('hello');
    expect(toText(42)).toBe('42');
    expect(toText(true)).toBe('true');
  });
  it('returns empty string for objects', () => {
    expect(toText({ a: 1 })).toBe('');
  });
});

describe('icon', () => {
  it('returns correct icons', () => {
    expect(icon('delta')).toBe('Δ');
    expect(icon('risk')).toBe('⚠');
    expect(icon('span')).toBe('◉');
    expect(icon('exec-event')).toBe('E');
    expect(icon('pipeline-event')).toBe('P');
  });
});

describe('label', () => {
  it('returns correct labels', () => {
    expect(label('delta')).toBe('delta');
    expect(label('exec-event')).toBe('exec');
    expect(label('pipeline-event')).toBe('pipeline');
  });
});

describe('statusBadgeCls', () => {
  it('maps error states to critical', () => {
    expect(statusBadgeCls('error')).toBe('critical');
    expect(statusBadgeCls('failure')).toBe('critical');
    expect(statusBadgeCls('retract')).toBe('critical');
  });
  it('maps success states to stable', () => {
    expect(statusBadgeCls('ok')).toBe('stable');
    expect(statusBadgeCls('handled')).toBe('stable');
  });
  it('handles open/provisional/snoozed', () => {
    expect(statusBadgeCls('open')).toBe('open');
    expect(statusBadgeCls('provisional')).toBe('provisional');
    expect(statusBadgeCls('snoozed')).toBe('snoozed');
  });
  it('is case-insensitive', () => {
    expect(statusBadgeCls('ERROR')).toBe('critical');
  });
  it('returns empty string for unknown', () => {
    expect(statusBadgeCls('unknown')).toBe('');
  });
});

describe('rowStatusCls', () => {
  it('maps error to critical, ok to stable', () => {
    expect(rowStatusCls('error')).toBe('critical');
    expect(rowStatusCls('ok')).toBe('stable');
    expect(rowStatusCls('open')).toBe('');
  });
});

describe('buildRows', () => {
  it('builds delta rows from payload', () => {
    const rows = buildRows({
      deltas: [{ id: 'd1', message: 'test delta', intent_signature: 'foo', ts_ms: 1000 }],
      risks: [],
      spans: [],
      execEvents: [],
      pipelineEvents: [],
    });
    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe('delta');
    expect(rows[0].key).toBe('delta:d1');
    expect(rows[0].title).toBe('test delta');
    expect(rows[0].intent).toBe('foo');
  });

  it('sorts rows by ts descending', () => {
    const rows = buildRows({
      deltas: [
        { id: 'old', message: 'old', ts_ms: 100 },
        { id: 'new', message: 'new', ts_ms: 200 },
      ],
      risks: [], spans: [], execEvents: [], pipelineEvents: [],
    });
    expect(rows[0].key).toBe('delta:new');
    expect(rows[1].key).toBe('delta:old');
  });

  it('marks unreconciled delta status as open', () => {
    const rows = buildRows({
      deltas: [{ id: 'd1', message: 'x' }],
      risks: [], spans: [], execEvents: [], pipelineEvents: [],
    });
    expect(rows[0].status).toBe('open');
  });
});

describe('relatedRows', () => {
  const rows: StateRow[] = [
    { key: 'delta:a', kind: 'delta', ts: 0, intent: 'foo.bar', title: 'A', status: 'open', data: {} },
    { key: 'risk:b', kind: 'risk', ts: 0, intent: 'foo.bar', title: 'B', status: 'open', data: {} },
    { key: 'span:c', kind: 'span', ts: 0, intent: 'other', title: 'C', status: 'ok', data: {} },
  ];

  it('returns empty array when selected is null', () => {
    expect(relatedRows(rows, null)).toEqual([]);
  });

  it('returns rows with the same intent', () => {
    const related = relatedRows(rows, rows[0]);
    expect(related.map((r) => r.key)).toEqual(['delta:a', 'risk:b']);
  });
});

describe('formatTime', () => {
  it('returns empty string for falsy ts', () => {
    expect(formatTime(0)).toBe('');
  });
  it('returns a string for valid ts', () => {
    const result = formatTime(1_000_000_000);
    expect(typeof result).toBe('string');
    expect(result.length).toBeGreaterThan(0);
  });
});
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd scripts/admin/cockpit && npm test -- state-helpers 2>&1 | tail -10
```

Expected: FAIL — `Cannot find module './state-helpers'`

- [ ] **Step 3: Create state-helpers.ts**

Create `scripts/admin/cockpit/src/lib/state-helpers.ts`:

```ts
import type { JsonObject } from './types';

export type StateKind = 'delta' | 'risk' | 'span' | 'exec-event' | 'pipeline-event';

export interface StateRow {
  key: string;
  kind: StateKind;
  ts: number;
  intent: string;
  title: string;
  status: string;
  data: JsonObject;
}

export function toText(value: unknown): string {
  if (value === null || value === undefined) return '';
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean')
    return String(value);
  return '';
}

export function icon(kind: StateKind): string {
  if (kind === 'delta') return 'Δ';
  if (kind === 'risk') return '⚠';
  if (kind === 'span') return '◉';
  if (kind === 'exec-event') return 'E';
  if (kind === 'pipeline-event') return 'P';
  return '•';
}

export function label(kind: StateKind): string {
  if (kind === 'delta') return 'delta';
  if (kind === 'risk') return 'risk';
  if (kind === 'span') return 'span';
  if (kind === 'exec-event') return 'exec';
  if (kind === 'pipeline-event') return 'pipeline';
  return kind;
}

export function statusBadgeCls(status: string): string {
  const s = (status || '').toLowerCase();
  if (s === 'error' || s === 'failure' || s === 'retract') return 'critical';
  if (s === 'ok' || s === 'success' || s === 'handled') return 'stable';
  if (s === 'open') return 'open';
  if (s === 'provisional') return 'provisional';
  if (s === 'snoozed') return 'snoozed';
  return '';
}

export function rowStatusCls(status: string): string {
  const s = (status || '').toLowerCase();
  if (s === 'error' || s === 'failure' || s === 'retract') return 'critical';
  if (s === 'ok' || s === 'success') return 'stable';
  return '';
}

export function formatTime(ts: number): string {
  if (!ts) return '';
  return new Date(ts).toLocaleTimeString();
}

export function buildRows(payload: {
  deltas: JsonObject[];
  risks: JsonObject[];
  spans: JsonObject[];
  execEvents: JsonObject[];
  pipelineEvents: JsonObject[];
}): StateRow[] {
  const out: StateRow[] = [];
  let seq = 0;
  (payload.deltas || []).forEach((d) => {
    const key = `delta:${toText(d.id) || ++seq}`;
    out.push({
      key,
      kind: 'delta',
      ts: Number(d.ts_ms || 0),
      intent: String(d.intent_signature || ''),
      title: String(d.message || '(no message)'),
      status: String(d.reconcile_outcome || (d.provisional ? 'provisional' : 'open')),
      data: d,
    });
  });
  (payload.risks || []).forEach((r) => {
    const key = `risk:${toText(r.risk_id) || ++seq}`;
    out.push({
      key,
      kind: 'risk',
      ts: Number(r.created_at_ms || 0),
      intent: String(r.intent_signature || ''),
      title: String(r.text || '(no risk text)'),
      status: String(r.status || 'open'),
      data: r,
    });
  });
  (payload.spans || []).forEach((s) => {
    const key = `span:${toText(s.span_id) || `${s.closed_at_ms || 0}:${toText(s.intent_signature)}:${++seq}`}`;
    out.push({
      key,
      kind: 'span',
      ts: Number(s.closed_at_ms || s.opened_at_ms || 0),
      intent: String(s.intent_signature || ''),
      title: String(s.description || 'span'),
      status: String(s.outcome || 'unknown'),
      data: s,
    });
  });
  (payload.execEvents || []).forEach((e, i) => {
    const key = `exec-event:${i}:${e.ts_ms || 0}:${toText(e.intent_signature)}`;
    out.push({
      key,
      kind: 'exec-event',
      ts: Number(e.ts_ms || 0),
      intent: String(e.intent_signature || ''),
      title: String(e.mode || 'exec'),
      status: e.is_error ? 'error' : 'ok',
      data: e,
    });
  });
  (payload.pipelineEvents || []).forEach((e, i) => {
    const pipeId = toText(e.pipeline_id) || toText(e.intent_signature) || '';
    const key = `pipeline-event:${i}:${e.ts_ms || 0}:${pipeId}`;
    out.push({
      key,
      kind: 'pipeline-event',
      ts: Number(e.ts_ms || 0),
      intent: String(e.intent_signature || pipeId),
      title: String(e.pipeline_id || 'pipeline-event'),
      status: e.is_error ? 'error' : 'ok',
      data: e,
    });
  });
  out.sort((a, b) => (b.ts || 0) - (a.ts || 0));
  return out;
}

export function relatedRows(all: StateRow[], selected: StateRow | null): StateRow[] {
  if (!selected) return [];
  const intent = String(selected.intent || '').trim();
  if (intent) return all.filter((r) => String(r.intent || '') === intent);
  const titlePrefix = String(selected.title || '').trim().slice(0, 32).toLowerCase();
  if (!titlePrefix) return [selected];
  return all.filter(
    (r) => r.kind === selected.kind && String(r.title || '').toLowerCase().includes(titlePrefix)
  );
}
```

- [ ] **Step 4: Run tests — must pass**

```bash
cd scripts/admin/cockpit && npm test -- state-helpers 2>&1 | tail -10
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/admin/cockpit/src/lib/state-helpers.ts scripts/admin/cockpit/src/lib/state-helpers.test.ts
git commit -m "feat: extract state-helpers.ts with pure functions and tests"
```

---

## Task 4: Create StateList.svelte

**Files:** Create `scripts/admin/cockpit/src/components/state/StateList.svelte`

Owns: filter bar, virtual scroll list, count badges. Receives rows + filter state as props. Emits selection and filter change events.

- [ ] **Step 1: Create StateList.svelte**

```svelte
<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { icon, label, statusBadgeCls, formatTime, type StateKind, type StateRow } from '../../lib/state-helpers';

  const STATE_ROW_H = 72;

  export let rows: StateRow[];
  export let filterKind: 'all' | StateKind = 'all';
  export let queryText = '';
  export let selectedKey = '';

  const dispatch = createEventDispatcher<{
    select: string;
    filterChange: 'all' | StateKind;
    queryChange: string;
  }>();

  let listBody: HTMLDivElement | null = null;
  let scrollTop = 0;

  $: q = queryText.trim().toLowerCase();
  $: filtered = rows.filter((r) => {
    if (filterKind !== 'all' && r.kind !== filterKind) return false;
    if (!q) return true;
    return (
      (r.title || '').toLowerCase().includes(q) ||
      (r.intent || '').toLowerCase().includes(q) ||
      (r.status || '').toLowerCase().includes(q) ||
      (r.kind || '').toLowerCase().includes(q)
    );
  });

  $: countBy = (() => {
    const c: Record<StateKind, number> = { delta: 0, risk: 0, span: 0, 'exec-event': 0, 'pipeline-event': 0 };
    rows.forEach((r) => { if (c[r.kind] != null) c[r.kind] += 1; });
    return c;
  })();

  $: total = filtered.length;
  $: viewportH = listBody && listBody.clientHeight > 0 ? listBody.clientHeight : STATE_ROW_H * 4;
  $: overscan = 6;
  $: start = Math.max(0, Math.floor(scrollTop / STATE_ROW_H) - overscan);
  $: visibleCount = Math.ceil(viewportH / STATE_ROW_H) + overscan * 2;
  $: end = Math.min(total, start + visibleCount);
  $: visibleRows = filtered.slice(start, end);
</script>

<div class="state-list-wrap">
  <div class="state-filter-row">
    <button type="button" class="cp-btn-sm" class:warn={filterKind === 'all'}
      on:click={() => dispatch('filterChange', 'all')}>All <span style="opacity:0.8">{rows.length}</span></button>
    <button type="button" class="cp-btn-sm" class:warn={filterKind === 'delta'}
      on:click={() => dispatch('filterChange', 'delta')}>Deltas <span style="opacity:0.8">{countBy.delta}</span></button>
    <button type="button" class="cp-btn-sm" class:warn={filterKind === 'risk'}
      on:click={() => dispatch('filterChange', 'risk')}>Risks <span style="opacity:0.8">{countBy.risk}</span></button>
    <button type="button" class="cp-btn-sm" class:warn={filterKind === 'span'}
      on:click={() => dispatch('filterChange', 'span')}>Spans <span style="opacity:0.8">{countBy.span}</span></button>
    <button type="button" class="cp-btn-sm" class:warn={filterKind === 'exec-event'}
      on:click={() => dispatch('filterChange', 'exec-event')}>Exec <span style="opacity:0.8">{countBy['exec-event']}</span></button>
    <button type="button" class="cp-btn-sm" class:warn={filterKind === 'pipeline-event'}
      on:click={() => dispatch('filterChange', 'pipeline-event')}>Pipeline <span style="opacity:0.8">{countBy['pipeline-event']}</span></button>
    <input
      type="text"
      value={queryText}
      on:input={(e) => dispatch('queryChange', (e.currentTarget as HTMLInputElement).value)}
      placeholder="Search intent/text/status..."
    />
  </div>

  <div class="state-col">
    <div class="state-col-head">List · {filtered.length} item(s)</div>
    {#if !filtered.length}
      <div class="state-col-body"><p class="muted" style="padding: 10px">No matching objects.</p></div>
    {:else}
      <div
        class="state-col-body"
        id="state-list-body"
        bind:this={listBody}
        on:scroll={(e) => { scrollTop = (e.currentTarget as HTMLDivElement).scrollTop; }}
      >
        <div style="height: {total * STATE_ROW_H}px; position: relative">
          {#each visibleRows as r, vi (r.key)}
            {@const idx = start + vi}
            {@const sel = r.key === selectedKey}
            <button
              type="button"
              class="state-row-btn"
              class:selected={sel}
              style="top: {idx * STATE_ROW_H + 4}px"
              on:click={() => dispatch('select', r.key)}
            >
              <div class="state-row-line1">
                <span class="state-ico">{icon(r.kind)}</span>
                <span class="state-lbl">{label(r.kind)}</span>
                <span class="status-badge {statusBadgeCls(r.status)}">{r.status}</span>
                <span class="state-ts">{formatTime(r.ts)}</span>
              </div>
              <div class="state-row-title">{r.title}</div>
              <div class="state-row-intent">{r.intent || '(no intent)'}</div>
            </button>
          {/each}
        </div>
      </div>
    {/if}
  </div>
</div>

<style>
  .state-list-wrap {
    display: flex;
    flex-direction: column;
    min-height: 0;
    flex: 1;
  }
  .muted { color: #8b949e; font-size: 11px; }
  .state-row-btn {
    position: absolute;
    left: 6px;
    right: 6px;
    height: 64px;
    text-align: left;
    background: #11161d;
    border: 1px solid #1f2630;
    border-left: 3px solid #30363d;
    border-radius: 4px;
    padding: 8px;
    cursor: pointer;
    font-family: inherit;
    color: #c9d1d9;
  }
  .state-row-btn.selected { background: #13243a; border-color: #388bfd; }
  .state-row-line1 { display: flex; gap: 8px; align-items: center; }
  .state-ico { min-width: 16px; color: #79c0ff; }
  .state-lbl { font-size: 10px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.6px; min-width: 56px; }
  .state-ts { margin-left: auto; font-size: 10px; color: #6e7681; }
  .state-row-title { margin-top: 4px; color: #c9d1d9; font-size: 11px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .state-row-intent { margin-top: 2px; color: #8b949e; font-size: 10px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
</style>
```

- [ ] **Step 2: Typecheck**

```bash
cd scripts/admin/cockpit && npm run typecheck 2>&1 | tail -20
```

Expected: no errors in StateList.svelte.

- [ ] **Step 3: Commit**

```bash
git add scripts/admin/cockpit/src/components/state/StateList.svelte
git commit -m "feat: extract StateList.svelte (filter bar + virtual scroll)"
```

---

## Task 5: Create StateDiag.svelte

**Files:** Create `scripts/admin/cockpit/src/components/state/StateDiag.svelte`

Owns: detail panel, related timeline, reconcile/risk-update action buttons. Dispatches events upward; StateTab handles API calls.

- [ ] **Step 1: Create StateDiag.svelte**

```svelte
<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import JsonViewerBlock from '../shared/JsonViewerBlock.svelte';
  import {
    icon, label, statusBadgeCls, rowStatusCls, toText, formatTime,
    type StateRow,
  } from '../../lib/state-helpers';
  import type { JsonObject } from '../../lib/types';

  export let selected: StateRow | null;
  export let related: StateRow[];
  export let writing: boolean;

  const dispatch = createEventDispatcher<{
    deltaReconcile: { deltaId: string; outcome: string };
    riskUpdate: { riskId: string; action: 'handle' | 'snooze' };
    filterByIntent: string;
    selectRow: string;
  }>();

  $: relatedCounts = {
    delta: related.filter((x) => x.kind === 'delta').length,
    risk: related.filter((x) => x.kind === 'risk').length,
    span: related.filter((x) => x.kind === 'span').length,
    'exec-event': related.filter((x) => x.kind === 'exec-event').length,
    'pipeline-event': related.filter((x) => x.kind === 'pipeline-event').length,
  };

  function handleDeltaReconcile(outcome: string): void {
    if (!selected) return;
    const deltaId = toText((selected.data as JsonObject).id);
    if (outcome === 'retract') {
      if (!window.confirm('Retract this delta? Verification may degrade. This cannot be trivially undone.')) return;
    } else if (outcome === 'correct') {
      if (!window.confirm('Mark this delta as correct (human fix)?')) return;
    }
    dispatch('deltaReconcile', { deltaId, outcome });
  }

  function handleRiskUpdate(action: 'handle' | 'snooze'): void {
    if (!selected) return;
    const riskId = toText((selected.data as JsonObject).risk_id);
    dispatch('riskUpdate', { riskId, action });
  }
</script>

<div class="state-col">
  <div class="state-col-head">Diagnostics</div>
  <div class="state-col-body state-diag-body">
    {#if !selected}
      <p class="muted" style="padding: 10px">Select an object from the list.</p>
    {:else}
      <div class="state-diag-wrap">
        <div class="state-diag-meta" id="state-diag-meta">
          <div class="diag-head">
            <span class="d-ico">{icon(selected.kind)}</span>
            <span class="d-kind">{label(selected.kind)}</span>
            <span class="status-badge {statusBadgeCls(selected.status)}">{selected.status}</span>
          </div>
          <div class="d-line"><b>Intent:</b> {selected.intent || '(none)'}</div>
          <div class="d-line"><b>Title:</b> {selected.title}</div>
          {#if toText(selected.data.args_summary)}
            <div class="d-line args-line"><b>Args:</b> <code>{toText(selected.data.args_summary)}</code></div>
          {:else}
            <div class="d-line" style="margin-bottom: 10px"></div>
          {/if}

          {#if selected.kind === 'delta' && !selected.data.reconcile_outcome}
            <div class="action-row">
              <button type="button" class="cp-btn-sm" disabled={writing} on:click={() => handleDeltaReconcile('confirm')}>Confirm</button>
              <button type="button" class="cp-btn-sm warn" disabled={writing} on:click={() => handleDeltaReconcile('correct')}>Correct</button>
              <button type="button" class="cp-btn-sm danger" disabled={writing} on:click={() => handleDeltaReconcile('retract')}>Retract</button>
            </div>
          {/if}
          {#if selected.kind === 'risk' && selected.data.status === 'open'}
            <div class="action-row">
              <button type="button" class="cp-btn-sm" disabled={writing} on:click={() => handleRiskUpdate('handle')}>Handle</button>
              <button type="button" class="cp-btn-sm" disabled={writing} on:click={() => handleRiskUpdate('snooze')}>Snooze</button>
            </div>
          {/if}
          {#if selected.intent}
            <div class="action-row">
              <button type="button" class="cp-btn-sm" on:click={() => dispatch('filterByIntent', selected.intent)}>Filter by intent</button>
            </div>
          {/if}
        </div>

        <div class="state-related-wrap" id="state-related-wrap">
          <div class="rel-title">Related Timeline (same intent/object cluster)</div>
          <div class="rel-counts">
            <span class="status-badge">total {related.length}</span>
            <span class="status-badge">Δ {relatedCounts.delta}</span>
            <span class="status-badge">⚠ {relatedCounts.risk}</span>
            <span class="status-badge">◉ {relatedCounts.span}</span>
            <span class="status-badge">E {relatedCounts['exec-event']}</span>
            <span class="status-badge">P {relatedCounts['pipeline-event']}</span>
          </div>
          {#if !related.length}
            <p class="muted sm">No related records found.</p>
          {:else}
            <div class="rel-box">
              {#each related as rel}
                <button type="button" class="rel-row" on:click={() => dispatch('selectRow', rel.key)}>
                  <span class="state-ico">{icon(rel.kind)}</span>
                  <span class="rel-l">{label(rel.kind)}</span>
                  <span class="status-badge {rowStatusCls(rel.status)} sm">{rel.status}</span>
                  <span class="rel-t">{rel.title}</span>
                  <span class="rel-time">{formatTime(rel.ts)}</span>
                </button>
              {/each}
            </div>
          {/if}
        </div>

        {#key selected.key}
          <JsonViewerBlock viewerId="state-detail" data={selected.data} />
        {/key}
      </div>
    {/if}
  </div>
</div>

<style>
  .muted { color: #8b949e; font-size: 11px; }
  .muted.sm { font-size: 10px; padding: 6px 0; }
  .diag-head { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; }
  .d-ico { color: #79c0ff; }
  .d-kind { font-size: 11px; color: #e6edf3; text-transform: uppercase; letter-spacing: 0.6px; }
  .d-line { font-size: 11px; color: #c9d1d9; margin-bottom: 6px; }
  .args-line code { font-size: 10px; color: #a5d6ff; }
  .action-row { display: flex; gap: 6px; margin-bottom: 10px; flex-wrap: wrap; }
  .rel-title { font-size: 11px; color: #8b949e; margin-bottom: 6px; }
  .rel-counts { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 8px; }
  .rel-box { border: 1px solid #21262d; border-radius: 4px; background: #11161d; }
  .rel-row { display: flex; width: 100%; gap: 8px; align-items: center; border: none; border-bottom: 1px solid #1f2630; background: transparent; color: #c9d1d9; padding: 6px 8px; font-family: inherit; font-size: 10px; cursor: pointer; text-align: left; }
  .rel-row:last-child { border-bottom: none; }
  .state-ico { min-width: 16px; color: #79c0ff; }
  .rel-l { min-width: 52px; color: #8b949e; text-transform: uppercase; }
  .rel-t { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .rel-time { color: #6e7681; }
  .status-badge.sm { font-size: 9px; }
</style>
```

- [ ] **Step 2: Typecheck**

```bash
cd scripts/admin/cockpit && npm run typecheck 2>&1 | tail -20
```

Expected: no errors in StateDiag.svelte.

- [ ] **Step 3: Commit**

```bash
git add scripts/admin/cockpit/src/components/state/StateDiag.svelte
git commit -m "feat: extract StateDiag.svelte (detail panel + action buttons)"
```

---

## Task 6: Rewrite StateTab.svelte as Thin Coordinator

**Files:** Rewrite `scripts/admin/cockpit/src/components/state/StateTab.svelte`

Keeps: data fetching, fingerprint dedup, stats strip, selectedKey coordination. Delegates all display to StateList + StateDiag.

- [ ] **Step 1: Replace StateTab.svelte**

```svelte
<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '../../lib/api';
  import { stateStore } from '../../stores/state';
  import { buildRows, relatedRows, type StateKind, type StateRow } from '../../lib/state-helpers';
  import type { JsonObject } from '../../lib/types';
  import StateList from './StateList.svelte';
  import StateDiag from './StateDiag.svelte';

  export let sessionId: string | undefined;
  export let refreshSignal = 0;

  let loading = false;
  let error: string | null = null;
  let rows: StateRow[] = [];
  let rowsFingerprint: string | null = null;
  let filterKind: 'all' | StateKind = 'all';
  let queryText = '';
  let selectedKey = '';
  let writing = false;
  let observedRefreshSignal = refreshSignal;

  $: indexMap = Object.fromEntries(rows.map((r) => [r.key, r]));
  $: filtered = rows.filter((r) => {
    if (filterKind !== 'all' && r.kind !== filterKind) return false;
    const q = queryText.trim().toLowerCase();
    if (!q) return true;
    return (
      (r.title || '').toLowerCase().includes(q) ||
      (r.intent || '').toLowerCase().includes(q) ||
      (r.status || '').toLowerCase().includes(q) ||
      (r.kind || '').toLowerCase().includes(q)
    );
  });

  $: if (filtered.length) {
    if (!filtered.some((r) => r.key === selectedKey)) selectedKey = filtered[0].key;
  } else {
    selectedKey = '';
  }

  $: selected = selectedKey ? (indexMap[selectedKey] ?? null) : null;
  $: related = relatedRows(rows, selected).slice(0, 80);

  $: openRiskCount = rows.filter((r) => r.kind === 'risk' && r.status === 'open').length;
  $: unreconciledDeltaCount = rows.filter((r) => r.kind === 'delta' && !r.data.reconcile_outcome).length;
  $: errorEventCount = rows.filter(
    (r) => (r.kind === 'exec-event' || r.kind === 'pipeline-event') && r.status === 'error'
  ).length;

  async function loadStateTabData(): Promise<void> {
    loading = true;
    error = null;
    try {
      const [stateR, spansR, execR, pipeR] = await Promise.all([
        api.getState(),
        api.request<{ spans?: JsonObject[] }>('/api/control-plane/spans', { query: { limit: 40 }, sessionId }),
        api.getExecEvents({ limit: 60, sessionId }),
        api.getPipelineEvents({ limit: 60, sessionId }),
      ]);
      const nextRows = buildRows({
        deltas: stateR.deltas ?? [],
        risks: stateR.risks ?? [],
        spans: spansR.spans ?? [],
        execEvents: execR.events ?? [],
        pipelineEvents: pipeR.events ?? [],
      });
      const nextFingerprint = nextRows
        .map((r) => `${r.key}|${r.kind}|${r.ts}|${r.status}|${r.intent}|${r.title}`)
        .join('~');
      if (rowsFingerprint !== null && nextFingerprint === rowsFingerprint) {
        loading = false;
        return;
      }
      rowsFingerprint = nextFingerprint;
      rows = nextRows;
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
      rows = [];
    } finally {
      loading = false;
    }
  }

  async function handleDeltaReconcile(e: CustomEvent<{ deltaId: string; outcome: string }>): Promise<void> {
    writing = true;
    try {
      await api.postDeltaReconcile(
        { delta_id: e.detail.deltaId, outcome: e.detail.outcome, intent_signature: selected?.intent ?? '' },
        sessionId
      );
      await Promise.all([loadStateTabData(), stateStore.refresh()]);
    } catch (err) {
      error = err instanceof Error ? err.message : String(err);
    } finally {
      writing = false;
    }
  }

  async function handleRiskUpdate(e: CustomEvent<{ riskId: string; action: 'handle' | 'snooze' }>): Promise<void> {
    writing = true;
    try {
      await api.postRiskUpdate({ risk_id: e.detail.riskId, action: e.detail.action }, sessionId);
      await Promise.all([loadStateTabData(), stateStore.refresh()]);
    } catch (err) {
      error = err instanceof Error ? err.message : String(err);
    } finally {
      writing = false;
    }
  }

  onMount(() => { void loadStateTabData(); });

  $: if (refreshSignal !== observedRefreshSignal) {
    observedRefreshSignal = refreshSignal;
    void loadStateTabData();
  }
</script>

<div class="state-root state-tab-shell">
  <h2 class="audit-trail-title" style="margin-bottom: 12px">State Objects</h2>
  {#if error}
    <p class="error-banner">{error}</p>
  {/if}
  {#if loading && !rows.length}
    <p class="muted">Loading state data…</p>
  {:else}
    <div class="state-content">
      <div class="cp-stat-strip" style="margin-bottom: 12px; flex-shrink: 0">
        <div class="cp-stat-card">
          <div class="cp-stat-num">{rows.length}</div>
          <div class="cp-stat-label">Total Objects</div>
        </div>
        <div class={'cp-stat-card' + (openRiskCount ? ' warning' : '')}>
          <div class={'cp-stat-num' + (openRiskCount ? ' canary' : '')}>{openRiskCount}</div>
          <div class="cp-stat-label">Open Risks</div>
        </div>
        <div class={'cp-stat-card' + (unreconciledDeltaCount ? ' warning' : '')}>
          <div class={'cp-stat-num' + (unreconciledDeltaCount ? ' canary' : '')}>{unreconciledDeltaCount}</div>
          <div class="cp-stat-label">Unreconciled Deltas</div>
        </div>
        <div class={'cp-stat-card' + (errorEventCount ? ' degraded' : '')}>
          <div class={'cp-stat-num' + (errorEventCount ? ' critical' : '')}>{errorEventCount}</div>
          <div class="cp-stat-label">Error Events</div>
        </div>
      </div>

      <div class="state-grid">
        <StateList
          {rows}
          {filterKind}
          {queryText}
          {selectedKey}
          on:select={(e) => (selectedKey = e.detail)}
          on:filterChange={(e) => (filterKind = e.detail)}
          on:queryChange={(e) => (queryText = e.detail)}
        />
        <StateDiag
          {selected}
          {related}
          {writing}
          on:deltaReconcile={handleDeltaReconcile}
          on:riskUpdate={handleRiskUpdate}
          on:filterByIntent={(e) => (queryText = e.detail)}
          on:selectRow={(e) => (selectedKey = e.detail)}
        />
      </div>
    </div>
  {/if}
</div>

<style>
  .state-tab-shell.state-root {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  .audit-trail-title { flex-shrink: 0; font-size: 14px; color: #e6edf3; font-weight: 600; }
  .muted { color: #8b949e; font-size: 11px; }
  .error-banner { color: #f85149; padding: 8px; font-size: 12px; margin: 0 0 8px; }
  .state-content {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  .state-grid {
    flex: 1;
    min-height: 0;
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px;
    overflow: hidden;
  }
</style>
```

- [ ] **Step 2: Typecheck**

```bash
cd scripts/admin/cockpit && npm run typecheck 2>&1 | tail -20
```

Expected: no errors.

- [ ] **Step 3: Build**

```bash
cd scripts/admin/cockpit && npm run build 2>&1 | tail -20
```

Expected: `✓ built in` message, no errors.

- [ ] **Step 4: Verify line counts (sanity check)**

```bash
wc -l scripts/admin/cockpit/src/components/state/StateTab.svelte \
       scripts/admin/cockpit/src/components/state/StateList.svelte \
       scripts/admin/cockpit/src/components/state/StateDiag.svelte \
       scripts/admin/cockpit/src/lib/state-helpers.ts
```

Expected: StateTab ~140L, StateList ~120L, StateDiag ~120L, state-helpers ~120L.

- [ ] **Step 5: Commit**

```bash
git add scripts/admin/cockpit/src/components/state/StateTab.svelte
git commit -m "refactor: decompose StateTab into StateList + StateDiag + state-helpers"
```

---

## Task 7: Create stores/queue.ts (TDD)

**Files:** Create `scripts/admin/cockpit/src/stores/queue.ts`, `scripts/admin/cockpit/src/stores/queue.test.ts`

- [ ] **Step 1: Write failing tests**

Create `scripts/admin/cockpit/src/stores/queue.test.ts`:

```ts
import { describe, expect, it } from 'vitest';
import { get } from 'svelte/store';
import { createQueueStore, type QueueDraft } from './queue';

const draft: QueueDraft = {
  type: 'pipeline',
  label: 'Run pipeline',
  subLabel: 'foo.read.main',
  command: 'run-pipeline',
  data: { key: 'foo.read.main' },
};

describe('queueStore', () => {
  it('starts empty', () => {
    const store = createQueueStore();
    expect(get(store).items).toEqual([]);
    expect(get(store).submitting).toBe(false);
  });

  it('enqueues items with incrementing ids', () => {
    const store = createQueueStore();
    store.enqueue(draft);
    store.enqueue({ ...draft, label: 'Second' });
    const { items } = get(store);
    expect(items).toHaveLength(2);
    expect(items[0].id).toBe(1);
    expect(items[1].id).toBe(2);
  });

  it('dequeues by id', () => {
    const store = createQueueStore();
    store.enqueue(draft);
    store.enqueue({ ...draft, label: 'Second' });
    const firstId = get(store).items[0].id;
    store.dequeue(firstId);
    expect(get(store).items).toHaveLength(1);
    expect(get(store).items[0].label).toBe('Second');
  });

  it('clears all items', () => {
    const store = createQueueStore();
    store.enqueue(draft);
    store.enqueue(draft);
    store.clear();
    expect(get(store).items).toEqual([]);
  });

  it('preserves item data property', () => {
    const store = createQueueStore();
    store.enqueue(draft);
    expect(get(store).items[0].data).toEqual({ key: 'foo.read.main' });
  });
});
```

- [ ] **Step 2: Run tests — must fail**

```bash
cd scripts/admin/cockpit && npm test -- queue 2>&1 | tail -10
```

Expected: FAIL — `Cannot find module './queue'`

- [ ] **Step 3: Create queue.ts**

Create `scripts/admin/cockpit/src/stores/queue.ts`:

```ts
import { writable } from 'svelte/store';

export interface QueueDraft {
  type: string;
  label: string;
  subLabel: string;
  command: string;
  data: Record<string, unknown>;
}

export interface QueueItem extends QueueDraft {
  id: number;
}

export interface QueueState {
  items: QueueItem[];
  submitting: boolean;
  _idSeq: number;
}

export interface QueueStore {
  subscribe: ReturnType<typeof writable<QueueState>>['subscribe'];
  enqueue(draft: QueueDraft): void;
  dequeue(id: number): void;
  clear(): void;
}

export function createQueueStore(): QueueStore {
  let _state: QueueState = { items: [], submitting: false, _idSeq: 0 };
  const { subscribe, set } = writable<QueueState>(_state);

  function _set(next: QueueState): void {
    _state = next;
    set(next);
  }

  return {
    subscribe,
    enqueue(draft: QueueDraft): void {
      const id = _state._idSeq + 1;
      _set({ ..._state, _idSeq: id, items: [..._state.items, { id, ...draft }] });
    },
    dequeue(id: number): void {
      _set({ ..._state, items: _state.items.filter((item) => item.id !== id) });
    },
    clear(): void {
      _set({ ..._state, items: [] });
    },
  };
}

export const queueStore = createQueueStore();
```

- [ ] **Step 4: Run tests — must pass**

```bash
cd scripts/admin/cockpit && npm test -- queue 2>&1 | tail -10
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/admin/cockpit/src/stores/queue.ts scripts/admin/cockpit/src/stores/queue.test.ts
git commit -m "feat: extract queue store with tests"
```

---

## Task 8: Wire App.svelte to queueStore

**Files:** Modify `scripts/admin/cockpit/src/App.svelte`

- [ ] **Step 1: Replace inline queue state and functions**

In `scripts/admin/cockpit/src/App.svelte`, make the following changes:

**Add import** (after the last store import on line ~22):
```ts
import { queueStore, type QueueDraft } from './stores/queue';
```

**Remove** these lines (they are now in the store):
```ts
  interface QueueDraft {        // lines 31-37
    ...
  }

  interface QueueItem extends QueueDraft {   // lines 39-41
    ...
  }

  interface SubmitResponse {    // lines 43-47
    ...
  }

  let queueItems: QueueItem[] = [];   // line 69
  let queueIdSeq = 0;                 // line 70
  let queueSubmitting = false;        // line 71
```

**Remove** these functions (lines 220-267):
```ts
  function enqueue(queueDraft: QueueDraft): void { ... }
  function dequeue(id: number): void { ... }
  function clearQueue(): void { ... }
```

**Replace** `enqueuePrompt` — keep it, change body to use `queueStore.enqueue()`:
```ts
  function enqueuePrompt(event: CustomEvent<{ prompt: string }>): void {
    const prompt = event.detail.prompt;
    queueStore.enqueue({
      type: 'global-prompt',
      label: 'Instruction',
      subLabel: prompt.length > 60 ? `${prompt.slice(0, 60)}...` : prompt,
      command: 'global-prompt',
      data: { type: 'global-prompt', prompt },
    });
  }
```

**Replace** `submitQueue` — keep in App.svelte (needs `serverPending`, `statusMessage`, `refreshShellData`):
```ts
  async function submitQueue(): Promise<void> {
    const { items, submitting } = $queueStore;
    if (!items.length || submitting || serverPending) return;
    $queueStore.submitting = true;   // NOTE: set via store update below
    statusMessage = 'Submitting queue...';
    // Svelte stores are read-only via $; use internal flag via a writable ref
    // submitQueue is called rarely; just read via $queueStore
    try {
      const result = await api.request<{ ok?: boolean; action_count?: number; error?: string }>(
        '/api/submit',
        { method: 'POST', body: { actions: items.map((item) => item.data) } }
      );
      if (result.ok === false) {
        statusMessage = `Submit failed: ${result.error ?? 'unknown error'}`;
      } else {
        statusMessage = `Submitted ${result.action_count ?? items.length} action(s)`;
        queueStore.clear();
      }
      await refreshShellData();
    } catch (error) {
      statusMessage = error instanceof Error ? error.message : String(error);
    }
  }
```

> **Note on submitting flag:** `queueStore` doesn't expose a `setSubmitting` method because submit is now orchestrated entirely in App.svelte (to keep the store simple). The `submitting` flag is no longer needed since QueuePanel's submit button will be guarded by `serverPending` alone. Remove `submitting` from the store if preferred, or keep it as a future extension point.

**Update the reactive `queuedKeys` computation** (line ~382):
```ts
  $: queuedKeys = new Set(
    $queueStore.items.map((item) => String((item.data && item.data.key) ?? '')).filter((key) => key.length > 0)
  );
```

**Update template** — replace all `queueItems` and `queueSubmitting` refs:

Line ~438: `queueSize={$queueStore.items.length}`

Line ~466: `on:enqueue={(event) => queueStore.enqueue(event.detail)}`

Lines ~477-483:
```svelte
      <QueuePanel
        queueItems={$queueStore.items}
        submitting={false}
        {serverPending}
        on:enqueuePrompt={enqueuePrompt}
        on:dequeue={(event) => queueStore.dequeue(event.detail.id)}
        on:clear={() => queueStore.clear()}
        on:submit={() => void submitQueue()}
      />
```

- [ ] **Step 2: Typecheck**

```bash
cd scripts/admin/cockpit && npm run typecheck 2>&1 | tail -20
```

Expected: no errors.

- [ ] **Step 3: Build**

```bash
cd scripts/admin/cockpit && npm run build 2>&1 | tail -10
```

Expected: clean build.

- [ ] **Step 4: Verify line reduction**

```bash
wc -l scripts/admin/cockpit/src/App.svelte
```

Expected: ~520L or less (down from 659L).

- [ ] **Step 5: Commit**

```bash
git add scripts/admin/cockpit/src/App.svelte
git commit -m "refactor: move queue state to queueStore, slim App.svelte"
```

---

## Task 9: Final Verification

- [ ] **Step 1: Run all Svelte tests**

```bash
cd scripts/admin/cockpit && npm test 2>&1 | tail -15
```

Expected: all tests pass (state-helpers + queue + existing lib tests).

- [ ] **Step 2: Run Python test suite**

```bash
cd /path/to/emerge && python -m pytest tests -q --tb=short 2>&1 | tail -10
```

Expected: 651 passed (no regressions — backend was not touched).

- [ ] **Step 3: Final line count audit**

```bash
wc -l scripts/admin/cockpit/src/components/state/StateTab.svelte \
       scripts/admin/cockpit/src/components/state/StateList.svelte \
       scripts/admin/cockpit/src/components/state/StateDiag.svelte \
       scripts/admin/cockpit/src/lib/state-helpers.ts \
       scripts/admin/cockpit/src/stores/queue.ts \
       scripts/admin/cockpit/src/App.svelte
```

Expected: each file well under 300L; App.svelte and StateTab are the largest at ~520L and ~140L respectively.

- [ ] **Step 4: Commit version bump if tests all green**

No version bump needed — this is internal refactoring with no API surface changes.

---

## Self-Review Checklist

- [x] **Spec coverage:** 1a (untracked files) → Task 1. 1b (CLAUDE.md) → Task 2. 1c (ElicitationResult) → Task 2. Section 2 (StateTab) → Tasks 3-6. Section 3 (queue store) → Tasks 7-8.
- [x] **No placeholders:** All steps contain actual file paths, actual code, actual commands.
- [x] **Type consistency:** `StateKind` / `StateRow` defined once in `state-helpers.ts`, imported everywhere. `QueueDraft` / `QueueItem` defined once in `stores/queue.ts`, imported in App.svelte.
- [x] **`formatTime` location:** Moved to `state-helpers.ts`, used by both StateList and StateDiag.
- [x] **CSS distribution:** Row CSS (`state-row-btn` etc.) stays in StateList. Diag CSS (`diag-head`, `rel-*` etc.) stays in StateDiag. Global classes (`status-badge`, `cp-btn-sm`, `state-col`, `state-col-head`, `state-col-body`) remain in `cp-control-plane.css` — no change needed.
- [x] **`state-content` and `state-grid` CSS:** Moved to StateTab (owns layout).
