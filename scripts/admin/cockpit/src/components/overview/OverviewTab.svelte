<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import PipelineGroup from './PipelineGroup.svelte';
  import QueuePanel from './QueuePanel.svelte';
  import type { PolicyPipeline, PolicyThresholds } from '../../lib/types';

  interface PipelineActionEvent {
    action: string;
    key: string;
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

  export let pipelines: PolicyPipeline[] = [];
  export let thresholds: PolicyThresholds = {};
  export let queueItems: QueueItem[] = [];
  export let queueSubmitting = false;
  export let serverPending = false;

  const dispatch = createEventDispatcher<{
    enqueue: QueueDraft;
    dequeue: { id: number };
    clearQueue: undefined;
    submitQueue: undefined;
  }>();

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

  function enqueuePrompt(event: CustomEvent<{ prompt: string }>): void {
    const prompt = event.detail.prompt;
    dispatch('enqueue', {
      type: 'global-prompt',
      label: 'Instruction',
      subLabel: prompt.length > 60 ? `${prompt.slice(0, 60)}...` : prompt,
      command: 'global-prompt',
      data: { type: 'global-prompt', prompt }
    });
  }

  function dequeue(event: CustomEvent<{ id: number }>): void {
    dispatch('dequeue', event.detail);
  }

  function clearQueue(): void {
    dispatch('clearQueue');
  }

  function submitQueue(): void {
    dispatch('submitQueue');
  }

  $: rollbackThreshold = Number(thresholds.rollback_consecutive_failures ?? 2);
  $: critical = pipelines.filter((pipeline) => Number(pipeline.consecutive_failures ?? 0) >= rollbackThreshold);
  $: canary = pipelines.filter(
    (pipeline) => Number(pipeline.consecutive_failures ?? 0) < rollbackThreshold && String(pipeline.status ?? 'explore') === 'canary'
  );
  $: stable = pipelines.filter(
    (pipeline) => Number(pipeline.consecutive_failures ?? 0) < rollbackThreshold && String(pipeline.status ?? 'explore') === 'stable'
  );
  $: explore = pipelines.filter((pipeline) => {
    if (Number(pipeline.consecutive_failures ?? 0) >= rollbackThreshold) {
      return false;
    }
    const status = String(pipeline.status ?? 'explore');
    return status !== 'stable' && status !== 'canary';
  });
  $: queuedKeys = new Set(
    queueItems.map((item) => String((item.data && item.data.key) ?? '')).filter((key) => key.length > 0)
  );
</script>

<section class="overview-layout">
  <div class="groups">
    <section>
      <h3>Critical ({critical.length})</h3>
      <PipelineGroup pipelines={critical} {queuedKeys} criticalThreshold={rollbackThreshold} on:queueAction={enqueueFromCard} />
    </section>

    <section>
      <h3>Canary ({canary.length})</h3>
      <PipelineGroup pipelines={canary} {queuedKeys} criticalThreshold={rollbackThreshold} on:queueAction={enqueueFromCard} />
    </section>

    <section>
      <h3>Stable ({stable.length})</h3>
      <PipelineGroup pipelines={stable} {queuedKeys} criticalThreshold={rollbackThreshold} on:queueAction={enqueueFromCard} />
    </section>

    <section>
      <h3>Explore ({explore.length})</h3>
      <PipelineGroup pipelines={explore} {queuedKeys} criticalThreshold={rollbackThreshold} on:queueAction={enqueueFromCard} />
    </section>
  </div>

  <QueuePanel
    {queueItems}
    submitting={queueSubmitting}
    {serverPending}
    on:enqueuePrompt={enqueuePrompt}
    on:dequeue={dequeue}
    on:clear={clearQueue}
    on:submit={submitQueue}
  />
</section>

<style>
  .overview-layout {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 19rem;
    gap: 0.85rem;
    align-items: start;
  }

  .groups {
    display: flex;
    flex-direction: column;
    gap: 0.8rem;
  }

  section {
    border: 1px solid color-mix(in srgb, var(--color-text-muted) 35%, transparent);
    border-radius: 0.7rem;
    background: color-mix(in srgb, var(--color-bg) 90%, black);
    padding: 0.65rem;
  }

  h3 {
    margin: 0 0 0.55rem;
    font-size: 0.85rem;
    color: #d0d7e2;
  }

  @media (max-width: 70rem) {
    .overview-layout {
      grid-template-columns: 1fr;
    }
  }
</style>
