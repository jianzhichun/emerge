<script lang="ts">
  import { onMount } from 'svelte';
  import AuditTab from './components/audit/AuditTab.svelte';
  import ConnectorTab from './components/connector/ConnectorTab.svelte';
  import MonitorsTab from './components/monitors/MonitorsTab.svelte';
  import OverviewTab from './components/overview/OverviewTab.svelte';
  import QueuePanel from './components/overview/QueuePanel.svelte';
  import SessionTab from './components/session/SessionTab.svelte';
  import StateTab from './components/state/StateTab.svelte';
  import CockpitDropdown from './components/shared/CockpitDropdown.svelte';
  import SettingsModal from './components/shared/SettingsModal.svelte';
  import TabBar from './components/shared/TabBar.svelte';
  import ThresholdsBar from './components/shared/ThresholdsBar.svelte';
  import { api } from './lib/api';
  import { navigate, readRouteFromUrl } from './lib/router';
  import { createSseClient } from './lib/sse';
  import type { AssetConnector } from './lib/types';
  import { monitorsStore } from './stores/monitors';
  import { policyStore } from './stores/policy';
  import { sessionStore } from './stores/session';
  import { stateStore } from './stores/state';
  import { uiStore } from './stores/ui';

  interface TabBarItem {
    id: string;
    label: string;
    warn?: boolean;
    subtle?: boolean;
  }

  interface QueueDraft {
    type: string;
    label: string;
    subLabel: string;
    command: string;
    data: Record<string, unknown>;
  }

  interface QueueItem extends QueueDraft {
    id: number;
  }

  interface SubmitResponse {
    ok?: boolean;
    action_count?: number;
    error?: string;
  }

  type GlobalTabId = 'overview' | 'monitors' | 'audit' | 'session' | 'state';
  type ConnectorPanelId = 'pipelines' | 'notes' | 'controls';
  const FALLBACK_REFRESH_MS = 30_000;

  const primaryTab: { id: GlobalTabId; label: string } = { id: 'overview', label: 'Overview' };
  const controlTabs: { id: Exclude<GlobalTabId, 'overview'>; label: string }[] = [
    { id: 'monitors', label: 'Monitors' },
    { id: 'audit', label: 'Audit' },
    { id: 'session', label: 'Session' },
    { id: 'state', label: 'State' }
  ];

  let activeTab = 'overview';
  let connectorAssets: Record<string, AssetConnector> = {};
  let connectorPanelByTab: Record<string, ConnectorPanelId> = {};
  let assetsError: string | null = null;

  let monitorsRefreshSignal = 0;
  let auditRefreshSignal = 0;
  let stateRefreshSignal = 0;
  let queueItems: QueueItem[] = [];
  let queueIdSeq = 0;
  let queueSubmitting = false;
  let serverPending = false;
  let ccActive = false;
  let statusMessage: string | null = null;
  let sseStatus = 'idle';

  function isGlobalTab(tab: string): tab is GlobalTabId {
    return tab === 'overview' || controlTabs.some((item) => item.id === tab);
  }

  function connectorNamesFromData(
    assets: Record<string, AssetConnector>,
    pipelines: Array<{ key?: string }>
  ): string[] {
    const assetNames = Object.keys(assets);
    const policyNames = pipelines.map((pipeline) => String(pipeline.key ?? '').split('.')[0] || '').filter((name) => name.length > 0);
    return Array.from(new Set([...assetNames, ...policyNames])).sort();
  }

  function isConnectorTab(tab: string): boolean {
    return connectorNames.includes(tab);
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

  function handleOverviewConnectorOpen(event: CustomEvent<{ id: string }>): void {
    const connectorId = event.detail.id;
    if (!isConnectorTab(connectorId)) {
      return;
    }
    const currentRoute = readRouteFromUrl();
    activeTab = connectorId;
    navigate(
      {
        tab: connectorId,
        session: currentRoute.session,
        panel: selectedConnectorPanel(connectorId, currentRoute.panel)
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
      ccActive = Boolean(response.cc_active);
    } catch {
      serverPending = false;
      ccActive = false;
    }
  }

  async function refreshAssets(): Promise<void> {
    assetsError = null;
    try {
      const payload = await api.getAssets();
      connectorAssets = payload.connectors ?? {};
    } catch (error) {
      assetsError = error instanceof Error ? error.message : String(error);
      connectorAssets = {};
    }
  }

  async function refreshShellData(): Promise<void> {
    const route = readRouteFromUrl();
    const routeSession = route.session;
    await Promise.allSettled([
      policyStore.refresh(routeSession),
      monitorsStore.refresh(),
      sessionStore.refresh(routeSession),
      stateStore.refresh(),
      refreshAssets(),
      refreshStatus()
    ]);

    syncTabFromUrl();
    const currentRoute = readRouteFromUrl();
    if (!isGlobalTab(currentRoute.tab) && !connectorNames.includes(currentRoute.tab)) {
      activeTab = 'overview';
      navigate({ tab: 'overview', session: currentRoute.session }, { replace: true });
    }
    auditRefreshSignal += 1;
    stateRefreshSignal += 1;
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

  function enqueuePrompt(event: CustomEvent<{ prompt: string }>): void {
    const prompt = event.detail.prompt;
    enqueue({
      type: 'global-prompt',
      label: 'Instruction',
      subLabel: prompt.length > 60 ? `${prompt.slice(0, 60)}...` : prompt,
      command: 'global-prompt',
      data: { type: 'global-prompt', prompt }
    });
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

  function switchSession(sessionId?: string): void {
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

  function handleSessionDropdownChange(event: CustomEvent<{ value: string }>): void {
    const sessionId = event.detail.value.trim() || undefined;
    switchSession(sessionId);
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
    const fallbackInterval = setInterval(() => {
      void refreshShellData();
    }, FALLBACK_REFRESH_MS);
    const sse = createSseClient<Record<string, unknown>>({
      onStatus: (status) => {
        sseStatus = status;
      },
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
        if ('data_updated' in payload && Boolean((payload as { data_updated?: unknown }).data_updated)) {
          const route = readRouteFromUrl();
          void policyStore.refresh(route.session);
          void stateStore.refresh();
          auditRefreshSignal += 1;
          stateRefreshSignal += 1;
        }
      }
    });
    sse.start();
    const onPopState = () => syncTabFromUrl();
    window.addEventListener('popstate', onPopState);
    return () => {
      clearInterval(fallbackInterval);
      window.removeEventListener('popstate', onPopState);
      sse.stop();
    };
  });

  $: connectorNames = connectorNamesFromData(connectorAssets, $policyStore.pipelines);
  $: rollbackThreshold = Number($policyStore.thresholds?.rollback_consecutive_failures ?? 2);
  $: leftTabs = [
    primaryTab,
    ...connectorNames.map((name) => {
      const hasCritical = $policyStore.pipelines.some(
        (pipeline) => String(pipeline.key ?? '').startsWith(`${name}.`) && Number(pipeline.consecutive_failures ?? 0) >= rollbackThreshold
      );
      return {
        id: name,
        label: name,
        warn: hasCritical
      } as TabBarItem;
    })
  ];
  $: rightTabs = controlTabs.map((item) => ({ ...item, subtle: true }));
  $: shellError =
    $policyStore.error ?? $monitorsStore.error ?? $sessionStore.error ?? $stateStore.error ?? assetsError;
  $: routeSessionId = readRouteFromUrl().session ?? $sessionStore.currentSessionId ?? null;
  $: connectorMode = !isGlobalTab(activeTab) && isConnectorTab(activeTab);
  $: showQueuePanel = activeTab === 'overview' || isConnectorTab(activeTab);
  $: activeConnectorPanel = isGlobalTab(activeTab) ? 'pipelines' : selectedConnectorPanel(activeTab, readRouteFromUrl().panel);
  $: activeConnector = isGlobalTab(activeTab) ? null : (connectorAssets[activeTab] ?? null);
  $: activeConnectorPipelines = isGlobalTab(activeTab)
    ? []
    : $policyStore.pipelines.filter((pipeline) => String(pipeline.key ?? '').startsWith(`${activeTab}.`));
  $: queuedKeys = new Set(
    queueItems.map((item) => String((item.data && item.data.key) ?? '')).filter((key) => key.length > 0)
  );
  $: daemonOnline = sseStatus === 'connected';
  $: ccIndicatorText = !daemonOnline
    ? '◐ Daemon offline'
    : ccActive
      ? '● CC connected'
      : '○ CC idle';

  $: sessionDropdownOptions = [
    { value: '', label: '(default/current)' },
    ...$sessionStore.sessions.map((row) => ({
      value: String(row.session_id ?? ''),
      label: String(row.session_id ?? '').slice(0, 44) || '(unknown)'
    }))
  ];
</script>

<main class="app">
  <header class="app-header">
    <h1>🌀 Emerge Cockpit</h1>
    <div class="header-session">
      <span class="session-label" id="cockpit-session-label">Session</span>
      <CockpitDropdown
        dropdownId="cockpit-session-dropdown"
        labelledBy="cockpit-session-label"
        ariaLabel="Control-plane session"
        title="Select control-plane session"
        options={sessionDropdownOptions}
        value={routeSessionId ?? ''}
        minWidth="180px"
        maxWidth="min(360px, 38vw)"
        emptyMenuLabel="(no sessions)"
        on:change={handleSessionDropdownChange}
      />
    </div>
  </header>

  <section class="shared-meta">
    <ThresholdsBar thresholds={$policyStore.thresholds} on:edit={openSettingsModal} />
  </section>
  <div class="shell-tabs">
    <TabBar {leftTabs} {rightTabs} activeTab={activeTab} on:select={handleTabSelect} />
  </div>

  <section class="main-layout">
    <section
      class={`tab-outlet ${connectorMode ? 'connector-mode' : ''}`}
      class:no-scroll={activeTab === 'state'}
      aria-label="Active tab placeholder"
    >
      {#if activeTab === 'overview'}
        <OverviewTab
          pipelines={$policyStore.pipelines}
          thresholds={$policyStore.thresholds}
          connectorNames={connectorNames}
          queueSize={queueItems.length}
          on:openConnector={handleOverviewConnectorOpen}
        />
      {:else if activeTab === 'monitors'}
        <MonitorsTab refreshSignal={monitorsRefreshSignal} />
      {:else if activeTab === 'audit'}
        <AuditTab sessionId={routeSessionId ?? undefined} refreshSignal={auditRefreshSignal} />
      {:else if activeTab === 'session'}
        <SessionTab
          session={$sessionStore.session}
          hookPlane={$sessionStore.hookPlane}
          sessionId={routeSessionId ?? undefined}
          loading={$sessionStore.loading}
          error={$sessionStore.error}
          on:refreshRequested={handleSessionRefreshRequested}
          on:notify={handleSessionNotify}
        />
      {:else if activeTab === 'state'}
        <StateTab sessionId={routeSessionId ?? undefined} refreshSignal={stateRefreshSignal} />
      {:else if isConnectorTab(activeTab)}
        <ConnectorTab
          connectorName={activeTab}
          connector={activeConnector}
          pipelines={activeConnectorPipelines}
          selectedPanel={activeConnectorPanel}
          {queuedKeys}
          criticalThreshold={rollbackThreshold}
          on:selectPanel={handleConnectorPanelSelect}
          on:enqueue={(event) => enqueue(event.detail)}
        />
      {:else}
        <p>This tab is not available.</p>
        {#if shellError}
          <p class="error-text">{shellError}</p>
        {/if}
      {/if}
    </section>
    {#if showQueuePanel}
      <QueuePanel
        {queueItems}
        submitting={queueSubmitting}
        {serverPending}
        on:enqueuePrompt={enqueuePrompt}
        on:dequeue={(event) => dequeue(event.detail.id)}
        on:clear={clearQueue}
        on:submit={() => void submitQueue()}
      />
    {/if}
  </section>
  <div class="status-bar">
    <span class="status-msg">{statusMessage ?? 'Ready'}</span>
    <span class={`cc-indicator ${!daemonOnline ? 'connecting' : ccActive ? 'online' : 'idle'}`}>{ccIndicatorText}</span>
  </div>

  <SettingsModal
    open={$uiStore.activeModal === 'settings'}
    thresholds={$policyStore.thresholds}
    on:close={closeSettingsModal}
    on:saved={() => void policyStore.refresh(readRouteFromUrl().session)}
  />
</main>

<style>
  .app {
    box-sizing: border-box;
    flex: 1;
    min-height: 0;
    max-width: none;
    margin: 0;
    width: 100%;
    padding: 0 0 22px;
    display: flex;
    flex-direction: column;
    gap: 0;
    overflow: hidden;
  }

  .app-header {
    flex-shrink: 0;
    display: flex;
    align-items: center;
    gap: 12px;
    background: var(--color-surface);
    border-bottom: 1px solid var(--color-border);
    padding: 6px 16px;
  }

  .shell-tabs {
    flex-shrink: 0;
  }

  h1 {
    margin: 0;
    font-size: 14px;
    line-height: 1;
    color: var(--color-text);
  }

  .header-session {
    display: flex;
    align-items: center;
    gap: 6px;
    margin-left: auto;
    flex-shrink: 0;
    min-width: 0;
  }

  .header-session .session-label {
    font-size: 10px;
    color: var(--color-text-faint);
    text-transform: uppercase;
    letter-spacing: 0.8px;
    flex-shrink: 0;
  }

  .shared-meta {
    display: flex;
    flex-direction: column;
    gap: 0;
    flex-shrink: 0;
  }

  /* One scroll surface (legacy #main-panel). State tab: no-scroll, inner panes scroll. */
  .tab-outlet {
    flex: 1 1 auto;
    min-width: 0;
    min-height: 0;
    border: none;
    border-radius: 0;
    padding: 12px 16px 28px;
    background: var(--color-bg);
    overflow-x: hidden;
    overflow-y: auto;
  }

  .tab-outlet.no-scroll {
    overflow: hidden;
    display: flex;
    flex-direction: column;
    padding-top: 0;
  }

  .tab-outlet.no-scroll > :global(*) {
    min-height: 0;
  }

  .main-layout {
    display: flex;
    flex: 1;
    width: 100%;
    min-height: 0;
    align-items: stretch;
    overflow: hidden;
  }

  .tab-outlet.connector-mode {
    padding-top: 0;
  }

  .tab-outlet p {
    margin: 8px 0 0;
  }

  .error-text {
    color: var(--color-red);
  }

  .status-bar {
    position: fixed;
    bottom: 0;
    left: 0;
    right: 0;
    height: 22px;
    background: #161b22;
    border-top: 1px solid #21262d;
    display: flex;
    align-items: center;
    padding: 0 12px;
    font-size: 10px;
    color: #8b949e;
    gap: 16px;
    z-index: 10;
  }

  .status-msg {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .cc-indicator {
    font-size: 11px;
    margin-left: auto;
  }

  .cc-indicator.online {
    color: #3fb950;
  }

  .cc-indicator.idle {
    color: #8b949e;
  }

  .cc-indicator.connecting {
    color: #d29922;
  }

  @media (max-width: 70rem) {
    .main-layout {
      flex-direction: column;
      flex: 1;
      min-height: 0;
      overflow: hidden;
    }

    .tab-outlet {
      flex: 1;
      min-height: 0;
    }
  }
</style>
