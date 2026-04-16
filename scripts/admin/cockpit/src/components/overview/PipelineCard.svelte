<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import Badge from '../shared/Badge.svelte';
  import type { PolicyPipeline } from '../../lib/types';

  type BadgeVariant = 'neutral' | 'info' | 'success' | 'warning' | 'danger';

  interface PipelineActionEvent {
    action: string;
    key: string;
  }

  const ACTION_DEFS: Record<string, Array<{ action: string; label: string }>> = {
    explore: [
      { action: 'promote-canary', label: '-> canary' },
      { action: 'reset-failures', label: 'reset' },
      { action: 'delete', label: 'delete' }
    ],
    canary: [
      { action: 'promote-stable', label: '-> stable' },
      { action: 'demote-explore', label: '-> explore' },
      { action: 'delete', label: 'delete' }
    ],
    stable: [
      { action: 'demote-canary', label: '-> canary' },
      { action: 'delete', label: 'delete' }
    ]
  };

  export let pipeline: PolicyPipeline;
  export let queued = false;
  export let critical = false;
  export let hideConnector = false;

  const dispatch = createEventDispatcher<{ queueAction: PipelineActionEvent }>();

  function parseKey(key: string): { connector: string; mode: string; name: string } {
    const [connector = '', mode = '', ...rest] = key.split('.');
    return {
      connector,
      mode,
      name: rest.join('.') || key
    };
  }

  function sendAction(action: string): void {
    if (action === 'delete') {
      const confirmed = window.confirm(`Delete pipeline "${String(pipeline.key ?? '')}"? This removes tracking data.`);
      if (!confirmed) {
        return;
      }
    }
    if (action === 'promote-stable') {
      const confirmed = window.confirm(
        `Promote "${String(pipeline.key ?? '')}" to stable? Stable pipelines can bypass LLM inference.`
      );
      if (!confirmed) {
        return;
      }
    }
    dispatch('queueAction', {
      action,
      key: String(pipeline.key ?? '')
    });
  }

  function metricClass(rate: number | null): string {
    if (rate == null) {
      return '';
    }
    return rate >= 0.95 ? 'good' : 'warn';
  }

  $: key = String(pipeline.key ?? '');
  $: parts = parseKey(key);
  $: status = String(pipeline.status ?? 'explore');
  $: successRate = typeof pipeline.success_rate === 'number' ? pipeline.success_rate : null;
  $: verifyRate = typeof pipeline.verify_rate === 'number' ? pipeline.verify_rate : null;
  $: failures = Number(pipeline.consecutive_failures ?? 0);
  $: actions = ACTION_DEFS[status] ?? ACTION_DEFS.explore;
  $: statusVariant = (
    critical ? 'danger' : status === 'stable' ? 'success' : status === 'canary' ? 'warning' : 'neutral'
  ) as BadgeVariant;
  $: statusLabel = `${status}${pipeline.rollout_pct == null ? '' : ` · ${pipeline.rollout_pct}%`}${pipeline.synthesis_ready ? ' · synthesis' : ''}`;
</script>

<article class={`pipeline-card ${status} ${critical ? 'critical' : ''} ${queued ? 'queued' : ''}`}>
  <header>
    <div class="id-group">
      {#if !hideConnector}
        <span class="connector">{parts.connector || 'unknown'}</span>
      {/if}
      {#if parts.mode}
        <span class="mode">{parts.mode}</span>
      {/if}
      <span class="name">{parts.name}</span>
    </div>
    <Badge label={statusLabel} variant={statusVariant} />
  </header>

  {#if pipeline.description}
    <p class="desc">{pipeline.description}</p>
  {/if}

  <div class="metrics">
    <span>success <b class={metricClass(successRate)}>{successRate == null ? '?' : successRate.toFixed(2)}</b></span>
    <span>verify <b class={metricClass(verifyRate)}>{verifyRate == null ? '?' : verifyRate.toFixed(2)}</b></span>
    <span>failures <b class={failures >= 2 ? 'warn' : ''}>{failures}</b></span>
    {#if Number(pipeline.rollback_executed_count ?? 0) > 0}
      <span>rollbacks <b class="warn">{pipeline.rollback_executed_count}</b></span>
    {/if}
  </div>

  <div class="actions">
    {#each actions as actionDef}
      <button type="button" on:click={() => sendAction(actionDef.action)}>{actionDef.label}</button>
    {/each}
  </div>
</article>

<style>
  .pipeline-card {
    border: 1px solid #21262d;
    border-radius: 0.55rem;
    background: #161b22;
    padding: 0.6rem 0.7rem;
    display: flex;
    flex-direction: column;
    gap: 0.45rem;
  }

  .pipeline-card.critical {
    border-left: 3px solid #f85149;
  }

  .pipeline-card.canary {
    border-left: 3px solid #d29922;
  }

  .pipeline-card.stable {
    border-left: 3px solid #3fb950;
  }

  .pipeline-card.queued {
    opacity: 0.7;
  }

  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.45rem;
  }

  .id-group {
    min-width: 0;
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    flex-wrap: wrap;
  }

  .connector,
  .mode {
    font-size: 0.66rem;
    color: var(--color-text-muted);
    border: 1px solid color-mix(in srgb, var(--color-text-muted) 45%, transparent);
    border-radius: 999px;
    padding: 0.1rem 0.4rem;
    text-transform: lowercase;
  }

  .name {
    font-size: 0.82rem;
    color: var(--color-text);
    word-break: break-all;
  }

  .desc {
    margin: 0;
    color: #8b949e;
    font-size: 0.72rem;
  }

  .metrics {
    display: flex;
    flex-wrap: wrap;
    gap: 0.6rem;
    font-size: 0.72rem;
    color: #8b949e;
  }

  b.good {
    color: #8bea9d;
  }

  b.warn {
    color: #ff9e9e;
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem;
  }

  .actions button {
    border: 1px solid color-mix(in srgb, var(--color-text-muted) 45%, transparent);
    border-radius: 0.4rem;
    background: #0d1117;
    color: var(--color-text-muted);
    font-size: 0.72rem;
    padding: 0.2rem 0.45rem;
    cursor: pointer;
  }

  .actions button:hover {
    color: var(--color-text);
    border-color: color-mix(in srgb, var(--color-text-muted) 75%, transparent);
  }
</style>
