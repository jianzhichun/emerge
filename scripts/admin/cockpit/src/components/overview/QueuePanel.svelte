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
</script>

<aside class="queue-panel">
  <header>
    <h3>Pending Actions ({queueItems.length})</h3>
  </header>

  <div class="prompt-row">
    <textarea
      bind:value={prompt}
      rows="3"
      placeholder="Free-form instruction..."
      on:keydown={(event) => {
        if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
          event.preventDefault();
          handleAddPrompt();
        }
      }}
    ></textarea>
    <button type="button" on:click={handleAddPrompt}>Add to Queue</button>
  </div>

  <div class="items">
    {#if !queueItems.length}
      <p class="empty">Queue is empty</p>
    {:else}
      {#each queueItems as item}
        <article class={`item ${item.type === 'pipeline-delete' ? 'delete' : item.type === 'global-prompt' ? 'prompt' : ''}`}>
          <div class="label">
            <b>{item.label}</b>
            <span>{item.subLabel}</span>
            {#if item.type !== 'global-prompt'}
              <small>{item.command}</small>
            {/if}
          </div>
          <button type="button" on:click={() => remove(item.id)}>x</button>
        </article>
      {/each}
    {/if}
  </div>

  <footer>
    <button type="button" class="clear" on:click={clearQueue} disabled={!queueItems.length || submitting}>Clear</button>
    <button type="button" class="submit" on:click={submitQueue} disabled={!queueItems.length || submitting || serverPending}>
      {submitting ? 'Submitting...' : `Submit (${queueItems.length})`}
    </button>
  </footer>
</aside>

<style>
  .queue-panel {
    border: 1px solid #21262d;
    border-radius: 0.7rem;
    background: #0f141d;
    display: flex;
    flex-direction: column;
    min-height: 18rem;
  }

  header {
    padding: 0.65rem 0.75rem;
    border-bottom: 1px solid #21262d;
  }

  h3 {
    margin: 0;
    font-size: 0.84rem;
    color: #8fd4ff;
  }

  .prompt-row {
    padding: 0.65rem 0.75rem;
    border-bottom: 1px solid #21262d;
    display: flex;
    flex-direction: column;
    gap: 0.45rem;
  }

  textarea {
    width: 100%;
    resize: vertical;
    border: 1px solid #30363d;
    border-radius: 0.45rem;
    background: #0d1117;
    color: var(--color-text);
    font-size: 0.78rem;
    padding: 0.4rem 0.45rem;
  }

  .prompt-row button {
    align-self: flex-end;
  }

  .items {
    flex: 1;
    overflow: auto;
    padding: 0.55rem;
    display: flex;
    flex-direction: column;
    gap: 0.45rem;
  }

  .empty {
    margin: 0;
    color: var(--color-text-muted);
    font-size: 0.78rem;
  }

  .item {
    border: 1px solid #28412b;
    border-radius: 0.45rem;
    background: #122118;
    padding: 0.4rem 0.45rem;
    display: flex;
    justify-content: space-between;
    gap: 0.45rem;
  }

  .item.delete {
    border-color: #4a1a1a;
    background: #231316;
  }

  .item.prompt {
    border-color: #30363d;
    background: #10151f;
  }

  .label {
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
  }

  b {
    font-size: 0.75rem;
    color: #d0d7e2;
  }

  span {
    font-size: 0.72rem;
    color: #8b949e;
    white-space: pre-wrap;
    word-break: break-word;
  }

  small {
    font-size: 0.66rem;
    color: #6e7681;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .item button {
    border: none;
    background: transparent;
    color: #8b949e;
    cursor: pointer;
    font-size: 0.8rem;
  }

  footer {
    border-top: 1px solid #21262d;
    padding: 0.6rem 0.75rem;
    display: flex;
    justify-content: flex-end;
    gap: 0.45rem;
  }

  button {
    border-radius: 0.4rem;
    border: 1px solid #30363d;
    background: #111822;
    color: var(--color-text);
    padding: 0.28rem 0.55rem;
    font-size: 0.76rem;
    cursor: pointer;
  }

  .submit {
    border-color: rgba(97, 218, 124, 0.45);
    background: rgba(30, 140, 66, 0.2);
    color: #8bea9d;
  }

  button:disabled {
    opacity: 0.55;
    cursor: default;
  }
</style>
