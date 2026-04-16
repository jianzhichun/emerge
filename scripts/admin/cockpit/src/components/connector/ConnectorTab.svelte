<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import PipelineCard from '../overview/PipelineCard.svelte';
  import type { AssetConnector, PolicyPipeline } from '../../lib/types';

  type ConnectorPanelId = 'pipelines' | 'notes' | 'controls';

  interface PanelDef {
    id: ConnectorPanelId;
    label: string;
    visible: boolean;
    count: number;
  }

  interface PipelineActionEvent {
    action: string;
    key: string;
  }

  interface QueueDraft {
    type: string;
    label: string;
    subLabel: string;
    command: string;
    data: Record<string, unknown>;
  }

  export let connectorName = '';
  export let connector: AssetConnector | null = null;
  export let pipelines: PolicyPipeline[] = [];
  export let selectedPanel: ConnectorPanelId = 'pipelines';
  export let queuedKeys: Set<string> = new Set<string>();
  export let criticalThreshold = 2;

  const dispatch = createEventDispatcher<{
    selectPanel: { panel: ConnectorPanelId };
    enqueue: QueueDraft;
  }>();

  function toText(value: unknown): string {
    if (value === null || value === undefined) {
      return '';
    }
    if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
      return String(value);
    }
    return '';
  }

  function toPanel(value: string | undefined): ConnectorPanelId {
    if (value === 'notes' || value === 'controls') {
      return value;
    }
    return 'pipelines';
  }

  function panelFromConnector(panelDefs: PanelDef[], candidate: ConnectorPanelId): ConnectorPanelId {
    if (panelDefs.some((panel) => panel.visible && panel.id === candidate)) {
      return candidate;
    }
    const firstVisible = panelDefs.find((panel) => panel.visible);
    return firstVisible?.id ?? 'pipelines';
  }

  function setPanel(panel: ConnectorPanelId): void {
    localPanel = panel;
    dispatch('selectPanel', { panel });
  }

  function makeQueueItemFromAction(action: PipelineActionEvent): QueueDraft | null {
    const map: Record<string, { label: string; type: string; fields?: Record<string, unknown> }> = {
      'promote-canary': { label: 'Promote -> canary', type: 'pipeline-set', fields: { status: 'canary', rollout_pct: 20 } },
      'promote-stable': { label: 'Promote -> stable', type: 'pipeline-set', fields: { status: 'stable', rollout_pct: 100 } },
      'demote-explore': { label: 'Demote -> explore', type: 'pipeline-set', fields: { status: 'explore', rollout_pct: 0 } },
      'demote-canary': { label: 'Demote -> canary', type: 'pipeline-set', fields: { status: 'canary', rollout_pct: 20 } },
      'reset-failures': { label: 'Reset failures', type: 'pipeline-set', fields: { consecutive_failures: 0 } },
      delete: { label: 'Delete pipeline', type: 'pipeline-delete' }
    };
    const def = map[action.action];
    if (!def) {
      return null;
    }
    if (def.type === 'pipeline-delete') {
      return {
        type: def.type,
        label: def.label,
        subLabel: action.key,
        command: `pipeline-delete ${action.key}`,
        data: { type: 'pipeline-delete', key: action.key }
      };
    }
    return {
      type: def.type,
      label: def.label,
      subLabel: action.key,
      command: `pipeline-set ${action.key} ${JSON.stringify(def.fields ?? {})}`,
      data: {
        type: 'pipeline-set',
        key: action.key,
        fields: def.fields ?? {}
      }
    };
  }

  function enqueueFromCard(event: CustomEvent<PipelineActionEvent>): void {
    const next = makeQueueItemFromAction(event.detail);
    if (!next) {
      return;
    }
    dispatch('enqueue', next);
  }

  function controlSrc(filename: string): string {
    return `/api/components/${encodeURIComponent(connectorName)}/${encodeURIComponent(filename)}`;
  }

  $: components = connector?.components ?? [];
  $: panelDefs = [
    { id: 'pipelines', label: 'Pipelines', visible: true, count: pipelines.length },
    { id: 'notes', label: 'Notes', visible: true, count: connector?.notes ? 1 : 0 },
    { id: 'controls', label: 'Controls', visible: components.length > 0, count: components.length }
  ] as PanelDef[];
  let localPanel: ConnectorPanelId = 'pipelines';
  let lastConnectorName = '';
  $: if (connectorName !== lastConnectorName) {
    lastConnectorName = connectorName;
    localPanel = toPanel(selectedPanel);
  }
  $: if (!panelDefs.some((panel) => panel.visible && panel.id === localPanel)) {
    localPanel = panelFromConnector(panelDefs, localPanel);
  }
  $: activePanel = panelFromConnector(panelDefs, localPanel);
</script>

<section class="connector-tab">
  <div class="panel-tabs" role="tablist" aria-label="Connector sections">
    {#each panelDefs as panel}
      {#if panel.visible}
        <button
          type="button"
          class={`panel-tab ${activePanel === panel.id ? 'active' : ''}`}
          role="tab"
          aria-selected={activePanel === panel.id ? 'true' : 'false'}
          on:click={() => setPanel(panel.id)}
        >
          {panel.label}
          <span class="count">{panel.count}</span>
        </button>
      {/if}
    {/each}
  </div>

  {#if activePanel === 'pipelines'}
    <div class="list">
      {#if pipelines.length === 0}
        <p class="empty-text">No pipelines for this connector.</p>
      {:else}
        {#each pipelines as pipeline}
          <PipelineCard
            {pipeline}
            hideConnector={true}
            queued={queuedKeys.has(toText(pipeline.key))}
            critical={Number(pipeline.consecutive_failures ?? 0) >= criticalThreshold}
            on:queueAction={enqueueFromCard}
          />
        {/each}
      {/if}
    </div>
  {:else if activePanel === 'notes'}
    <div class="notes-block">
      {#if connector?.notes}
        <pre>{connector.notes}</pre>
      {:else}
        <p class="empty-text">No NOTES.md yet for this connector.</p>
      {/if}
    </div>
  {:else if activePanel === 'controls'}
    <div class="controls-list">
      {#if components.length === 0}
        <p class="empty-text">No injected controls yet.</p>
      {:else}
        {#each components as component}
          <article class="control-slot">
            <div class="control-label">
              <strong>{toText(component.filename) || '(component)'}</strong>
              <span>{toText(component.context)}</span>
            </div>
            {#if component.filename}
              <iframe src={controlSrc(component.filename)} title={component.filename}></iframe>
            {/if}
          </article>
        {/each}
      {/if}
    </div>
  {/if}
</section>

<style>
  .connector-tab {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .panel-tabs {
    display: flex;
    border-bottom: 1px solid #30363d;
    background: #161b22;
    overflow-x: auto;
    margin: 0 -16px 0;
    padding: 0 6px;
    position: sticky;
    top: 0;
    z-index: 2;
  }

  .panel-tabs::-webkit-scrollbar {
    height: 3px;
  }

  .panel-tabs::-webkit-scrollbar-thumb {
    background: #30363d;
  }

  .panel-tab {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 8px 16px;
    border: none;
    border-bottom: 2px solid transparent;
    background: transparent;
    color: #8b949e;
    font-size: 12px;
    cursor: pointer;
    white-space: nowrap;
    flex-shrink: 0;
    font-family: inherit;
  }

  .panel-tab:hover {
    color: #e6edf3;
  }

  .panel-tab.active {
    border-bottom-color: #58a6ff;
    color: #e6edf3;
  }

  .count {
    min-width: 14px;
    padding: 0 4px;
    border-radius: 999px;
    background: #21262d;
    color: #8b949e;
    font-size: 10px;
    line-height: 1.3;
    text-align: center;
  }

  .panel-tab.active .count {
    color: #79c0ff;
    background: #1c2a3a;
  }

  .list {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .notes-block pre {
    margin: 0;
    max-height: 22rem;
    overflow: auto;
    border: 1px solid color-mix(in srgb, var(--color-text-muted) 20%, transparent);
    border-radius: 0.5rem;
    padding: 0.65rem;
    background: rgba(10, 15, 23, 0.55);
    color: #bcc8d6;
    font-size: 0.74rem;
    white-space: pre-wrap;
    word-break: break-word;
  }

  .controls-list {
    display: flex;
    flex-direction: column;
    gap: 0.65rem;
  }

  .control-slot {
    border: 1px solid color-mix(in srgb, var(--color-text-muted) 24%, transparent);
    border-radius: 0.55rem;
    padding: 0.55rem;
    background: color-mix(in srgb, var(--color-bg) 90%, black);
  }

  .control-label {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    margin-bottom: 0.5rem;
  }

  .control-label strong {
    color: var(--color-text);
    font-size: 0.78rem;
  }

  .control-label span {
    color: var(--color-text-muted);
    font-size: 0.68rem;
  }

  iframe {
    width: 100%;
    border: 1px solid color-mix(in srgb, var(--color-text-muted) 24%, transparent);
    border-radius: 0.45rem;
    min-height: 14rem;
    background: #fff;
  }

  .empty-text {
    margin: 0;
    color: var(--color-text-muted);
    font-size: 0.78rem;
  }
</style>
