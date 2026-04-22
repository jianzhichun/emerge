<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import JsonViewerBlock from '../shared/JsonViewerBlock.svelte';
  import { api } from '../../lib/api';
  import type { HealthDeepResponse, HookStateResponse, SessionResponse } from '../../lib/types';

  export let session: SessionResponse | null = null;
  export let hookPlane: HookStateResponse | null = null;
  /** Session scope for export/reset (matches header picker / URL). */
  export let sessionId: string | undefined = undefined;
  export let loading = false;
  export let error: string | null = null;

  const dispatch = createEventDispatcher<{
    refreshRequested: Record<string, never>;
    notify: { message: string };
  }>();

  let pendingReset = false;
  let pendingExport = false;
  let opsMetrics: Record<string, number> = {};

  function toText(value: unknown): string {
    if (value === null || value === undefined) {
      return '';
    }
    if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
      return String(value);
    }
    return '';
  }

  function formatHookCommand(raw: string): string {
    return raw.replace(/.*emerge[^\s]*/i, '…emerge').slice(0, 80);
  }

  function refreshNow(): void {
    dispatch('refreshRequested', {});
    void loadOpsMetrics();
  }

  async function loadOpsMetrics(): Promise<void> {
    try {
      const payload: HealthDeepResponse = await api.getHealthDeep();
      opsMetrics = payload.metrics ?? {};
    } catch {
      opsMetrics = {};
    }
  }

  async function exportSession(): Promise<void> {
    if (pendingExport) {
      return;
    }
    pendingExport = true;
    try {
      const payload = await api.exportSession(sessionId);
      const snapshot = payload.snapshot ?? {};
      const blob = new Blob([JSON.stringify(snapshot, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = 'emerge-session-export.json';
      link.click();
      URL.revokeObjectURL(url);
      dispatch('notify', { message: 'Session export downloaded.' });
    } catch (saveError) {
      const message = saveError instanceof Error ? saveError.message : String(saveError);
      dispatch('notify', { message: `Session export failed: ${message}` });
    } finally {
      pendingExport = false;
    }
  }

  async function resetSession(): Promise<void> {
    if (pendingReset) {
      return;
    }
    const answer = window.prompt('Type RESET to confirm full session reset', '');
    if (answer !== 'RESET') {
      return;
    }
    pendingReset = true;
    try {
      const payload = await api.resetSession({ confirm: 'RESET', full: true }, sessionId);
      if (payload.ok === false) {
        dispatch('notify', { message: `Session reset failed: ${payload.error ?? 'unknown error'}` });
      } else {
        dispatch('notify', { message: 'Session reset completed.' });
        dispatch('refreshRequested', {});
      }
    } catch (resetError) {
      const message = resetError instanceof Error ? resetError.message : String(resetError);
      dispatch('notify', { message: `Session reset failed: ${message}` });
    } finally {
      pendingReset = false;
    }
  }

  $: hf = hookPlane?.hook_fields;
  $: turnCount = Number(hf?.turn_count ?? 0);
  $: activeSpanId = toText(hf?.active_span_id);
  $: spanNudgeSent = Boolean(hf?.span_nudge_sent);
  $: activeSpanIntent = toText(hf?.active_span_intent);
  $: registeredHooks = Array.isArray(hookPlane?.registered_hooks) ? hookPlane!.registered_hooks! : [];
  $: contextPreview = toText(hookPlane?.context_preview);
  $: hookOk = Boolean(hookPlane?.hook_fields);
  $: requestsTotal = Number(opsMetrics.requests_total ?? 0);
  $: requestErrors = Number(opsMetrics.request_errors ?? 0);
  $: runnerConnected = Number(opsMetrics.runner_connected ?? 0);
  $: eventQueueDepth = Number(opsMetrics.event_appender_queue_depth ?? 0);

  $: if (session || hookPlane) {
    void loadOpsMetrics();
  }
</script>

<section class="session-tab">
  <div class="session-stack">
    <div class="session-title-row">
      <h2 class="audit-trail-title">Session</h2>
      <button type="button" class="cp-btn-sm" on:click={refreshNow} disabled={loading}>
        {loading ? 'Loading…' : 'Refresh'}
      </button>
    </div>

    {#if error}
      <p class="error-text">{error}</p>
    {/if}

    {#if session}
      <div class="cp-stat-strip" style="margin-bottom: 16px">
        <div class="cp-stat-card">
          <div class="cp-stat-num">{toText(session.session_id) || '—'}</div>
          <div class="cp-stat-label">Session ID</div>
        </div>
        <div class="cp-stat-card">
          <div class="cp-stat-num">{session.wal_entries ?? 0}</div>
          <div class="cp-stat-label">WAL Entries</div>
        </div>
      </div>
    {:else if !loading}
      <p class="muted">No session payload.</p>
    {/if}

    {#if hookOk}
      <div
        style="margin-bottom: 16px; border: 1px solid #21262d; border-radius: 6px; padding: 12px"
      >
        <div style="font-size: 12px; color: #e6edf3; margin-bottom: 10px; font-weight: 600">Hook State</div>
        <div style="display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin-bottom: 10px">
          <div class="cp-stat-card" style="padding: 8px">
            <div class="cp-stat-num" style="font-size: 18px">{turnCount}</div>
            <div class="cp-stat-label">Turn</div>
          </div>
          <div class="cp-stat-card" style="padding: 8px">
            <div
              class="cp-stat-num"
              style="font-size: 12px; color: {activeSpanId ? '#3fb950' : '#6e7681'}"
            >
              {activeSpanId ? `● ${activeSpanId.slice(-8)}` : '○ none'}
            </div>
            <div class="cp-stat-label">Active Span</div>
          </div>
          <div class="cp-stat-card" style="padding: 8px">
            <div class="cp-stat-num" style="font-size: 12px; color: {spanNudgeSent ? '#d29922' : '#6e7681'}">
              {spanNudgeSent ? '✓ sent' : '✗ not sent'}
            </div>
            <div class="cp-stat-label">Span Nudge</div>
          </div>
        </div>
        {#if activeSpanIntent}
          <div style="font-size: 11px; color: #8b949e; margin-bottom: 6px">
            Intent: <span style="color: #58a6ff">{activeSpanIntent}</span>
          </div>
        {/if}
        {#if registeredHooks.length > 0}
          <div style="margin-top: 10px; border-top: 1px solid #21262d; padding-top: 8px">
            <div style="font-size: 11px; color: #6e7681; margin-bottom: 4px">Registered Hooks</div>
            {#each registeredHooks as h}
              <div
                class="hook-line-mono"
                title={toText(h.command)}
              >
                <span style="color: #58a6ff; margin-right: 6px">{toText(h.event)}</span>{formatHookCommand(
                  toText(h.command)
                )}
              </div>
            {/each}
          </div>
        {/if}
      </div>
    {/if}

    <div style="margin-bottom: 16px; border: 1px solid #21262d; border-radius: 6px; padding: 12px">
      <div style="font-size: 12px; color: #e6edf3; margin-bottom: 10px; font-weight: 600">Ops Metrics</div>
      <div style="display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px">
        <div class="cp-stat-card" style="padding: 8px">
          <div class="cp-stat-num" style="font-size: 16px">{requestsTotal}</div>
          <div class="cp-stat-label">Requests</div>
        </div>
        <div class="cp-stat-card" style="padding: 8px">
          <div class="cp-stat-num" style="font-size: 16px; color: {requestErrors ? '#f85149' : '#3fb950'}">
            {requestErrors}
          </div>
          <div class="cp-stat-label">Errors</div>
        </div>
        <div class="cp-stat-card" style="padding: 8px">
          <div class="cp-stat-num" style="font-size: 16px">{runnerConnected}</div>
          <div class="cp-stat-label">Runner</div>
        </div>
        <div class="cp-stat-card" style="padding: 8px">
          <div class="cp-stat-num" style="font-size: 16px">{eventQueueDepth}</div>
          <div class="cp-stat-label">Event Queue</div>
        </div>
      </div>
    </div>

    {#if contextPreview}
      <div style="margin-bottom: 16px; border: 1px solid #21262d; border-radius: 6px; padding: 12px">
        <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px">
          <div style="font-size: 12px; color: #e6edf3; font-weight: 600">Context Injection Preview</div>
          <span style="font-size: 10px; color: #6e7681">UserPromptSubmit turn {turnCount} + 1</span>
        </div>
        <pre
          style="font-size: 10px; color: #8b949e; background: #0d1117; border: 1px solid #21262d; border-radius: 4px; padding: 10px; white-space: pre-wrap; word-break: break-word; max-height: 300px; overflow-y: auto; margin: 0"
          >{contextPreview}</pre
        >
      </div>
    {/if}

    {#if session?.checkpoint}
      <div style="margin-bottom: 12px">
        <div style="font-size: 12px; color: #e6edf3; margin-bottom: 6px; font-weight: 600">Checkpoint</div>
        <JsonViewerBlock viewerId="session-checkpoint" data={session.checkpoint} />
      </div>
    {/if}

    {#if session?.recovery}
      <div style="margin-bottom: 12px">
        <div style="font-size: 12px; color: #d29922; margin-bottom: 6px; font-weight: 600">Recovery</div>
        <JsonViewerBlock viewerId="session-recovery" data={session.recovery} />
      </div>
    {/if}

    <div style="display: flex; gap: 8px; margin-top: 16px">
      <button
        type="button"
        class="cp-btn-sm"
        disabled={pendingExport || loading}
        on:click={() => void exportSession()}
      >
        {pendingExport ? 'Exporting…' : 'Export Snapshot'}
      </button>
      <button
        type="button"
        class="cp-btn-sm danger"
        disabled={pendingReset || loading}
        on:click={() => void resetSession()}
      >
        {pendingReset ? 'Resetting…' : 'Reset Session'}
      </button>
    </div>
  </div>
</section>

<style>
  .session-tab {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-height: 0;
  }
  .session-title-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    margin-bottom: 4px;
  }
  .audit-trail-title {
    margin: 0;
  }
  .error-text {
    margin: 0;
    color: #f85149;
    font-size: 12px;
  }
  .muted {
    color: #8b949e;
    font-size: 11px;
  }
  .hook-line-mono {
    font-size: 10px;
    color: #8b949e;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    margin-bottom: 4px;
  }
</style>
