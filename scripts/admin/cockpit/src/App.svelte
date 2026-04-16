<script lang="ts">
  import { onMount } from 'svelte';
  import AuditTab from './components/audit/AuditTab.svelte';
  import ConnectorTab from './components/connector/ConnectorTab.svelte';
  import MonitorsTab from './components/monitors/MonitorsTab.svelte';
  import OverviewTab from './components/overview/OverviewTab.svelte';
  import SessionTab from './components/session/SessionTab.svelte';
  import StateTab from './components/state/StateTab.svelte';
  import Badge from './components/shared/Badge.svelte';
  import GoalBar from './components/shared/GoalBar.svelte';
  import SettingsModal from './components/shared/SettingsModal.svelte';
  import StatusDot from './components/shared/StatusDot.svelte';
  import TabBar from './components/shared/TabBar.svelte';
  import ThresholdsBar from './components/shared/ThresholdsBar.svelte';
  import { api } from './lib/api';
  import { navigate, readRouteFromUrl } from './lib/router';
  import { createSseClient } from './lib/sse';
  import type { AssetConnector } from './lib/types';
  import { goalStore } from './stores/goal';
  import { monitorsStore } from './stores/monitors';
  import { policyStore } from './stores/policy';
  import { sessionStore } from './stores/session';
  import { stateStore } from './stores/state';
  import { uiStore } from './stores/ui';

  interface TabBarItem {
    id: string;
    label: string;
  }

  interface QueueItem {
    id: number;
    type: string;
    label: string;
    subLabel: string;
    command: string;
    data: Record<string, unknown>;
  }

  interface QueueDraft {
    type: string;
    label: string;
    subLabel: string;
    command: string;
    data: Record<string, unknown>;
  }

  interface SubmitResponse {
    ok?: boolean;
    action_count?: number;
    error?: string;
  }

  type GlobalTabId = 'overview' | 'monitors' | 'audit' | 'session' | 'state';
  type ConnectorPanelId = 'pipelines' | 'notes' | 'controls';

  const globalTabs: { id: GlobalTabId; label: string }[] = [
    { id: 'overview', label: 'Overview' },
    { id: 'monitors', label: 'Monitors' },
    { id: 'audit', label: 'Audit' },
    { id: 'session', label: 'Session' },
    { id: 'state', label: 'State' }
  ];

  let activeTab = 'overview';
  let connectorAssets: Record<string, AssetConnector> = {};
  let connectorPanelByTab: Record<string, ConnectorPanelId> = {};
  let assetsLoading = false;
  let assetsError: string | null = null;

  let monitorsRefreshSignal = 0;
  let auditRefreshSignal = 0;
  let queueItems: QueueItem[] = [];
  let queueIdSeq = 0;
  let queueSubmitting = false;
  let serverPending = false;
  let statusMessage: string | null = null;

  function isGlobalTab(tab: string): tab is GlobalTabId {
    return globalTabs.some((item) => item.id === tab);
  }

  function connectorNamesFromAssets(): string[] {
    return Object.keys(connectorAssets).sort();
  }

  function isConnectorTab(tab: string): boolean {
    return connectorNamesFromAssets().includes(tab);
  }

  function toConnectorPanel(panel: string | undefined): ConnectorPanelId {
    if (panel === 'notes' || panel === 'controls') {
      return panel;
    }
    return 'pipelines';
  }

  function selectedConnectorPanel(tabId: string, routePanel?: string): ConnectorPanelId {
    if (connectorPanelByTab[tabId]) {
      return connectorPanelByTab[tabId];
    }
    return toConnectorPanel(routePanel);
  }

  function resolveTab(tabId: string | undefined, connectorNames: string[]): string {
    if (!tabId) {
      return 'overview';
    }
    if (isGlobalTab(tabId) || connectorNames.includes(tabId)) {
      return tabId;
    }
    return 'overview';
  }

  function syncTabFromUrl(): void {
    const route = readRouteFromUrl();
    const connectorNames = connectorNamesFromAssets();
    const resolved = resolveTab(route.tab, connectorNames);
    activeTab = resolved;
    if (!isGlobalTab(resolved) && route.panel) {
      connectorPanelByTab = {
        ...connectorPanelByTab,
        [resolved]: toConnectorPanel(route.panel)
      };
    }
  }

  function handleTabSelect(event: CustomEvent<{ id: string }>): void {
    const tabId = event.detail.id;
    if (!isGlobalTab(tabId) && !isConnectorTab(tabId)) {
      return;
    }
    activeTab = tabId;
    const currentRoute = readRouteFromUrl();
    navigate(
      {
        tab: tabId,
        session: currentRoute.session,
        panel: isGlobalTab(tabId) ? undefined : selectedConnectorPanel(tabId, currentRoute.panel)
      },
      { replace: true }
    );
  }

  function openSettingsModal(): void {
    uiStore.setModal('settings');
  }

  function closeSettingsModal(): void {
    uiStore.setModal(null);
  }

  async function refreshStatus(): Promise<void> {
    try {
      const response = await api.getStatus();
      serverPending = Boolean(response.pending);
    } catch {
      serverPending = false;
    }
  }

  async function refreshAssets(): Promise<void> {
    assetsLoading = true;
    assetsError = null;
    try {
      const payload = await api.getAssets();
      connectorAssets = payload.connectors ?? {};
    } catch (error) {
      assetsError = error instanceof Error ? error.message : String(error);
      connectorAssets = {};
    } finally {
      assetsLoading = false;
    }
  }

  async function refreshShellData(): Promise<void> {
    const route = readRouteFromUrl();
    const routeSession = route.session;
    await Promise.allSettled([
      policyStore.refresh(routeSession),
      monitorsStore.refresh(),
      sessionStore.refresh(routeSession),
      goalStore.refresh(),
      stateStore.refresh(),
      refreshAssets(),
      refreshStatus()
    ]);

    syncTabFromUrl();
    const connectorNames = connectorNamesFromAssets();
    const currentRoute = readRouteFromUrl();
    if (!isGlobalTab(currentRoute.tab) && !connectorNames.includes(currentRoute.tab)) {
      activeTab = 'overview';
      navigate({ tab: 'overview', session: currentRoute.session }, { replace: true });
    }
    auditRefreshSignal += 1;
  }

  function queueMonitorsRefresh(): void {
    monitorsRefreshSignal += 1;
  }

  function enqueue(queueDraft: QueueDraft): void {
    queueIdSeq += 1;
    queueItems = [...queueItems, { id: queueIdSeq, ...queueDraft }];
  }

  function dequeue(id: number): void {
    queueItems = queueItems.filter((item) => item.id !== id);
  }

  function clearQueue(): void {
    queueItems = [];
  }

  async function submitQueue(): Promise<void> {
    if (!queueItems.length || queueSubmitting || serverPending) {
      return;
    }
    queueSubmitting = true;
    statusMessage = 'Submitting queue...';
    try {
      const result = await api.request<SubmitResponse>('/api/submit', {
        method: 'POST',
        body: { actions: queueItems.map((item) => item.data) }
      });
      if (result.ok === false) {
        statusMessage = `Submit failed: ${result.error ?? 'unknown error'}`;
      } else {
        statusMessage = `Submitted ${result.action_count ?? queueItems.length} action(s)`;
        queueItems = [];
      }
      await refreshShellData();
    } catch (error) {
      statusMessage = error instanceof Error ? error.message : String(error);
    } finally {
      queueSubmitting = false;
    }
  }

  function handleSessionSelect(event: CustomEvent<{ sessionId: string }>): void {
    const sessionId = event.detail.sessionId.trim() || undefined;
    const route = readRouteFromUrl();
    navigate(
      {
        tab: activeTab,
        session: sessionId,
        panel: isGlobalTab(activeTab) ? undefined : selectedConnectorPanel(activeTab, route.panel)
      },
      { replace: true }
    );
    void refreshShellData();
  }

  function handleSessionRefreshRequested(): void {
    void refreshShellData();
  }

  function handleSessionNotify(event: CustomEvent<{ message: string }>): void {
    statusMessage = event.detail.message;
  }

  function handleConnectorPanelSelect(event: CustomEvent<{ panel: ConnectorPanelId }>): void {
    if (isGlobalTab(activeTab)) {
      return;
    }
    const panel = event.detail.panel;
    connectorPanelByTab = {
      ...connectorPanelByTab,
      [activeTab]: panel
    };
    const route = readRouteFromUrl();
    navigate(
      {
        tab: activeTab,
        session: route.session,
        panel
      },
      { replace: true }
    );
  }

  onMount(() => {
    syncTabFromUrl();
    void refreshShellData();
    const statusInterval = setInterval(() => {
      void refreshStatus();
    }, 10_000);
    const sse = createSseClient<Record<string, unknown>>({
      onMessage: (message) => {
        const payload = message.data;
        if (!payload || typeof payload !== 'object') {
          return;
        }
        if ('pending' in payload) {
          serverPending = Boolean((payload as { pending?: unknown }).pending);
        }
        if ('monitors_updated' in payload && Boolean((payload as { monitors_updated?: unknown }).monitors_updated)) {
          queueMonitorsRefresh();
        }
      }
    });
    sse.start();
    const onPopState = () => syncTabFromUrl();
    window.addEventListener('popstate', onPopState);
    return () => {
      clearInterval(statusInterval);
      window.removeEventListener('popstate', onPopState);
      sse.stop();
    };
  });

  $: connectorNames = connectorNamesFromAssets();
  $: tabs = [...globalTabs, ...connectorNames.map((name) => ({ id: name, label: name }))] as TabBarItem[];
  $: activeTabLabel = isGlobalTab(activeTab) ? (tabs.find((tab) => tab.id === activeTab)?.label ?? 'Overview') : `Connector: ${activeTab}`;
  $: monitorsOnlineCount = $monitorsStore.runners.filter((runner) => runner.connected !== false).length;
  $: shellLoading =
    $policyStore.loading || $monitorsStore.loading || $sessionStore.loading || $goalStore.loading || $stateStore.loading || assetsLoading;
  $: shellError =
    $policyStore.error ?? $monitorsStore.error ?? $sessionStore.error ?? $goalStore.error ?? $stateStore.error ?? assetsError;
  $: routeSessionId = readRouteFromUrl().session ?? $sessionStore.currentSessionId ?? null;
  $: activeConnectorPanel = isGlobalTab(activeTab) ? 'pipelines' : selectedConnectorPanel(activeTab, readRouteFromUrl().panel);
  $: activeConnector = isGlobalTab(activeTab) ? null : (connectorAssets[activeTab] ?? null);
  $: activeConnectorPipelines = isGlobalTab(activeTab)
    ? []
    : $policyStore.pipelines.filter((pipeline) => String(pipeline.key ?? '').startsWith(`${activeTab}.`));
</script>

<main class="app">
  <header class="app-header">
    <div>
      <h1>Cockpit</h1>
      <p class="subtitle">Svelte shell scaffold for control-plane tabs</p>
    </div>
    <div class="status-line">
      <StatusDot status={$monitorsStore.teamActive ? 'online' : 'offline'} label={$monitorsStore.teamActive ? 'Monitors active' : 'Monitors idle'} />
      <Badge label={`${monitorsOnlineCount} runner(s) online`} variant={monitorsOnlineCount > 0 ? 'success' : 'neutral'} />
    </div>
  </header>

  <TabBar tabs={tabs} activeTab={activeTab} on:select={handleTabSelect} />

  <section class="shared-meta">
    <GoalBar />
    <ThresholdsBar thresholds={$policyStore.thresholds} on:edit={openSettingsModal} />
  </section>

  <section class="tab-outlet" aria-label="Active tab placeholder">
    <header class="tab-outlet-header">
      <h2>{activeTabLabel}</h2>
      <Badge label={shellLoading ? 'Refreshing' : 'Ready'} variant={shellLoading ? 'info' : 'neutral'} />
    </header>
    {#if activeTab === 'overview'}
      <OverviewTab
        pipelines={$policyStore.pipelines}
        thresholds={$policyStore.thresholds}
        {queueItems}
        {queueSubmitting}
        {serverPending}
        on:enqueue={(event) => enqueue(event.detail)}
        on:dequeue={(event) => dequeue(event.detail.id)}
        on:clearQueue={clearQueue}
        on:submitQueue={() => void submitQueue()}
      />
    {:else if activeTab === 'monitors'}
      <MonitorsTab refreshSignal={monitorsRefreshSignal} />
    {:else if activeTab === 'audit'}
      <AuditTab sessionId={routeSessionId ?? undefined} refreshSignal={auditRefreshSignal} />
    {:else if activeTab === 'session'}
      <SessionTab
        sessions={$sessionStore.sessions}
        selectedSessionId={routeSessionId}
        session={$sessionStore.session}
        hookState={$sessionStore.hookState}
        loading={$sessionStore.loading}
        error={$sessionStore.error}
        on:selectSession={handleSessionSelect}
        on:refreshRequested={handleSessionRefreshRequested}
        on:notify={handleSessionNotify}
      />
    {:else if activeTab === 'state'}
      <StateTab
        deltas={$stateStore.deltas}
        risks={$stateStore.risks}
        verificationState={$stateStore.verificationState}
        activeSpanId={$stateStore.activeSpanId}
        activeSpanIntent={$stateStore.activeSpanIntent}
        loading={$stateStore.loading}
        error={$stateStore.error}
      />
    {:else if isConnectorTab(activeTab)}
      <ConnectorTab
        connectorName={activeTab}
        connector={activeConnector}
        pipelines={activeConnectorPipelines}
        selectedPanel={activeConnectorPanel}
        on:selectPanel={handleConnectorPanelSelect}
      />
    {:else}
      <p>This tab is not available.</p>
      {#if shellError}
        <p class="error-text">{shellError}</p>
      {/if}
    {/if}
    {#if statusMessage}
      <p class="status-text">{statusMessage}</p>
    {/if}
  </section>

  <SettingsModal
    open={$uiStore.activeModal === 'settings'}
    thresholds={$policyStore.thresholds}
    on:close={closeSettingsModal}
    on:saved={() => void policyStore.refresh(readRouteFromUrl().session)}
  />
</main>

<style>
  .app {
    max-width: 68rem;
    margin: 0 auto;
    padding: 1.25rem 1.25rem 2rem;
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  .app-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 0.75rem;
  }

  h1 {
    margin: 0;
    font-size: 1.6rem;
    line-height: 1.2;
  }

  .subtitle {
    margin: 0.25rem 0 0;
    color: var(--color-text-muted);
    font-size: 0.9rem;
  }

  .status-line {
    display: inline-flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
    justify-content: flex-end;
  }

  .shared-meta {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }

  .tab-outlet {
    min-height: 12rem;
    border: 1px dashed color-mix(in srgb, var(--color-text-muted) 65%, transparent);
    border-radius: 0.75rem;
    padding: 0.9rem;
    background: color-mix(in srgb, var(--color-bg) 90%, black);
  }

  .tab-outlet-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 0.6rem;
  }

  .tab-outlet h2 {
    margin: 0;
    font-size: 1.15rem;
  }

  .tab-outlet p {
    margin: 0.7rem 0 0;
  }

  .error-text {
    color: #ff9e9e;
  }

  .status-text {
    color: #8fd4ff;
    font-size: 0.82rem;
  }
</style>
