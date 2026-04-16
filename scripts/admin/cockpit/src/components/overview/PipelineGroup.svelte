<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import PipelineCard from './PipelineCard.svelte';
  import type { PolicyPipeline } from '../../lib/types';

  interface PipelineActionEvent {
    action: string;
    key: string;
  }

  export let pipelines: PolicyPipeline[] = [];
  export let queuedKeys: Set<string> = new Set<string>();
  export let criticalThreshold = 2;

  const dispatch = createEventDispatcher<{ queueAction: PipelineActionEvent }>();

  function connectorHue(name: string): number {
    let hash = 0;
    for (let i = 0; i < name.length; i += 1) {
      hash = (hash * 31 + name.charCodeAt(i)) & 0xffff;
    }
    return 60 + (hash % 240);
  }

  function connectorStyle(name: string): string {
    const hue = connectorHue(name);
    return `background:hsl(${hue},30%,12%);color:hsl(${hue},70%,65%);border:1px solid hsl(${hue},30%,22%)`;
  }

  function connectorFromKey(key: string): string {
    const [connector] = key.split('.');
    return connector || 'unknown';
  }

  function groupByConnector(list: PolicyPipeline[]): Array<{ connector: string; pipelines: PolicyPipeline[] }> {
    const grouped = new Map<string, PolicyPipeline[]>();
    for (const pipeline of list) {
      const connector = connectorFromKey(String(pipeline.key ?? ''));
      const existing = grouped.get(connector);
      if (existing) {
        existing.push(pipeline);
      } else {
        grouped.set(connector, [pipeline]);
      }
    }
    return Array.from(grouped.entries()).map(([connector, connectorPipelines]) => ({
      connector,
      pipelines: connectorPipelines
    }));
  }

  function forwardAction(event: CustomEvent<PipelineActionEvent>): void {
    dispatch('queueAction', event.detail);
  }

  $: groupedPipelines = groupByConnector(pipelines);
</script>

{#if !groupedPipelines.length}
  <p class="empty">No pipelines in this group.</p>
{:else}
  <div class="connector-groups">
    {#each groupedPipelines as group}
      <section class="connector-group">
        <header>
          <span class="name" style={connectorStyle(group.connector)}>{group.connector}</span>
          <span class="count">{group.pipelines.length} pipeline{group.pipelines.length === 1 ? '' : 's'}</span>
        </header>

        <div class="cards">
          {#each group.pipelines as pipeline}
            <PipelineCard
              {pipeline}
              hideConnector={true}
              queued={queuedKeys.has(String(pipeline.key ?? ''))}
              critical={Number(pipeline.consecutive_failures ?? 0) >= criticalThreshold}
              on:queueAction={forwardAction}
            />
          {/each}
        </div>
      </section>
    {/each}
  </div>
{/if}

<style>
  .empty {
    margin: 0;
    color: var(--color-text-muted);
    font-size: 0.8rem;
  }

  .connector-groups {
    display: flex;
    flex-direction: column;
    gap: 0.7rem;
  }

  .connector-group {
    border: 1px solid color-mix(in srgb, var(--color-text-muted) 40%, transparent);
    border-radius: 0.6rem;
    background: color-mix(in srgb, var(--color-bg) 88%, black);
    padding: 0.6rem;
  }

  header {
    display: flex;
    align-items: center;
    gap: 0.45rem;
    margin-bottom: 0.55rem;
  }

  .name {
    font-size: 0.68rem;
    font-weight: 700;
    letter-spacing: 0.05em;
    border-radius: 0.25rem;
    padding: 0.1rem 0.4rem;
    text-transform: lowercase;
  }

  .count {
    color: var(--color-text-muted);
    font-size: 0.72rem;
  }

  .cards {
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
  }
</style>
