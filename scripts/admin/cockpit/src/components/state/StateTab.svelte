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
