<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '../../lib/api';
  import { stateStore } from '../../stores/state';
  import JsonViewerBlock from '../shared/JsonViewerBlock.svelte';
  import type { JsonObject } from '../../lib/types';

  const STATE_ROW_H = 72;

  export let sessionId: string | undefined;
  export let refreshSignal = 0;

  type StateKind = 'delta' | 'risk' | 'span' | 'exec-event' | 'pipeline-event';

  interface StateRow {
    key: string;
    kind: StateKind;
    ts: number;
    intent: string;
    title: string;
    status: string;
    data: JsonObject;
  }

  let loading = false;
  let error: string | null = null;
  let rows: StateRow[] = [];
  let rowsFingerprint: string | null = null;
  let filterKind: 'all' | StateKind = 'all';
  let queryText = '';
  let selectedKey = '';
  let listBody: HTMLDivElement | null = null;
  let scrollTop = 0;
  let observedRefreshSignal = refreshSignal;
  let writing = false;

  function toText(value: unknown): string {
    if (value === null || value === undefined) {
      return '';
    }
    if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
      return String(value);
    }
    return '';
  }

  function icon(kind: StateKind): string {
    if (kind === 'delta') return 'Δ';
    if (kind === 'risk') return '⚠';
    if (kind === 'span') return '◉';
    if (kind === 'exec-event') return 'E';
    if (kind === 'pipeline-event') return 'P';
    return '•';
  }

  function label(kind: StateKind): string {
    if (kind === 'delta') return 'delta';
    if (kind === 'risk') return 'risk';
    if (kind === 'span') return 'span';
    if (kind === 'exec-event') return 'exec';
    if (kind === 'pipeline-event') return 'pipeline';
    return kind;
  }

  function buildRows(payload: {
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
        data: d
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
        data: r
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
        data: s
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
        data: e
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
        data: e
      });
    });
    out.sort((a, b) => (b.ts || 0) - (a.ts || 0));
    return out;
  }

  function relatedRows(all: StateRow[], selected: StateRow | null): StateRow[] {
    if (!selected) {
      return [];
    }
    const intent = String(selected.intent || '').trim();
    if (intent) {
      return all.filter((r) => String(r.intent || '') === intent);
    }
    const titlePrefix = String(selected.title || '')
      .trim()
      .slice(0, 32)
      .toLowerCase();
    if (!titlePrefix) {
      return [selected];
    }
    return all.filter(
      (r) =>
        r.kind === selected.kind && String(r.title || '').toLowerCase().includes(titlePrefix)
    );
  }

  function statusBadgeCls(status: string): string {
    const s = (status || '').toLowerCase();
    if (s === 'error' || s === 'failure' || s === 'retract') {
      return 'critical';
    }
    if (s === 'ok' || s === 'success' || s === 'handled') {
      return 'stable';
    }
    if (s === 'open') {
      return 'open';
    }
    if (s === 'provisional') {
      return 'provisional';
    }
    if (s === 'snoozed') {
      return 'snoozed';
    }
    return '';
  }

  function rowStatusCls(status: string): string {
    const s = (status || '').toLowerCase();
    if (s === 'error' || s === 'failure' || s === 'retract') {
      return 'critical';
    }
    if (s === 'ok' || s === 'success') {
      return 'stable';
    }
    return '';
  }

  $: indexMap = Object.fromEntries(rows.map((r) => [r.key, r]));
  $: q = queryText.trim().toLowerCase();
  $: filtered = rows.filter((r) => {
    if (filterKind !== 'all' && r.kind !== filterKind) {
      return false;
    }
    if (!q) {
      return true;
    }
    return (
      (r.title || '').toLowerCase().includes(q) ||
      (r.intent || '').toLowerCase().includes(q) ||
      (r.status || '').toLowerCase().includes(q) ||
      (r.kind || '').toLowerCase().includes(q)
    );
  });

  $: if (filtered.length) {
    const still = filtered.some((r) => r.key === selectedKey);
    if (!still) {
      selectedKey = filtered[0].key;
    }
  } else {
    selectedKey = '';
  }

  $: selected = selectedKey ? indexMap[selectedKey] ?? null : null;
  $: related = relatedRows(rows, selected).slice(0, 80);
  $: relatedCounts = {
    delta: related.filter((x) => x.kind === 'delta').length,
    risk: related.filter((x) => x.kind === 'risk').length,
    span: related.filter((x) => x.kind === 'span').length,
    'exec-event': related.filter((x) => x.kind === 'exec-event').length,
    'pipeline-event': related.filter((x) => x.kind === 'pipeline-event').length
  };

  $: countBy = (() => {
    const c = { delta: 0, risk: 0, span: 0, 'exec-event': 0, 'pipeline-event': 0 };
    rows.forEach((r) => {
      if (c[r.kind] != null) {
        c[r.kind] += 1;
      }
    });
    return c;
  })();

  $: openRiskCount = rows.filter((r) => r.kind === 'risk' && r.status === 'open').length;
  $: unreconciledDeltaCount = rows.filter((r) => r.kind === 'delta' && !r.data.reconcile_outcome).length;
  $: errorEventCount = rows.filter(
    (r) => (r.kind === 'exec-event' || r.kind === 'pipeline-event') && r.status === 'error'
  ).length;

  $: total = filtered.length;
  $: viewportH =
    listBody && listBody.clientHeight > 0 ? listBody.clientHeight : STATE_ROW_H * 4;
  $: overscan = 6;
  $: start = Math.max(
    0,
    Math.floor(scrollTop / STATE_ROW_H) - overscan
  );
  $: visibleCount = Math.ceil(viewportH / STATE_ROW_H) + overscan * 2;
  $: end = Math.min(total, start + visibleCount);
  $: visibleRows = filtered.slice(start, end);

  async function loadStateTabData(): Promise<void> {
    loading = true;
    error = null;
    try {
      const [stateR, spansR, execR, pipeR] = await Promise.all([
        api.getState(),
        api.request<{ spans?: JsonObject[] }>('/api/control-plane/spans', {
          query: { limit: 40 },
          sessionId
        }),
        api.getExecEvents({ limit: 60, sessionId }),
        api.getPipelineEvents({ limit: 60, sessionId })
      ]);

      const nextRows = buildRows({
        deltas: stateR.deltas ?? [],
        risks: stateR.risks ?? [],
        spans: spansR.spans ?? [],
        execEvents: execR.events ?? [],
        pipelineEvents: pipeR.events ?? []
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

  async function onDeltaReconcile(deltaId: string, outcome: string): Promise<void> {
    if (outcome === 'retract') {
      const ok = window.confirm(
        'Retract this delta? Verification may degrade. This cannot be trivially undone.'
      );
      if (!ok) {
        return;
      }
    } else if (outcome === 'correct') {
      const ok2 = window.confirm('Mark this delta as correct (human fix)?');
      if (!ok2) {
        return;
      }
    }
    writing = true;
    try {
      await api.postDeltaReconcile(
        {
          delta_id: deltaId,
          outcome,
          intent_signature: toText((selected?.data && selected.data['intent_signature']) ?? '')
        },
        sessionId
      );
      await Promise.all([loadStateTabData(), stateStore.refresh()]);
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      writing = false;
    }
  }

  async function onRiskUpdate(riskId: string, action: 'handle' | 'snooze'): Promise<void> {
    writing = true;
    try {
      await api.postRiskUpdate({ risk_id: riskId, action }, sessionId);
      await Promise.all([loadStateTabData(), stateStore.refresh()]);
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      writing = false;
    }
  }

  function selectKey(key: string): void {
    selectedKey = key;
  }

  function formatTime(ts: number): string {
    if (!ts) {
      return '';
    }
    return new Date(ts).toLocaleTimeString();
  }

  onMount(() => {
    void loadStateTabData();
  });

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

      <div class="state-filter-row">
        <button
          type="button"
          class="cp-btn-sm"
          class:warn={filterKind === 'all'}
          on:click={() => (filterKind = 'all')}>All <span style="opacity: 0.8">{rows.length}</span></button
        >
        <button
          type="button"
          class="cp-btn-sm"
          class:warn={filterKind === 'delta'}
          on:click={() => (filterKind = 'delta')}>Deltas <span style="opacity: 0.8">{countBy.delta}</span></button
        >
        <button
          type="button"
          class="cp-btn-sm"
          class:warn={filterKind === 'risk'}
          on:click={() => (filterKind = 'risk')}>Risks <span style="opacity: 0.8">{countBy.risk}</span></button
        >
        <button
          type="button"
          class="cp-btn-sm"
          class:warn={filterKind === 'span'}
          on:click={() => (filterKind = 'span')}>Spans <span style="opacity: 0.8">{countBy.span}</span></button
        >
        <button
          type="button"
          class="cp-btn-sm"
          class:warn={filterKind === 'exec-event'}
          on:click={() => (filterKind = 'exec-event')}>Exec <span style="opacity: 0.8"
            >{countBy['exec-event']}</span
          ></button
        >
        <button
          type="button"
          class="cp-btn-sm"
          class:warn={filterKind === 'pipeline-event'}
          on:click={() => (filterKind = 'pipeline-event')}
          >Pipeline <span style="opacity: 0.8">{countBy['pipeline-event']}</span></button
        >
        <input
          type="text"
          value={queryText}
          on:input={(e) => (queryText = (e.currentTarget as HTMLInputElement).value)}
          placeholder="Search intent/text/status..."
        />
      </div>

      <div class="state-grid">
        <div class="state-col">
          <div class="state-col-head">List · {filtered.length} item(s)</div>
          {#if !filtered.length}
            <div class="state-col-body"><p class="muted" style="padding: 10px">No matching objects.</p></div>
          {:else}
            <div
              class="state-col-body"
              id="state-list-body"
              bind:this={listBody}
              on:scroll={(e) => {
                scrollTop = (e.currentTarget as HTMLDivElement).scrollTop;
              }}
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
                    on:click={() => selectKey(r.key)}
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
                    <div class="d-line args-line">
                      <b>Args:</b>
                      <code>{toText(selected.data.args_summary)}</code>
                    </div>
                  {:else}
                    <div class="d-line" style="margin-bottom: 10px"></div>
                  {/if}

                  {#if selected.kind === 'delta' && !selected.data.reconcile_outcome}
                    <div class="action-row">
                      <button
                        type="button"
                        class="cp-btn-sm"
                        disabled={writing}
                        on:click={() => onDeltaReconcile(toText(selected.data.id), 'confirm')}>Confirm</button
                      >
                      <button
                        type="button"
                        class="cp-btn-sm warn"
                        disabled={writing}
                        on:click={() => onDeltaReconcile(toText(selected.data.id), 'correct')}>Correct</button
                      >
                      <button
                        type="button"
                        class="cp-btn-sm danger"
                        disabled={writing}
                        on:click={() => onDeltaReconcile(toText(selected.data.id), 'retract')}>Retract</button
                      >
                    </div>
                  {/if}
                  {#if selected.kind === 'risk' && selected.data.status === 'open'}
                    <div class="action-row">
                      <button
                        type="button"
                        class="cp-btn-sm"
                        disabled={writing}
                        on:click={() => onRiskUpdate(toText(selected.data.risk_id), 'handle')}>Handle</button
                      >
                      <button
                        type="button"
                        class="cp-btn-sm"
                        disabled={writing}
                        on:click={() => onRiskUpdate(toText(selected.data.risk_id), 'snooze')}>Snooze</button
                      >
                    </div>
                  {/if}
                  {#if selected.intent}
                    <div class="action-row">
                      <button
                        type="button"
                        class="cp-btn-sm"
                        on:click={() => (queryText = selected.intent)}>Filter by intent</button
                      >
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
                        <button type="button" class="rel-row" on:click={() => selectKey(rel.key)}>
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

                {#key selectedKey}
                  <JsonViewerBlock viewerId="state-detail" data={selected.data} />
                {/key}
              </div>
            {/if}
          </div>
        </div>
      </div>
    </div>
  {/if}
</div>

<style>
  /* Fill .tab-outlet.no-scroll; inner .state-grid scrolls (legacy main-panel.no-scroll). */
  .state-tab-shell.state-root {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  .audit-trail-title {
    flex-shrink: 0;
    font-size: 14px;
    color: #e6edf3;
    font-weight: 600;
  }
  .muted {
    color: #8b949e;
    font-size: 11px;
  }
  .muted.sm {
    font-size: 10px;
    padding: 6px 0;
  }
  .error-banner {
    color: #f85149;
    padding: 8px;
    font-size: 12px;
    margin: 0 0 8px;
  }
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
  .state-row-btn.selected {
    background: #13243a;
    border-color: #388bfd;
  }
  .state-row-line1 {
    display: flex;
    gap: 8px;
    align-items: center;
  }
  .state-ico {
    min-width: 16px;
    color: #79c0ff;
  }
  .state-lbl {
    font-size: 10px;
    color: #8b949e;
    text-transform: uppercase;
    letter-spacing: 0.6px;
    min-width: 56px;
  }
  .state-ts {
    margin-left: auto;
    font-size: 10px;
    color: #6e7681;
  }
  .state-row-title {
    margin-top: 4px;
    color: #c9d1d9;
    font-size: 11px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .state-row-intent {
    margin-top: 2px;
    color: #8b949e;
    font-size: 10px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .diag-head {
    display: flex;
    gap: 8px;
    align-items: center;
    margin-bottom: 8px;
  }
  .d-ico {
    color: #79c0ff;
  }
  .d-kind {
    font-size: 11px;
    color: #e6edf3;
    text-transform: uppercase;
    letter-spacing: 0.6px;
  }
  .d-line {
    font-size: 11px;
    color: #c9d1d9;
    margin-bottom: 6px;
  }
  .args-line code {
    font-size: 10px;
    color: #a5d6ff;
  }
  .action-row {
    display: flex;
    gap: 6px;
    margin-bottom: 10px;
    flex-wrap: wrap;
  }
  .rel-title {
    font-size: 11px;
    color: #8b949e;
    margin-bottom: 6px;
  }
  .rel-counts {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    margin-bottom: 8px;
  }
  .rel-box {
    border: 1px solid #21262d;
    border-radius: 4px;
    background: #11161d;
  }
  .rel-row {
    display: flex;
    width: 100%;
    gap: 8px;
    align-items: center;
    border: none;
    border-bottom: 1px solid #1f2630;
    background: transparent;
    color: #c9d1d9;
    padding: 6px 8px;
    font-family: inherit;
    font-size: 10px;
    cursor: pointer;
    text-align: left;
  }
  .rel-row:last-child {
    border-bottom: none;
  }
  .rel-l {
    min-width: 52px;
    color: #8b949e;
    text-transform: uppercase;
  }
  .rel-t {
    flex: 1;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .rel-time {
    color: #6e7681;
  }
  .status-badge.sm {
    font-size: 9px;
  }
</style>
