<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import type { PolicyPipeline } from '../../lib/types';

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

  function actionClass(action: string): string {
    if (action.startsWith('promote')) {
      return 'promote';
    }
    if (action.startsWith('demote')) {
      return 'demote';
    }
    if (action === 'delete') {
      return 'danger';
    }
    return '';
  }

  $: key = String(pipeline.key ?? '');
  $: parts = parseKey(key);
  $: status = String(pipeline.status ?? 'explore');
  $: successRate = typeof pipeline.success_rate === 'number' ? pipeline.success_rate : null;
  $: verifyRate = typeof pipeline.verify_rate === 'number' ? pipeline.verify_rate : null;
  $: failures = Number(pipeline.consecutive_failures ?? 0);
  $: actions = ACTION_DEFS[status] ?? ACTION_DEFS.explore;
  $: statusLabel = `${status}${pipeline.rollout_pct == null ? '' : ` · ${pipeline.rollout_pct}%`}${pipeline.synthesis_ready ? ' · synthesis' : ''}`;
</script>

<article class={`pipeline-card ${status} ${critical ? 'critical' : ''} ${queued ? 'queued' : ''}`}>
  <header class="pipeline-card-row">
    <div class="key-parts">
      {#if !hideConnector}
        <span class="key-connector" style={connectorStyle(parts.connector || 'unknown')}>{parts.connector || 'unknown'}</span>
      {/if}
      {#if parts.mode}
        <span class={`key-mode ${parts.mode}`}>{parts.mode}</span>
      {/if}
      <span class="key-name">{parts.name}</span>
    </div>
    <span class={`pipeline-badge badge-${status}`}>{statusLabel}</span>
    <div class="pipeline-actions">
      {#each actions as actionDef}
        <button type="button" class={`act-btn ${actionClass(actionDef.action)}`} on:click={() => sendAction(actionDef.action)}>
          {actionDef.label}
        </button>
      {/each}
    </div>
  </header>

  {#if pipeline.description}
    <p class="pipeline-desc">{pipeline.description}</p>
  {/if}

  <div class="pipeline-metrics">
    <span>success <b class={metricClass(successRate)}>{successRate == null ? '?' : successRate.toFixed(2)}</b></span>
    <span>verify <b class={metricClass(verifyRate)}>{verifyRate == null ? '?' : verifyRate.toFixed(2)}</b></span>
    <span>failures <b class={failures >= 2 ? 'warn' : ''}>{failures}</b></span>
    {#if Number(pipeline.rollback_executed_count ?? 0) > 0}
      <span>rollbacks <b class="warn">{pipeline.rollback_executed_count}</b></span>
    {/if}
  </div>
</article>

<style>
  .pipeline-card {
    background: #161b22;
    border: 1px solid #21262d;
    border-radius: 6px;
    padding: 8px 12px;
    margin-bottom: 4px;
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
    opacity: 0.6;
  }

  .pipeline-card-row {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .key-parts {
    display: flex;
    align-items: center;
    gap: 5px;
    flex: 1;
    min-width: 0;
    overflow: hidden;
  }

  .key-connector {
    padding: 1px 7px;
    border-radius: 3px;
    font-size: 10px;
    font-weight: 700;
    flex-shrink: 0;
    letter-spacing: 0.3px;
  }

  .key-mode {
    padding: 1px 5px;
    border-radius: 3px;
    font-size: 10px;
    flex-shrink: 0;
  }

  .key-mode.read {
    background: #0d1f2d;
    color: #58a6ff;
    border: 1px solid #1a3a5a;
  }

  .key-mode.write {
    background: #1f1200;
    color: #d29922;
    border: 1px solid #4a3a10;
  }

  .key-name {
    font-size: 12px;
    color: #e6edf3;
    font-weight: 600;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .pipeline-badge {
    padding: 1px 7px;
    border-radius: 10px;
    font-size: 10px;
    flex-shrink: 0;
  }

  .badge-stable {
    background: #1b2d1b;
    color: #3fb950;
    border: 1px solid #2a4a2a;
  }

  .badge-canary {
    background: #2d2208;
    color: #d29922;
    border: 1px solid #4a3a10;
  }

  .badge-explore {
    background: #1c2128;
    color: #8b949e;
    border: 1px solid #30363d;
  }

  .pipeline-desc {
    font-size: 10px;
    color: #6e7681;
    margin: 2px 0 0;
    font-style: italic;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .pipeline-metrics {
    font-size: 10px;
    color: #8b949e;
    margin-top: 3px;
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
  }

  .pipeline-metrics .warn {
    color: #f85149;
  }

  .pipeline-metrics .good {
    color: #3fb950;
  }

  .pipeline-actions {
    display: flex;
    gap: 4px;
    flex-shrink: 0;
    flex-wrap: wrap;
    justify-content: flex-end;
  }

  .act-btn {
    padding: 2px 8px;
    font-size: 10px;
    border-radius: 3px;
    cursor: pointer;
    border: 1px solid #30363d;
    background: #1c2128;
    color: #8b949e;
    white-space: nowrap;
  }

  .act-btn:hover {
    border-color: #58a6ff;
    color: #58a6ff;
  }

  .act-btn.promote {
    border-color: #2a4a2a;
    color: #3fb950;
  }

  .act-btn.promote:hover {
    background: #1b2d1b;
  }

  .act-btn.demote {
    border-color: #4a3a10;
    color: #d29922;
  }

  .act-btn.demote:hover {
    background: #2d2208;
  }

  .act-btn.danger {
    border-color: #4a1a1a;
    color: #f85149;
  }

  .act-btn.danger:hover {
    background: #1a0d0d;
  }

  .badge-unknown {
    background: #1c2128;
    color: #8b949e;
    border: 1px solid #30363d;
  }
</style>
