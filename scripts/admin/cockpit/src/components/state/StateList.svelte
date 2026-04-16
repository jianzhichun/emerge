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
