<script lang="ts">
  import { createEventDispatcher } from 'svelte';

  export let open = false;
  export let title = 'Confirm';
  export let message = '';
  export let confirmLabel = 'Confirm';
  export let cancelLabel = 'Cancel';

  const dispatch = createEventDispatcher<{ confirm: void; cancel: void }>();

  function handleConfirm(): void {
    dispatch('confirm');
  }

  function handleCancel(): void {
    dispatch('cancel');
  }
</script>

{#if open}
  <div class="modal-backdrop" role="dialog" aria-modal="true" aria-label={title}>
    <div class="modal-card">
      <h3>{title}</h3>
      <p>{message}</p>
      <div class="actions">
        <button type="button" class="cp-btn-sm" on:click={handleCancel}>{cancelLabel}</button>
        <button type="button" class="cp-btn-sm primary" on:click={handleConfirm}>{confirmLabel}</button>
      </div>
    </div>
  </div>
{/if}

<style>
  .modal-backdrop {
    position: fixed;
    inset: 0;
    background: rgb(0 0 0 / 50%);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 50;
  }
  .modal-card {
    width: min(420px, 90vw);
    background: var(--color-surface);
    border: 1px solid var(--color-border);
    border-radius: 8px;
    padding: 14px;
    color: var(--color-text);
  }
  h3 {
    margin: 0 0 8px;
    font-size: 14px;
  }
  p {
    margin: 0;
    color: var(--color-text-muted);
    font-size: 12px;
    line-height: 1.5;
  }
  .actions {
    margin-top: 12px;
    display: flex;
    justify-content: flex-end;
    gap: 8px;
  }
</style>
