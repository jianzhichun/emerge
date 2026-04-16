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
    color: #484f58;
    font-size: 11px;
    padding: 6px 0;
  }

  .connector-groups {
    display: flex;
    flex-direction: column;
    gap: 0;
  }

  .connector-group {
    margin-top: 10px;
  }

  .connector-group:first-child {
    margin-top: 0;
  }

  header {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 4px;
    padding: 2px 0;
  }

  .name {
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.5px;
    border-radius: 3px;
    padding: 1px 7px;
  }

  .count {
    color: #484f58;
    font-size: 10px;
  }

  .cards {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
</style>
