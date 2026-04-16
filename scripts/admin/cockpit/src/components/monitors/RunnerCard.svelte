<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import type { MonitorRunner, RunnerEvent, RunnerEventsResponse } from '../../lib/types';
  import EventFeed from './EventFeed.svelte';
  import Sparkline from './Sparkline.svelte';

  export let runner: MonitorRunner;
  export let eventsState: RunnerEventsResponse | null = null;
  export let expanded = false;
  export let loadingFeed = false;
  export let feedError: string | null = null;

  const dispatch = createEventDispatcher<{ toggle: { profile: string } }>();

  function formatAge(tsMs: number | undefined): string {
    if (!tsMs || tsMs <= 0) {
      return '—';
    }
    const seconds = Math.max(0, Math.round((Date.now() - tsMs) / 1000));
    if (seconds < 60) {
      return `${seconds}s ago`;
    }
    if (seconds < 3600) {
      return `${Math.round(seconds / 60)}m ago`;
    }
    return `${Math.round(seconds / 3600)}h ago`;
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

  $: profile = toText(runner.runner_profile);
  $: connected = runner.connected !== false;
  $: connectedAge = formatAge(runner.connected_at_ms);
  $: lastEventAge = formatAge(runner.last_event_ts_ms);
  $: eventList = eventsState?.events ?? [];
  $: previewEvents = eventList.slice(0, 2);
  $: todayEvents = eventsState?.today_events ?? 0;
  $: todayAlerts = eventsState?.today_alerts ?? 0;
  $: hasAlert = Boolean(runner.last_alert);
  $: alertText = hasAlert
    ? `${toText(runner.last_alert?.stage)}: ${toText(runner.last_alert?.intent_signature)}`
    : 'no alerts';
  $: feedToggleLabel = expanded ? '▲ Event feed' : '▼ Event feed';
</script>

<article class:offline={!connected} class="runner-card">
  <header class="card-header">
    <div class="title-row">
      <div class="profile-wrap">
        <span class:offline-dot={!connected} class="status-dot"></span>
        <span class="profile">{profile || 'unknown'}</span>
      </div>
      <span class="age">{connectedAge}</span>
    </div>
    <Sparkline activity={eventsState?.activity ?? []} />
  </header>

  <section class="card-body">
    <div class="stats-grid">
      <div>
        <div class="stat-label">Machine</div>
        <div class="stat-value mono">{toText(runner.machine_id) || '—'}</div>
      </div>
      <div>
        <div class="stat-label">Last Event</div>
        <div class="stat-value">{lastEventAge}</div>
      </div>
      <div>
        <div class="stat-label">Today</div>
        <div class="stat-value">
          {todayEvents} ops · <span class="alert-count">{todayAlerts} alerts</span>
        </div>
      </div>
    </div>

    <div class="alert-line" class:alert-line--none={!hasAlert}>{alertText}</div>

    <div class="recent-box">
      <div class="recent-label">Recent</div>
      {#if previewEvents.length}
        <EventFeed events={previewEvents} />
      {:else}
        <div class="recent-empty">—</div>
      {/if}
    </div>
  </section>

  <button class:expanded class="toggle-feed" type="button" on:click={() => dispatch('toggle', { profile })}>
    {feedToggleLabel}
  </button>

  {#if expanded}
    <EventFeed events={eventList as RunnerEvent[]} loading={loadingFeed} error={feedError} />
  {/if}
</article>

<style>
  .runner-card {
    background: #161b22;
    border: 1px solid #238636;
    border-radius: 0.55rem;
    overflow: hidden;
  }

  .runner-card.offline {
    border-color: #30363d;
  }

  .card-header {
    padding: 0.75rem 0.85rem;
    border-bottom: 1px solid #21262d;
  }

  .title-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 0.5rem;
    gap: 0.6rem;
  }

  .profile-wrap {
    display: flex;
    align-items: center;
    gap: 0.45rem;
  }

  .status-dot {
    width: 0.5rem;
    height: 0.5rem;
    border-radius: 999px;
    background: #3fb950;
    box-shadow: 0 0 5px #3fb95099;
    flex: 0 0 auto;
  }

  .status-dot.offline-dot {
    background: #6e7681;
    box-shadow: none;
  }

  .profile {
    color: #e6edf3;
    font-size: 0.8rem;
    font-weight: 700;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }

  .age {
    color: #8b949e;
    font-size: 0.64rem;
  }

  .card-body {
    padding: 0.75rem 0.85rem;
  }

  .stats-grid {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 0.5rem;
    margin-bottom: 0.6rem;
  }

  .stat-label {
    font-size: 0.56rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: #8b949e;
    margin-bottom: 0.1rem;
  }

  .stat-value {
    font-size: 0.7rem;
    color: #c9d1d9;
  }

  .stat-value.mono {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }

  .alert-count {
    color: #58a6ff;
  }

  .alert-line {
    border: 1px solid #2a4a2a;
    background: #1c2813;
    color: #3fb950;
    border-radius: 999px;
    font-size: 0.64rem;
    padding: 0.2rem 0.55rem;
    margin-bottom: 0.6rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .alert-line--none {
    border: none;
    background: transparent;
    color: #6e7681;
    padding-left: 0;
  }

  .recent-box {
    background: #0d1117;
    border-radius: 0.3rem;
    padding-top: 0.4rem;
    margin-bottom: 0.15rem;
  }

  .recent-label {
    color: #6e7681;
    font-size: 0.56rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    padding: 0 0.6rem 0.35rem;
  }

  .recent-empty {
    color: #6e7681;
    font-size: 0.68rem;
    padding: 0.1rem 0.6rem 0.55rem;
  }

  .toggle-feed {
    width: 100%;
    border: 0;
    border-top: 1px solid #21262d;
    background: transparent;
    color: #8b949e;
    font-size: 0.65rem;
    text-align: left;
    padding: 0.5rem 0.85rem;
    cursor: pointer;
  }

  .toggle-feed:hover,
  .toggle-feed.expanded {
    color: #58a6ff;
  }
</style>
