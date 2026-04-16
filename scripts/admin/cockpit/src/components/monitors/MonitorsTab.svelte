<script lang="ts">
  import { get } from 'svelte/store';
  import { onMount } from 'svelte';
  import { api } from '../../lib/api';
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

  let installBash = '';
  let installPs = '';
  let installError = '';
  let installLoading = false;
  let copiedField: string | null = null;

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

  async function loadInstallUrl(): Promise<void> {
    installLoading = true;
    installError = '';
    try {
      const data = await api.getRunnerInstallUrl();
      if (data.ok) {
        installBash = data.bash ?? '';
        installPs = data.powershell ?? '';
      } else {
        installError = data.error ?? 'Failed to generate install URL';
      }
    } catch (e) {
      installError = e instanceof Error ? e.message : String(e);
    } finally {
      installLoading = false;
    }
  }

  function copyToClipboard(text: string, field: string): void {
    void navigator.clipboard.writeText(text);
    copiedField = field;
    setTimeout(() => {
      if (copiedField === field) copiedField = null;
    }, 1500);
  }

  onMount(() => {
    startPolling();
    void queueRefresh();
    void loadInstallUrl();
    return () => {
      stopPolling();
    };
  });

  $: runnerCount = $monitorsStore.runners.length;
</script>

<section class="monitors-tab monitors-wrap">
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

  <div class="install-panel">
    <div class="install-head">Add Runner</div>
    {#if installLoading}
      <p class="install-hint">Loading install commands…</p>
    {:else if installError}
      <p class="install-hint install-err">{installError}</p>
    {:else if installBash}
      <div class="install-row">
        <span class="install-label">Linux / macOS</span>
        <code class="install-cmd">{installBash}</code>
        <button type="button" class="cp-btn-sm" on:click={() => copyToClipboard(installBash, 'bash')}>
          {copiedField === 'bash' ? 'Copied' : 'Copy'}
        </button>
      </div>
      {#if installPs}
        <div class="install-row">
          <span class="install-label">Windows</span>
          <code class="install-cmd">{installPs}</code>
          <button type="button" class="cp-btn-sm" on:click={() => copyToClipboard(installPs, 'ps')}>
            {copiedField === 'ps' ? 'Copied' : 'Copy'}
          </button>
        </div>
      {/if}
      <p class="install-hint">Paste in the target machine's terminal. The runner connects automatically.</p>
    {/if}
  </div>

  {#if !runnerCount && !$monitorsStore.loading}
    <div class="empty-state">
      <div>No runners connected yet.</div>
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

  .install-panel {
    border: 1px solid #21262d;
    border-radius: 0.4rem;
    background: #161b22;
    padding: 0.6rem 0.75rem;
  }

  .install-head {
    font-size: 0.72rem;
    font-weight: 600;
    color: #e6edf3;
    margin-bottom: 0.5rem;
  }

  .install-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 0.4rem;
  }

  .install-label {
    font-size: 0.64rem;
    color: #8b949e;
    min-width: 6.5rem;
    flex-shrink: 0;
  }

  .install-cmd {
    flex: 1;
    font-size: 0.64rem;
    color: #79c0ff;
    background: #0d1117;
    border: 1px solid #21262d;
    border-radius: 4px;
    padding: 0.3rem 0.5rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    min-width: 0;
  }

  .install-hint {
    font-size: 0.62rem;
    color: #484f58;
    margin: 0.3rem 0 0;
  }

  .install-err {
    color: #f85149;
  }

  .empty-state {
    text-align: center;
    color: #484f58;
    padding: 2.5rem 1rem;
  }

  .error-text {
    margin: 0;
    color: #ff8b8b;
  }
</style>
