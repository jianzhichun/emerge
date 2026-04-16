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
