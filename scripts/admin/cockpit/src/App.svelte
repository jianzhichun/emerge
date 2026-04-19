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
  import type { ActionTypeEntry, AssetConnector } from './lib/types';
  import { monitorsStore } from './stores/monitors';
  import { policyStore } from './stores/policy';
  import { sessionStore } from './stores/session';
  import { stateStore } from './stores/state';
  import { uiStore } from './stores/ui';
  import { queueStore, type QueueDraft } from './stores/queue';

  interface TabBarItem {
    id: string;
    label: string;
    warn?: boolean;
    subtle?: boolean;
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
  let serverPending = false;
  let ccActive = false;
  let hasCockpitSubmission = false;
  let cockpitAckPending = false;
  let cockpitAckLagMs: number | null = null;
  let queueSubmitting = false;
  let statusMessage: string | null = null;
  let sseStatus = 'idle';
  let actionTypesById: Record<string, ActionTypeEntry> = {};

  function isGlobalTab(tab: string): tab is GlobalTabId {
    return tab === 'overview' || controlTabs.some((item) => item.id === tab);
  }

  function connectorNamesFromData(
    assets: Record<string, AssetConnector>,
    intents: Array<{ key?: string }>
  ): string[] {
    const assetNames = Object.keys(assets);
    const policyNames = intents.map((intent) => String(intent.key ?? '').split('.')[0] || '').filter((name) => name.length > 0);
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
      hasCockpitSubmission = Boolean(response.last_cockpit_event_id);
      cockpitAckPending = Boolean(response.cockpit_ack_pending);
      cockpitAckLagMs =
        typeof response.cockpit_ack_lag_ms === 'number' ? response.cockpit_ack_lag_ms : null;
    } catch {
      serverPending = false;
      ccActive = false;
      hasCockpitSubmission = false;
      cockpitAckPending = false;
      cockpitAckLagMs = null;
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

  async function refreshActionTypes(): Promise<void> {
    try {
      const payload = await api.getActionTypes();
      const entries = Array.isArray(payload.types) ? payload.types : [];
      actionTypesById = Object.fromEntries(
        entries
          .filter((row): row is ActionTypeEntry => Boolean(row && typeof row.type === 'string' && row.type.trim()))
          .map((row) => [row.type, row])
      );
    } catch {
      actionTypesById = {};
    }
  }

  function buildQueueDraftFromAction(action: Record<string, unknown>): QueueDraft {
    const actionType = String(action.type ?? '').trim();
    const typeMeta = actionTypesById[actionType];
    const label = actionType || 'custom.action';
    const rawPreview = JSON.stringify(action) ?? actionType;
    const subLabel =
      rawPreview.length > 80
        ? `${rawPreview.slice(0, 80)}...`
        : rawPreview;
    return {
      type: label,
      label,
      subLabel,
      command: label,
      data: action
    };
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
      refreshActionTypes(),
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

  function enqueuePrompt(event: CustomEvent<{ prompt: string }>): void {
    const prompt = event.detail.prompt;
    queueStore.enqueue({
      type: 'core.prompt',
      label: 'Instruction',
      subLabel: prompt.length > 60 ? `${prompt.slice(0, 60)}...` : prompt,
      command: 'core.prompt',
      data: { type: 'core.prompt', prompt },
    });
  }

  async function submitQueue(): Promise<void> {
    const items = $queueStore.items;
    if (!items.length || queueSubmitting || serverPending) return;
    queueSubmitting = true;
    statusMessage = 'Submitting queue...';
    try {
      const result = await api.request<{ ok?: boolean; action_count?: number; error?: string }>(
        '/api/submit',
        { method: 'POST', body: { actions: items.map((item) => item.data) } }
      );
      if (result.ok === false) {
        statusMessage = `Submit failed: ${result.error ?? 'unknown error'}`;
      } else {
        statusMessage = `Submitted ${result.action_count ?? items.length} action(s)`;
        queueStore.clear();
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
    const onControlMessage = (event: MessageEvent): void => {
      if (event.origin !== window.location.origin) {
        return;
      }
      const payload = event.data;
      if (!payload || typeof payload !== 'object') {
        return;
      }
      const messageType = String((payload as { type?: unknown }).type ?? '');
      if (messageType === 'emerge:enqueue') {
        const action = (payload as { action?: unknown }).action;
        if (!action || typeof action !== 'object' || Array.isArray(action)) {
          return;
        }
        queueStore.enqueue(buildQueueDraftFromAction(action as Record<string, unknown>));
        return;
      }
      if (messageType === 'emerge:dequeue') {
        const id = Number((payload as { id?: unknown }).id);
        if (Number.isFinite(id) && id > 0) {
          queueStore.dequeue(id);
        }
        return;
      }
      if (messageType === 'emerge:clear') {
        queueStore.clear();
      }
    };
    window.addEventListener('popstate', onPopState);
    window.addEventListener('message', onControlMessage);
    return () => {
      clearInterval(fallbackInterval);
      window.removeEventListener('popstate', onPopState);
      window.removeEventListener('message', onControlMessage);
      sse.stop();
    };
  });

  $: connectorNames = connectorNamesFromData(connectorAssets, $policyStore.intents);
  $: rollbackThreshold = Number($policyStore.thresholds?.rollback_consecutive_failures ?? 2);
  $: leftTabs = [
    primaryTab,
    ...connectorNames.map((name) => {
      const hasCritical = $policyStore.intents.some(
        (intent) => String(intent.key ?? '').startsWith(`${name}.`) && Number(intent.consecutive_failures ?? 0) >= rollbackThreshold
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
  $: activeConnectorIntents = isGlobalTab(activeTab)
    ? []
    : $policyStore.intents.filter((intent) => String(intent.key ?? '').startsWith(`${activeTab}.`));
  $: queuedKeys = new Set(
    $queueStore.items.map((item) => String((item.data && item.data.key) ?? '')).filter((key) => key.length > 0)
  );
  $: daemonOnline = sseStatus === 'connected';
  $: ccIndicatorText = !daemonOnline
    ? '◐ Daemon offline'
    : ccActive
      ? '● CC connected'
      : '○ CC idle';
  $: cockpitDeliveryText = !daemonOnline
    ? '· dispatch unknown'
    : !hasCockpitSubmission
      ? '· no cockpit actions'
      : cockpitAckPending
        ? '⏳ cockpit pending consumption'
        : cockpitAckLagMs == null
          ? '✓ cockpit consumed'
          : `✓ cockpit consumed (${cockpitAckLagMs}ms)`;

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
    <div class="header-logo">
      <svg viewBox="0 0 64 64" width="28" height="28" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
        <rect width="64" height="64" rx="14" fill="#0f172a"/>
        <circle cx="32" cy="32" r="8" fill="#6366f1" opacity="0.12"/>
        <path d="M32 32 C32 32 26 21 20 21 C13 21 13 43 20 43 C26 43 32 32 32 32 Z" stroke="#60a5fa" stroke-width="4.5" fill="none" stroke-linejoin="round"/>
        <path d="M32 32 C32 32 38 21 44 21 C51 21 51 43 44 43 C38 43 32 32 32 32 Z" stroke="#a78bfa" stroke-width="4.5" fill="none" stroke-linejoin="round"/>
        <polygon points="44,21 48,26 40,24" fill="#c4b5fd" opacity="0.9"/>
        <circle cx="32" cy="32" r="3.5" fill="white" opacity="0.95"/>
      </svg>
      <span class="header-wordmark">emerge</span>
    </div>
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
          intents={$policyStore.intents}
          thresholds={$policyStore.thresholds}
          connectorNames={connectorNames}
          queueSize={$queueStore.items.length}
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
          intents={activeConnectorIntents}
          selectedPanel={activeConnectorPanel}
          {queuedKeys}
          criticalThreshold={rollbackThreshold}
          on:selectPanel={handleConnectorPanelSelect}
          on:enqueue={(event) => queueStore.enqueue(event.detail)}
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
        queueItems={$queueStore.items}
        submitting={queueSubmitting}
        {serverPending}
        actionHazards={Object.fromEntries(
          Object.entries(actionTypesById).map(([type, row]) => [type, String(row.hazard ?? 'write')])
        )}
        on:enqueuePrompt={enqueuePrompt}
        on:dequeue={(event) => queueStore.dequeue(event.detail.id)}
        on:clear={() => queueStore.clear()}
        on:submit={() => void submitQueue()}
      />
    {/if}
  </section>
  <div class="status-bar">
    <span class="status-msg">{statusMessage ?? 'Ready'}</span>
    <span class={`delivery-indicator ${cockpitAckPending ? 'pending' : 'ok'}`}>{cockpitDeliveryText}</span>
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

  .header-logo {
    display: flex;
    align-items: center;
    gap: 10px;
  }

  .header-wordmark {
    font-size: 14px;
    font-weight: 600;
    line-height: 1;
    color: #e2e8f0;
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

  .delivery-indicator {
    font-size: 11px;
    color: #8b949e;
  }

  .delivery-indicator.ok {
    color: #3fb950;
  }

  .delivery-indicator.pending {
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
