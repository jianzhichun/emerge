<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '../../lib/api';
  import type { JsonObject } from '../../lib/types';

  interface AuditItem {
    id: string;
    ts: number;
    type: 'exec' | 'pipeline' | 'span' | 'goal' | 'tool';
    detail: string;
    outcome: string;
    severity: 'ok' | 'warn' | 'error';
  }

  export let sessionId: string | undefined;
  export let refreshSignal = 0;

  let loading = false;
  let error: string | null = null;
  let items: AuditItem[] = [];
  let observedRefreshSignal = refreshSignal;

  function toText(value: unknown): string {
    if (value === null || value === undefined) {
      return '';
    }
    if (typeof value === 'string') {
      return value;
    }
    if (typeof value === 'number' || typeof value === 'boolean') {
      return String(value);
    }
    return '';
  }

  function toTimestamp(value: unknown): number {
    const next = Number(value ?? 0);
    return Number.isFinite(next) ? next : 0;
  }

  function mapExec(event: JsonObject, index: number): AuditItem {
    const ts = toTimestamp(event.ts_ms);
    const failed = Boolean(event.is_error);
    return {
      id: `exec-${ts}-${index}`,
      ts,
      type: 'exec',
      detail: toText(event.intent_signature) || toText(event.mode) || '(exec)',
      outcome: failed ? 'error' : 'ok',
      severity: failed ? 'error' : 'ok'
    };
  }

  function mapPipeline(event: JsonObject, index: number): AuditItem {
    const ts = toTimestamp(event.ts_ms);
    const failed = Boolean(event.is_error);
    return {
      id: `pipeline-${ts}-${index}`,
      ts,
      type: 'pipeline',
      detail: toText(event.pipeline_id) || toText(event.intent_signature) || '(pipeline)',
      outcome: failed ? 'error' : 'ok',
      severity: failed ? 'error' : 'ok'
    };
  }

  function mapTool(event: JsonObject, index: number): AuditItem {
    const ts = toTimestamp(event.ts_ms);
    const toolName = toText(event.tool_name);
    const shortName = toolName.split('__').pop() || toolName || '(tool)';
    const argsSummary = toText(event.args_summary);
    const detail = argsSummary ? `${shortName} ${argsSummary}` : shortName;
    const hasSideEffects = Boolean(event.has_side_effects);
    return {
      id: `tool-${ts}-${index}`,
      ts,
      type: 'tool',
      detail,
      outcome: hasSideEffects ? 'write' : 'read',
      severity: hasSideEffects ? 'warn' : 'ok'
    };
  }

  function mapSpan(event: JsonObject, index: number): AuditItem {
    const ts = toTimestamp(event.closed_at_ms ?? event.opened_at_ms);
    const outcome = toText(event.outcome) || 'unknown';
    const severity = outcome === 'success' ? 'ok' : outcome === 'aborted' ? 'warn' : 'error';
    return {
      id: `span-${ts}-${index}`,
      ts,
      type: 'span',
      detail: toText(event.intent_signature) || toText(event.description) || '(span)',
      outcome,
      severity
    };
  }

  function mapGoal(event: JsonObject, index: number): AuditItem {
    const ts = toTimestamp(event.ts_ms);
    const eventType = toText(event.event_type) || toText(event.type) || 'event';
    const text = toText(event.text) || toText(event.goal) || '(goal)';
    return {
      id: `goal-${ts}-${index}`,
      ts,
      type: 'goal',
      detail: `${eventType}: ${text}`.slice(0, 180),
      outcome: eventType,
      severity: eventType.includes('rollback') || eventType.includes('rejected') ? 'warn' : 'ok'
    };
  }

  async function refreshAudit(): Promise<void> {
    loading = true;
    error = null;
    try {
      const [execPayload, pipelinePayload, spanPayload, goalPayload, toolPayload] = await Promise.all([
        api.getExecEvents({ limit: 60, sessionId }),
        api.getPipelineEvents({ limit: 60, sessionId }),
        api.request<{ spans?: JsonObject[] }>('/api/control-plane/spans', {
          query: { limit: 40 },
          sessionId
        }),
        api.request<{ events?: JsonObject[] }>('/api/goal-history', {
          query: { limit: 40 }
        }),
        api.getToolEvents({ limit: 120, sessionId })
      ]);

      const execItems = (execPayload.events ?? []).map((event, index) => mapExec(event as JsonObject, index));
      const pipelineItems = (pipelinePayload.events ?? []).map((event, index) => mapPipeline(event as JsonObject, index));
      const spanItems = (spanPayload.spans ?? []).map((event, index) => mapSpan(event as JsonObject, index));
      const goalItems = (goalPayload.events ?? []).map((event, index) => mapGoal(event as JsonObject, index));
      const toolItems = (toolPayload.events ?? []).map((event, index) => mapTool(event as JsonObject, index));

      items = [...execItems, ...pipelineItems, ...spanItems, ...goalItems, ...toolItems]
        .sort((a, b) => b.ts - a.ts)
        .slice(0, 100);
    } catch (loadError) {
      error = loadError instanceof Error ? loadError.message : String(loadError);
      items = [];
    } finally {
      loading = false;
    }
  }

  function formatTime(ts: number): string {
    if (!ts) {
      return '--';
    }
    return new Date(ts).toLocaleTimeString();
  }

  onMount(() => {
    void refreshAudit();
  });

  $: if (refreshSignal !== observedRefreshSignal) {
    observedRefreshSignal = refreshSignal;
    void refreshAudit();
  }

  $: timeline = items;
</script>

<section class="audit-tab">
  <div class="toolbar">
    <div class="title">Timeline</div>
    <button type="button" class="refresh-btn" on:click={() => void refreshAudit()} disabled={loading}>
      {loading ? 'Refreshing...' : 'Refresh'}
    </button>
  </div>

  {#if error}
    <p class="error-text">{error}</p>
  {/if}

  {#if !timeline.length && !loading}
    <p class="empty-text">No audit events yet.</p>
  {:else}
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Time</th>
            <th>Type</th>
            <th>Intent / Detail</th>
            <th>Outcome</th>
          </tr>
        </thead>
        <tbody>
          {#each timeline as item (item.id)}
            <tr>
              <td class="time-cell">{formatTime(item.ts)}</td>
              <td>
                <span class={`type-badge type-${item.type}`}>{item.type}</span>
              </td>
              <td class="detail-cell">{item.detail}</td>
              <td>
                <span class={`outcome outcome-${item.severity}`}>{item.outcome}</span>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</section>

<style>
  .audit-tab {
    display: flex;
    flex-direction: column;
    gap: 0.7rem;
  }

  .toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.8rem;
  }

  .title {
    font-size: 0.88rem;
    color: var(--color-text-muted);
  }

  .refresh-btn {
    border: 1px solid color-mix(in srgb, var(--color-text-muted) 35%, transparent);
    border-radius: 0.45rem;
    background: color-mix(in srgb, var(--color-bg) 82%, black);
    color: var(--color-text);
    font-size: 0.76rem;
    padding: 0.35rem 0.55rem;
    cursor: pointer;
  }

  .refresh-btn:disabled {
    opacity: 0.6;
    cursor: default;
  }

  .table-wrap {
    overflow: auto;
    border: 1px solid color-mix(in srgb, var(--color-text-muted) 26%, transparent);
    border-radius: 0.55rem;
  }

  table {
    width: 100%;
    border-collapse: collapse;
    min-width: 36rem;
  }

  th,
  td {
    text-align: left;
    border-bottom: 1px solid color-mix(in srgb, var(--color-text-muted) 18%, transparent);
    padding: 0.45rem 0.55rem;
    font-size: 0.75rem;
    vertical-align: top;
  }

  thead th {
    color: var(--color-text-muted);
    font-weight: 600;
    position: sticky;
    top: 0;
    background: color-mix(in srgb, var(--color-bg) 88%, black);
  }

  .time-cell {
    white-space: nowrap;
    color: var(--color-text-muted);
    width: 5rem;
  }

  .detail-cell {
    color: var(--color-text);
    word-break: break-word;
  }

  .type-badge {
    display: inline-block;
    border-radius: 999px;
    padding: 0.12rem 0.45rem;
    font-size: 0.68rem;
    border: 1px solid transparent;
  }

  .type-exec {
    color: #79c0ff;
    background: rgba(48, 105, 165, 0.2);
    border-color: rgba(121, 192, 255, 0.42);
  }

  .type-pipeline {
    color: #8bea9d;
    background: rgba(31, 111, 62, 0.2);
    border-color: rgba(95, 209, 128, 0.4);
  }

  .type-tool {
    color: #d2a8ff;
    background: rgba(99, 58, 135, 0.2);
    border-color: rgba(188, 140, 243, 0.4);
  }

  .type-span {
    color: #58a6ff;
    background: rgba(55, 100, 150, 0.2);
    border-color: rgba(88, 166, 255, 0.42);
  }

  .type-goal {
    color: #f2cc60;
    background: rgba(122, 99, 35, 0.2);
    border-color: rgba(242, 204, 96, 0.42);
  }

  .outcome {
    font-size: 0.72rem;
  }

  .outcome-ok {
    color: #8bea9d;
  }

  .outcome-warn {
    color: #f9d27d;
  }

  .outcome-error {
    color: #ff9e9e;
  }

  .error-text {
    margin: 0;
    color: #ff9e9e;
    font-size: 0.78rem;
  }

  .empty-text {
    margin: 0;
    color: var(--color-text-muted);
    font-size: 0.8rem;
  }
</style>
