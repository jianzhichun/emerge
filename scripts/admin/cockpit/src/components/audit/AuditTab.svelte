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
    toolShort?: string;
    toolArgs?: string;
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
    const hasSideEffects = Boolean(event.has_side_effects);
    return {
      id: `tool-${ts}-${index}`,
      ts,
      type: 'tool',
      detail: argsSummary ? `${shortName} ${argsSummary}` : shortName,
      toolShort: shortName,
      toolArgs: argsSummary || undefined,
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
    const detailText = toText(event.text) || toText(event.goal) || '(goal)';
    const detail = detailText.slice(0, 60);
    const outcome =
      toText(event.type) || toText(event.event_type) || toText(event.source) || '';
    return {
      id: `goal-${ts}-${index}`,
      ts,
      type: 'goal',
      detail,
      outcome,
      severity: outcome.toLowerCase().includes('rollback') || outcome.toLowerCase().includes('rejected') ? 'warn' : 'ok'
    };
  }

  async function refreshAudit(): Promise<void> {
    loading = true;
    error = null;
    try {
      const [execPayload, pipelinePayload, spanPayload, goalPayload, toolPayload] = await Promise.all([
        api.getExecEvents({ limit: 50, sessionId }),
        api.getPipelineEvents({ limit: 50, sessionId }),
        api.request<{ spans?: JsonObject[] }>('/api/control-plane/spans', {
          query: { limit: 30 },
          sessionId
        }),
        api.request<{ events?: JsonObject[] }>('/api/goal-history', {
          query: { limit: 30 }
        }),
        api.getToolEvents({ limit: 200, sessionId })
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
  <div class="audit-head">
    <h2 class="audit-trail-title">Audit Trail</h2>
    <button type="button" class="cp-btn-sm" on:click={() => void refreshAudit()} disabled={loading}>
      {loading ? 'Loading…' : 'Refresh'}
    </button>
  </div>

  {#if error}
    <p class="error-text">{error}</p>
  {/if}

  {#if loading && !timeline.length}
    <p class="loading-text">Loading audit data…</p>
  {:else if !timeline.length}
    <p class="empty-text">No audit events yet.</p>
  {:else}
    <div class="cp-intent-table-wrap">
      <table class="cp-intent-table">
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
              <td>{formatTime(item.ts)}</td>
              <td>
                {#if item.type === 'exec'}
                  <span class="source-badge exec">exec</span>
                {:else if item.type === 'pipeline'}
                  <span class="source-badge span">pipeline</span>
                {:else if item.type === 'span'}
                  <span class="source-badge both">span</span>
                {:else if item.type === 'goal'}
                  <span class="goal-pill">goal</span>
                {:else if item.type === 'tool'}
                  <span class="tool-pill">tool</span>
                {/if}
              </td>
              <td class="detail-td">
                {#if item.type === 'tool' && item.toolShort}
                  <span class="tool-name">{item.toolShort}</span>
                  {#if item.toolArgs}
                    <span class="tool-args">{item.toolArgs}</span>
                  {/if}
                {:else}
                  {item.detail}
                {/if}
              </td>
              <td class="outcome-td">
                {#if item.type === 'exec'}
                  {#if item.outcome === 'error'}
                    <span class="critical">error</span>
                  {:else}
                    <span class="stable">ok</span>
                  {/if}
                {:else if item.type === 'pipeline'}
                  {#if item.outcome === 'error'}
                    <span class="critical">error</span>
                  {:else}
                    <span class="stable">ok</span>
                  {/if}
                {:else if item.type === 'span'}
                  {#if item.outcome === 'success'}
                    <span class="stable">success</span>
                  {:else}
                    <span class="critical">{item.outcome}</span>
                  {/if}
                {:else if item.type === 'goal'}
                  {item.outcome}
                {:else if item.type === 'tool'}
                  {#if item.outcome === 'write'}
                    <span class="tool-write">write</span>
                  {:else}
                    <span class="tool-read">read</span>
                  {/if}
                {/if}
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
    gap: 10px;
    padding: 16px;
    min-height: 0;
  }
  .audit-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }
  .audit-trail-title {
    margin: 0;
    font-size: 14px;
    color: #e6edf3;
    font-weight: 600;
  }
  .goal-pill {
    font-size: 10px;
    color: #79c0ff;
  }
  .tool-pill {
    font-size: 10px;
    color: #d2a8ff;
  }
  .detail-td {
    color: #cdd9e5;
    word-break: break-word;
  }
  .tool-name {
    color: #cdd9e5;
  }
  .tool-args {
    color: #8b949e;
    font-size: 10px;
    margin-left: 4px;
  }
  .outcome-td {
    font-size: 11px;
  }
  .tool-write {
    color: #ffa657;
    font-size: 10px;
  }
  .tool-read {
    color: #8b949e;
    font-size: 10px;
  }
  .error-text {
    margin: 0;
    color: #f85149;
    font-size: 12px;
  }
  .empty-text,
  .loading-text {
    margin: 0;
    color: #8b949e;
    font-size: 11px;
  }
</style>
