<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import type { AssetConnector, PolicyPipeline } from '../../lib/types';

  type ConnectorPanelId = 'pipelines' | 'notes' | 'controls';

  interface PanelDef {
    id: ConnectorPanelId;
    label: string;
    visible: boolean;
    count: number;
  }

  export let connectorName = '';
  export let connector: AssetConnector | null = null;
  export let pipelines: PolicyPipeline[] = [];
  export let selectedPanel: ConnectorPanelId = 'pipelines';

  const dispatch = createEventDispatcher<{
    selectPanel: { panel: ConnectorPanelId };
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
    dispatch('selectPanel', { panel });
  }

  function statusBadge(status: string): string {
    const normalized = status.toLowerCase();
    if (normalized === 'stable') {
      return 'success';
    }
    if (normalized === 'canary') {
      return 'warning';
    }
    return 'neutral';
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
  $: activePanel = panelFromConnector(panelDefs, toPanel(selectedPanel));
</script>

<section class="connector-tab">
  {#if !connector}
    <p class="empty-text">Connector "{connectorName}" not found.</p>
  {:else}
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
            <article class="pipeline-card">
              <div class="pipeline-head">
                <div class="pipeline-key">{toText(pipeline.key) || '(no key)'}</div>
                <span class={`pill ${statusBadge(toText(pipeline.status) || 'explore')}`}>{toText(pipeline.status) || 'explore'}</span>
              </div>
              <div class="pipeline-meta">
                success: {pipeline.success_rate ?? '?'} · verify: {pipeline.verify_rate ?? '?'} · failures: {pipeline.consecutive_failures ?? 0}
              </div>
            </article>
          {/each}
        {/if}
      </div>
    {:else if activePanel === 'notes'}
      <div class="notes-block">
        {#if connector.notes}
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
  {/if}
</section>

<style>
  .connector-tab {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }

  .panel-tabs {
    display: flex;
    flex-wrap: wrap;
    gap: 0.45rem;
  }

  .panel-tab {
    border: 1px solid color-mix(in srgb, var(--color-text-muted) 35%, transparent);
    border-radius: 0.5rem;
    background: color-mix(in srgb, var(--color-bg) 86%, black);
    color: var(--color-text-muted);
    font-size: 0.76rem;
    padding: 0.33rem 0.55rem;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
  }

  .panel-tab.active {
    color: var(--color-text);
    border-color: color-mix(in srgb, var(--color-text-muted) 50%, transparent);
    background: color-mix(in srgb, var(--color-text-muted) 16%, transparent);
  }

  .count {
    border-radius: 999px;
    padding: 0.05rem 0.38rem;
    background: rgba(99, 110, 123, 0.25);
    font-size: 0.66rem;
  }

  .list {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .pipeline-card {
    border: 1px solid color-mix(in srgb, var(--color-text-muted) 24%, transparent);
    border-radius: 0.55rem;
    padding: 0.55rem;
    background: color-mix(in srgb, var(--color-bg) 90%, black);
  }

  .pipeline-head {
    display: flex;
    align-items: center;
    gap: 0.4rem;
  }

  .pipeline-key {
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', monospace;
    color: var(--color-text);
    font-size: 0.74rem;
    word-break: break-word;
  }

  .pipeline-meta {
    margin-top: 0.3rem;
    color: var(--color-text-muted);
    font-size: 0.68rem;
  }

  .pill {
    margin-left: auto;
    border: 1px solid transparent;
    border-radius: 999px;
    padding: 0.1rem 0.4rem;
    font-size: 0.66rem;
    text-transform: lowercase;
  }

  .pill.neutral {
    color: var(--color-text);
    background: color-mix(in srgb, var(--color-text-muted) 14%, transparent);
    border-color: color-mix(in srgb, var(--color-text-muted) 40%, transparent);
  }

  .pill.warning {
    color: #f9d27d;
    background: rgba(163, 121, 24, 0.2);
    border-color: rgba(242, 190, 90, 0.45);
  }

  .pill.success {
    color: #8bea9d;
    background: rgba(30, 140, 66, 0.2);
    border-color: rgba(104, 216, 137, 0.45);
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
