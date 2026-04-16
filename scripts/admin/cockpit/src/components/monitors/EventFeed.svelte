<script lang="ts">
  import type { RunnerEvent } from '../../lib/types';

  export let events: RunnerEvent[] = [];
  export let loading = false;
  export let error: string | null = null;

  interface EventRowView {
    ageLabel: string;
    badgeLabel: string;
    badgeClass: string;
    contentClass: string;
    contentText: string;
  }

  function ageFromTs(tsMs: number | undefined, nowMs: number): string {
    if (!tsMs || tsMs <= 0) {
      return '0s';
    }
    const ageSeconds = Math.max(0, Math.round((nowMs - tsMs) / 1000));
    if (ageSeconds < 60) {
      return `${ageSeconds}s`;
    }
    if (ageSeconds < 3600) {
      return `${Math.round(ageSeconds / 60)}m`;
    }
    return `${Math.round(ageSeconds / 3600)}h`;
  }

  function toText(value: unknown): string {
    if (typeof value === 'string') {
      return value;
    }
    if (typeof value === 'number' || typeof value === 'boolean') {
      return String(value);
    }
    return '';
  }

  function eventToView(event: RunnerEvent, nowMs: number): EventRowView {
    const type = toText(event.type) || 'event';
    if (type === 'pattern_alert') {
      const intent = toText(event.intent_signature);
      const stage = toText(event.stage);
      return {
        ageLabel: ageFromTs(event.ts_ms, nowMs),
        badgeLabel: 'pattern',
        badgeClass: 'badge--pattern',
        contentClass: 'content--pattern',
        contentText: [intent, stage].filter(Boolean).join(' · ') || 'pattern alert'
      };
    }
    if (type === 'operator_message') {
      return {
        ageLabel: ageFromTs(event.ts_ms, nowMs),
        badgeLabel: 'operator',
        badgeClass: 'badge--operator',
        contentClass: 'content--operator',
        contentText: toText(event.text) || 'operator message'
      };
    }
    if (type === 'runner_online') {
      const machineId = toText(event.machine_id);
      return {
        ageLabel: ageFromTs(event.ts_ms, nowMs),
        badgeLabel: 'online',
        badgeClass: 'badge--online',
        contentClass: 'content--muted',
        contentText: machineId ? `runner connected · ${machineId}` : 'runner connected'
      };
    }
    return {
      ageLabel: ageFromTs(event.ts_ms, nowMs),
      badgeLabel: 'event',
      badgeClass: 'badge--event',
      contentClass: 'content--muted',
      contentText: type
    };
  }

  $: nowMs = Date.now();
  $: rows = events.map((event) => eventToView(event, nowMs));
</script>

{#if error}
  <div class="error-line">{error}</div>
{:else if loading}
  <div class="empty-line">Loading...</div>
{:else if !rows.length}
  <div class="empty-line">No events.</div>
{:else}
  <div class="event-feed">
    {#each rows as row}
      <div class="event-row">
        <span class="age">{row.ageLabel}</span>
        <span class={`badge ${row.badgeClass}`}>{row.badgeLabel}</span>
        <span class={`content ${row.contentClass}`} title={row.contentText}>{row.contentText}</span>
      </div>
    {/each}
  </div>
{/if}

<style>
  .event-feed {
    max-height: 12.5rem;
    overflow-y: auto;
    padding: 0 0.9rem 0.65rem;
  }

  .event-row {
    display: flex;
    align-items: flex-start;
    gap: 0.45rem;
    border-bottom: 1px solid rgba(139, 148, 158, 0.14);
    padding: 0.36rem 0;
    font-size: 0.68rem;
  }

  .age {
    min-width: 1.7rem;
    color: #6e7681;
    flex: 0 0 auto;
  }

  .badge {
    border-radius: 0.16rem;
    border: 1px solid transparent;
    padding: 0 0.3rem;
    font-size: 0.56rem;
    line-height: 1.2;
    white-space: nowrap;
    text-transform: lowercase;
    flex: 0 0 auto;
  }

  .badge--pattern {
    color: #f0883e;
    background: #2d1b00;
    border-color: #6f3d00;
  }

  .badge--operator {
    color: #58a6ff;
    background: #0d2233;
    border-color: rgba(31, 111, 235, 0.3);
  }

  .badge--online {
    color: #3fb950;
    background: #1c2813;
    border-color: #2a4a2a;
  }

  .badge--event {
    color: #8b949e;
    background: #161b22;
    border-color: #30363d;
  }

  .content {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .content--pattern {
    color: #8b949e;
  }

  .content--operator {
    color: #58a6ff;
  }

  .content--muted {
    color: #6e7681;
  }

  .empty-line {
    padding: 0.5rem 0.9rem 0.65rem;
    color: #6e7681;
    font-size: 0.68rem;
  }

  .error-line {
    padding: 0.5rem 0.9rem 0.65rem;
    color: #ff8b8b;
    font-size: 0.68rem;
  }
</style>
