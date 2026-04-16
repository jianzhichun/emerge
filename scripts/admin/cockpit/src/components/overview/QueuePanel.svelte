<script lang="ts">
  import { createEventDispatcher } from 'svelte';

  interface QueueItem {
    id: number;
    type: string;
    label: string;
    subLabel: string;
    command: string;
    data: Record<string, unknown>;
  }

  export let queueItems: QueueItem[] = [];
  export let submitting = false;
  export let serverPending = false;
  export let actionHazards: Record<string, string> = {};

  const dispatch = createEventDispatcher<{
    enqueuePrompt: { prompt: string };
    dequeue: { id: number };
    clear: undefined;
    submit: undefined;
  }>();

  let prompt = '';

  function handleAddPrompt(): void {
    const trimmed = prompt.trim();
    if (!trimmed) {
      return;
    }
    dispatch('enqueuePrompt', { prompt: trimmed });
    prompt = '';
  }

  function remove(id: number): void {
    dispatch('dequeue', { id });
  }

  function submitQueue(): void {
    dispatch('submit');
  }

  function clearQueue(): void {
    dispatch('clear');
  }

  function hazardFor(type: string): string {
    return actionHazards[type] ?? 'write';
  }
</script>

<aside class="queue-panel">
  <header class="queue-header">Pending Actions ({queueItems.length})</header>

  <div class="prompt-input-area">
    <textarea
      bind:value={prompt}
      class="prompt-textarea"
      rows="3"
      placeholder="Free-form instruction..."
      on:keydown={(event) => {
        if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
          event.preventDefault();
          handleAddPrompt();
        }
      }}
    ></textarea>
    <button type="button" class="prompt-add-btn" on:click={handleAddPrompt}>+ Add to Queue</button>
  </div>

  <div class="queue-items">
    {#if !queueItems.length}
      <p class="empty">Queue is empty</p>
    {:else}
      {#each queueItems as item}
        <article class={`queue-item hazard-${hazardFor(item.type)}`}>
          <div class="action-label">
            <b class:del={hazardFor(item.type) === 'danger'} class:promote={item.label.toLowerCase().includes('promote')} class:prompt-lbl={item.type === 'core.prompt'}>
              {item.label}
            </b>
            <span>{item.subLabel}</span>
            {#if item.type !== 'core.prompt'}
              <small>{item.command}</small>
            {/if}
          </div>
          <button type="button" class="remove-btn" on:click={() => remove(item.id)}>x</button>
        </article>
      {/each}
    {/if}
  </div>

  <footer class="queue-footer">
    <button type="button" class="submit-btn" on:click={submitQueue} disabled={!queueItems.length || submitting || serverPending}>
      {submitting ? 'Submitting...' : `Submit (${queueItems.length})`}
    </button>
    <button type="button" class="clear-btn" on:click={clearQueue} disabled={!queueItems.length || submitting}>Clear Queue</button>
  </footer>
</aside>

<style>
  /* Legacy: same row as main-panel — fill content row height; list flexes between header and footer. */
  .queue-panel {
    box-sizing: border-box;
    width: 300px;
    flex: 0 0 300px;
    align-self: stretch;
    min-height: 0;
    border-left: 1px solid #21262d;
    background: #0d1117;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  .queue-header {
    padding: 10px 14px;
    font-size: 11px;
    color: #58a6ff;
    text-transform: uppercase;
    letter-spacing: 1px;
    border-bottom: 1px solid #21262d;
    flex-shrink: 0;
  }

  .prompt-input-area {
    padding: 8px 10px;
    border-bottom: 1px solid #21262d;
    flex-shrink: 0;
    display: flex;
    flex-direction: column;
    gap: 5px;
  }

  .prompt-textarea {
    width: 100%;
    resize: vertical;
    border: 1px solid #30363d;
    border-radius: 4px;
    background: #0d1117;
    color: #e6edf3;
    font-size: 11px;
    padding: 4px 8px;
  }

  .prompt-add-btn {
    align-self: flex-end;
    background: #162032;
    border: 1px solid #1f6feb;
    border-radius: 4px;
    color: #58a6ff;
    font-size: 11px;
    padding: 4px 10px;
    cursor: pointer;
  }

  .prompt-add-btn:hover {
    background: #1f3a5a;
  }

  .queue-items {
    flex: 1 1 auto;
    min-height: 0;
    overflow-y: auto;
    overflow-x: hidden;
    padding: 8px;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .empty {
    margin: 0;
    color: #6e7681;
    font-size: 11px;
  }

  .queue-item {
    border: 1px solid #28412b;
    border-radius: 4px;
    background: #122118;
    padding: 6px 8px;
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 6px;
  }

  .queue-item.hazard-danger {
    border-color: #4a1a1a;
    background: #231316;
  }

  .queue-item.hazard-safe {
    background: #111722;
    border-color: #2b3646;
  }

  .queue-item.hazard-write {
    background: #122118;
    border-color: #28412b;
  }

  .action-label {
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .action-label b {
    font-size: 11px;
    color: #58a6ff;
    display: block;
  }

  .action-label b.del {
    color: #f85149;
  }

  .action-label b.promote {
    color: #3fb950;
  }

  .action-label b.prompt-lbl {
    color: #d2a8ff;
  }

  .action-label span {
    font-size: 10px;
    color: #8b949e;
    white-space: pre-wrap;
    word-break: break-word;
  }

  .action-label small {
    font-size: 9px;
    color: #6e7681;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .remove-btn {
    background: transparent;
    border: none;
    color: #484f58;
    cursor: pointer;
    font-size: 14px;
    line-height: 1;
    padding: 0;
  }

  .remove-btn:hover {
    color: #f85149;
  }

  .queue-footer {
    border-top: 1px solid #21262d;
    padding: 10px 12px;
    flex-shrink: 0;
  }

  .submit-btn {
    width: 100%;
    padding: 8px;
    background: #1a3a2a;
    border: 1px solid #3fb950;
    border-radius: 5px;
    color: #3fb950;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
  }

  .submit-btn:hover {
    background: #2a4a3a;
  }

  .submit-btn:disabled {
    opacity: 0.4;
    cursor: default;
  }

  .clear-btn {
    width: 100%;
    padding: 4px;
    background: none;
    border: none;
    color: #484f58;
    font-size: 11px;
    cursor: pointer;
    margin-top: 4px;
  }

  .clear-btn:disabled {
    opacity: 0.5;
    cursor: default;
  }

  @media (max-width: 70rem) {
    .queue-panel {
      width: 100%;
      flex: 0 1 auto;
      height: auto;
      max-height: min(50vh, 460px);
      align-self: auto;
      border-left: none;
      border-top: 1px solid #21262d;
    }
  }
</style>
