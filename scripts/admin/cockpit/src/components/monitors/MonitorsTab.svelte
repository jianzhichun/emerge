<script lang="ts">
  import { get } from 'svelte/store';
  import { onMount } from 'svelte';
  import type { MonitorRunner } from '../../lib/types';
  import { monitorsStore } from '../../stores/monitors';
  import RunnerCard from './RunnerCard.svelte';

  export let refreshSignal = 0;

  const pollIntervalMs = 10_000;

  let pollTimer: ReturnType<typeof setInterval> | null = null;
  let feedLoadingByProfile: Record<string, boolean> = {};
  let refreshInFlight = false;
  let pendingRefresh = false;
  let observedRefreshSignal = refreshSignal;

  function profileOf(runner: MonitorRunner): string {
    return typeof runner.runner_profile === 'string' ? runner.runner_profile : '';
  }

  function setFeedLoading(profile: string, next: boolean): void {
    feedLoadingByProfile = { ...feedLoadingByProfile, [profile]: next };
  }

  async function refreshRunnerEvents(profile: string): Promise<void> {
    if (!profile) {
      return;
    }
    setFeedLoading(profile, true);
    try {
      await monitorsStore.refreshRunnerEvents(profile, 20);
    } catch {
      // feed-level errors are captured in monitorsStore.feedErrorByProfile
    } finally {
      setFeedLoading(profile, false);
    }
  }

  async function refreshMonitorsData(): Promise<void> {
    await monitorsStore.refresh();
    const profiles = get(monitorsStore)
      .runners.map((runner) => profileOf(runner))
      .filter(Boolean);
    await Promise.allSettled(profiles.map((profile) => refreshRunnerEvents(profile)));
  }

  async function queueRefresh(): Promise<void> {
    if (refreshInFlight) {
      pendingRefresh = true;
      return;
    }
    refreshInFlight = true;
    do {
      pendingRefresh = false;
      try {
        await refreshMonitorsData();
      } catch {
        // refresh() already captures error on the store
      }
    } while (pendingRefresh);
    refreshInFlight = false;
  }

  async function handleToggleFeed(profile: string): Promise<void> {
    monitorsStore.toggleFeed(profile);
    const isExpanded = Boolean(get(monitorsStore).expandedFeeds[profile]);
    if (isExpanded) {
      await refreshRunnerEvents(profile);
    }
  }

  function startPolling(): void {
    if (pollTimer) {
      return;
    }
    pollTimer = setInterval(() => {
      void queueRefresh();
    }, pollIntervalMs);
  }

  function stopPolling(): void {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  $: if (refreshSignal !== observedRefreshSignal) {
    observedRefreshSignal = refreshSignal;
    void queueRefresh();
  }

  onMount(() => {
    startPolling();
    void queueRefresh();
    return () => {
      stopPolling();
    };
  });

  $: runnerCount = $monitorsStore.runners.length;
  $: onlineCount = $monitorsStore.runners.filter((runner) => runner.connected !== false).length;
</script>

<section class="monitors-tab">
  <div class="team-status">
    <span class:offline={!$monitorsStore.teamActive || !runnerCount} class="team-dot"></span>
    {#if runnerCount}
      <span class="team-text">Agents team active</span>
      <strong class="team-count">{runnerCount} runner{runnerCount === 1 ? '' : 's'}</strong>
      <span class="team-tail">updated just now</span>
    {:else}
      <span class="team-text">No runners connected</span>
    {/if}
  </div>

  {#if $monitorsStore.error}
    <p class="error-text">Error loading monitors: {$monitorsStore.error}</p>
  {/if}

  {#if !runnerCount && !$monitorsStore.loading}
    <div class="empty-state">
      <div>No runners connected.</div>
      <div class="empty-hint">Run the install script on the target machine - it connects automatically.</div>
    </div>
  {:else}
    <div class="card-grid">
      {#each $monitorsStore.runners as runner}
        {#key runner.runner_profile}
          <RunnerCard
            {runner}
            eventsState={$monitorsStore.recentByProfile[profileOf(runner)] ?? null}
            expanded={Boolean($monitorsStore.expandedFeeds[profileOf(runner)])}
            loadingFeed={Boolean(feedLoadingByProfile[profileOf(runner)])}
            feedError={$monitorsStore.feedErrorByProfile[profileOf(runner)] ?? null}
            on:toggle={(event) => void handleToggleFeed(event.detail.profile)}
          />
        {/key}
      {/each}
    </div>
  {/if}

  <div class="footer">
    <span>{onlineCount} online</span>
    {#if $monitorsStore.lastUpdatedMs}
      <span>Last refresh: {new Date($monitorsStore.lastUpdatedMs).toLocaleTimeString()}</span>
    {/if}
  </div>
</section>

<style>
  .monitors-tab {
    display: flex;
    flex-direction: column;
    gap: 0.8rem;
  }

  .team-status {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    border: 1px solid #21262d;
    border-radius: 0.4rem;
    background: #161b22;
    padding: 0.5rem 0.75rem;
  }

  .team-dot {
    width: 0.38rem;
    height: 0.38rem;
    border-radius: 999px;
    background: #3fb950;
    flex: 0 0 auto;
  }

  .team-dot.offline {
    background: #6e7681;
  }

  .team-text {
    color: #8b949e;
    font-size: 0.68rem;
  }

  .team-count {
    color: #3fb950;
    font-size: 0.68rem;
    font-weight: 600;
  }

  .team-tail {
    margin-left: auto;
    color: #8b949e;
    font-size: 0.64rem;
  }

  .card-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(21rem, 1fr));
    gap: 1rem;
  }

  .empty-state {
    text-align: center;
    color: #6e7681;
    padding: 2.5rem 1rem;
    border: 1px dashed rgba(139, 148, 158, 0.35);
    border-radius: 0.6rem;
  }

  .empty-hint {
    margin-top: 0.45rem;
    font-size: 0.7rem;
  }

  .error-text {
    margin: 0;
    color: #ff8b8b;
  }

  .footer {
    display: flex;
    justify-content: flex-end;
    align-items: center;
    gap: 0.8rem;
    color: #8b949e;
    font-size: 0.64rem;
  }
</style>
