<script lang="ts">
  import type { DeltaItem, RiskItem } from '../../lib/types';

  const MAX_ROWS = 200;

  export let deltas: DeltaItem[] = [];
  export let risks: RiskItem[] = [];
  export let verificationState: string | null = null;
  export let activeSpanId: string | null = null;
  export let activeSpanIntent: string | null = null;
  export let loading = false;
  export let error: string | null = null;

  function toText(value: unknown): string {
    if (value === null || value === undefined) {
      return '';
    }
    if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
      return String(value);
    }
    return '';
  }

  function statusClass(status: string): string {
    const normalized = status.toLowerCase();
    if (normalized === 'error' || normalized === 'failure' || normalized === 'retract') {
      return 'danger';
    }
    if (normalized === 'handled' || normalized === 'ok' || normalized === 'success') {
      return 'success';
    }
    if (normalized === 'snoozed' || normalized === 'provisional') {
      return 'warning';
    }
    return 'neutral';
  }

  function levelClass(level: string): string {
    const normalized = level.toLowerCase();
    if (normalized === 'error' || normalized === 'critical') {
      return 'danger';
    }
    if (normalized === 'warning' || normalized === 'warn') {
      return 'warning';
    }
    return 'neutral';
  }

  function formatTime(tsMs: unknown): string {
    const ts = Number(tsMs ?? 0);
    if (!Number.isFinite(ts) || ts <= 0) {
      return '--';
    }
    return new Date(ts).toLocaleTimeString();
  }

  $: deltaRows = deltas
    .slice()
    .sort((a, b) => Number(b.ts_ms ?? 0) - Number(a.ts_ms ?? 0))
    .slice(0, MAX_ROWS);
  $: riskRows = risks
    .slice()
    .sort((a, b) => Number(b.created_at_ms ?? 0) - Number(a.created_at_ms ?? 0))
    .slice(0, MAX_ROWS);
</script>

<section class="state-tab">
  <div class="meta-strip">
    <div class="meta-item">
      <span class="meta-label">Verification</span>
      <span class={`pill ${statusClass(toText(verificationState) || 'neutral')}`}>{toText(verificationState) || 'unknown'}</span>
    </div>
    <div class="meta-item">
      <span class="meta-label">Active span</span>
      <span class="mono">{activeSpanId ? activeSpanId.slice(-12) : 'none'}</span>
    </div>
    {#if activeSpanIntent}
      <div class="meta-item">
        <span class="meta-label">Intent</span>
        <span class="mono">{activeSpanIntent}</span>
      </div>
    {/if}
  </div>

  {#if error}
    <p class="error-text">{error}</p>
  {/if}

  <div class="lists-grid">
    <section class="list-panel">
      <header>
        <h3>Deltas</h3>
        <span class="count">showing {deltaRows.length}/{deltas.length}</span>
      </header>
      {#if !deltaRows.length && !loading}
        <p class="empty-text">No deltas recorded.</p>
      {:else}
        <div class="rows">
          {#each deltaRows as row}
            <article class="row">
              <div class="row-top">
                <span class={`pill ${levelClass(toText(row.level) || 'neutral')}`}>{toText(row.level) || 'info'}</span>
                <span class="time">{formatTime(row.ts_ms)}</span>
              </div>
              <p class="message">{toText(row.message) || '(no message)'}</p>
              <p class="subtle">{toText(row.intent_signature) || '(no intent)'}</p>
            </article>
          {/each}
        </div>
      {/if}
    </section>

    <section class="list-panel">
      <header>
        <h3>Risks</h3>
        <span class="count">showing {riskRows.length}/{risks.length}</span>
      </header>
      {#if !riskRows.length && !loading}
        <p class="empty-text">No risks recorded.</p>
      {:else}
        <div class="rows">
          {#each riskRows as row}
            <article class="row">
              <div class="row-top">
                <span class={`pill ${statusClass(toText(row.status) || 'open')}`}>{toText(row.status) || 'open'}</span>
                <span class="time">{formatTime(row.created_at_ms)}</span>
              </div>
              <p class="message">{toText(row.text) || '(no risk text)'}</p>
              <p class="subtle">{toText(row.intent_signature) || '(no intent)'}</p>
            </article>
          {/each}
        </div>
      {/if}
    </section>
  </div>
</section>

<style>
  .state-tab {
    display: flex;
    flex-direction: column;
    gap: 0.8rem;
  }

  .meta-strip {
    display: flex;
    flex-wrap: wrap;
    gap: 0.6rem;
  }

  .meta-item {
    border: 1px solid color-mix(in srgb, var(--color-text-muted) 24%, transparent);
    border-radius: 0.5rem;
    padding: 0.4rem 0.55rem;
    background: color-mix(in srgb, var(--color-bg) 90%, black);
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    font-size: 0.72rem;
  }

  .meta-label {
    color: var(--color-text-muted);
  }

  .mono {
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', monospace;
    color: var(--color-text);
  }

  .lists-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 0.8rem;
  }

  .list-panel {
    border: 1px solid color-mix(in srgb, var(--color-text-muted) 22%, transparent);
    border-radius: 0.6rem;
    padding: 0.6rem;
    background: color-mix(in srgb, var(--color-bg) 92%, black);
    min-height: 14rem;
  }

  header {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 0.6rem;
    margin-bottom: 0.5rem;
  }

  h3 {
    margin: 0;
    font-size: 0.84rem;
  }

  .count {
    color: var(--color-text-muted);
    font-size: 0.68rem;
  }

  .rows {
    max-height: 22rem;
    overflow: auto;
    display: flex;
    flex-direction: column;
    gap: 0.45rem;
    padding-right: 0.1rem;
  }

  .row {
    border: 1px solid color-mix(in srgb, var(--color-text-muted) 18%, transparent);
    border-left-width: 3px;
    border-radius: 0.45rem;
    padding: 0.45rem 0.5rem;
    background: color-mix(in srgb, var(--color-bg) 90%, black);
  }

  .row-top {
    display: flex;
    align-items: center;
    gap: 0.4rem;
  }

  .time {
    margin-left: auto;
    color: var(--color-text-muted);
    font-size: 0.66rem;
  }

  .pill {
    display: inline-flex;
    align-items: center;
    border: 1px solid transparent;
    border-radius: 999px;
    font-size: 0.66rem;
    padding: 0.08rem 0.42rem;
    text-transform: lowercase;
  }

  .pill.neutral {
    color: var(--color-text);
    background: color-mix(in srgb, var(--color-text-muted) 14%, transparent);
    border-color: color-mix(in srgb, var(--color-text-muted) 45%, transparent);
  }

  .pill.success {
    color: #8bea9d;
    background: rgba(30, 140, 66, 0.2);
    border-color: rgba(104, 216, 137, 0.45);
  }

  .pill.warning {
    color: #f9d27d;
    background: rgba(163, 121, 24, 0.2);
    border-color: rgba(242, 190, 90, 0.45);
  }

  .pill.danger {
    color: #ff9e9e;
    background: rgba(150, 45, 45, 0.2);
    border-color: rgba(255, 138, 138, 0.45);
  }

  .message {
    margin: 0.32rem 0 0;
    font-size: 0.74rem;
    color: var(--color-text);
    word-break: break-word;
  }

  .subtle {
    margin: 0.22rem 0 0;
    font-size: 0.66rem;
    color: var(--color-text-muted);
    word-break: break-word;
  }

  .empty-text {
    margin: 0.5rem 0 0;
    color: var(--color-text-muted);
    font-size: 0.76rem;
  }

  .error-text {
    margin: 0;
    color: #ff9e9e;
    font-size: 0.78rem;
  }

  @media (max-width: 70rem) {
    .lists-grid {
      grid-template-columns: 1fr;
    }
  }
</style>
