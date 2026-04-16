<script lang="ts">
  import { onDestroy } from 'svelte';
  import { createEventDispatcher } from 'svelte';
  import { api } from '../../lib/api';
  import type { PolicyThresholds } from '../../lib/types';

  interface SaveSettingsResponse {
    ok?: boolean;
    error?: string;
  }

  const THRESHOLD_FIELDS = [
    'promote_min_attempts',
    'promote_min_success_rate',
    'promote_min_verify_rate',
    'promote_max_human_fix_rate',
    'stable_min_attempts',
    'stable_min_success_rate',
    'stable_min_verify_rate',
    'rollback_consecutive_failures'
  ] as const;

  type ThresholdField = (typeof THRESHOLD_FIELDS)[number];

  const INTEGER_FIELDS = new Set<ThresholdField>([
    'promote_min_attempts',
    'stable_min_attempts',
    'rollback_consecutive_failures'
  ]);

  export let open = false;
  export let thresholds: PolicyThresholds = {};

  const dispatch = createEventDispatcher<{ close: undefined; saved: undefined }>();

  let previousOpen = false;
  let formValues: Record<ThresholdField, string> = {
    promote_min_attempts: '',
    promote_min_success_rate: '',
    promote_min_verify_rate: '',
    promote_max_human_fix_rate: '',
    stable_min_attempts: '',
    stable_min_success_rate: '',
    stable_min_verify_rate: '',
    rollback_consecutive_failures: ''
  };
  let saving = false;
  let message: string | null = null;
  let error = false;
  let closeTimer: ReturnType<typeof setTimeout> | null = null;

  function syncFromThresholds(): void {
    for (const key of THRESHOLD_FIELDS) {
      const value = thresholds[key];
      formValues = {
        ...formValues,
        [key]: value == null ? '' : String(value)
      };
    }
  }

  function clearCloseTimer(): void {
    if (closeTimer) {
      clearTimeout(closeTimer);
      closeTimer = null;
    }
  }

  function closeModal(): void {
    clearCloseTimer();
    dispatch('close');
  }

  async function save(): Promise<void> {
    const policy: Record<string, number> = {};
    for (const key of THRESHOLD_FIELDS) {
      const raw = formValues[key].trim();
      if (!raw) {
        continue;
      }
      const parsed = INTEGER_FIELDS.has(key) ? parseInt(raw, 10) : parseFloat(raw);
      if (!Number.isFinite(parsed)) {
        message = `Invalid number for ${key}`;
        error = true;
        return;
      }
      policy[key] = parsed;
    }

    saving = true;
    message = 'Saving...';
    error = false;

    try {
      const response = await api.request<SaveSettingsResponse>('/api/settings', {
        method: 'POST',
        body: { policy }
      });
      if (response.ok === false) {
        message = response.error ?? 'Failed to save settings';
        error = true;
        return;
      }
      message = 'Saved';
      error = false;
      dispatch('saved');
      clearCloseTimer();
      closeTimer = setTimeout(() => {
        closeTimer = null;
        dispatch('close');
      }, 500);
    } catch (saveError) {
      message = saveError instanceof Error ? saveError.message : String(saveError);
      error = true;
    } finally {
      saving = false;
    }
  }

  $: if (open && !previousOpen) {
    clearCloseTimer();
    syncFromThresholds();
    message = null;
    error = false;
  }
  $: if (!open && previousOpen) {
    clearCloseTimer();
  }
  $: previousOpen = open;

  onDestroy(() => {
    clearCloseTimer();
  });
</script>

{#if open}
  <div class="modal-overlay" role="presentation" on:click={(event) => event.target === event.currentTarget && closeModal()}>
    <div class="modal" role="dialog" aria-modal="true" aria-label="Edit thresholds">
      <header>
        <h3>Threshold Settings</h3>
      </header>

      <div class="form-grid">
        {#each THRESHOLD_FIELDS as key}
          <label for={`cfg-${key}`}>{key}</label>
          <input id={`cfg-${key}`} type="text" bind:value={formValues[key]} />
        {/each}
      </div>

      <footer>
        {#if message}
          <span class:err={error}>{message}</span>
        {/if}
        <div class="actions">
          <button type="button" on:click={closeModal} disabled={saving}>Cancel</button>
          <button type="button" on:click={() => void save()} disabled={saving}>{saving ? 'Saving...' : 'Save'}</button>
        </div>
      </footer>
    </div>
  </div>
{/if}

<style>
  .modal-overlay {
    position: fixed;
    inset: 0;
    z-index: 40;
    display: grid;
    place-items: center;
    background: rgba(0, 0, 0, 0.56);
    padding: 1rem;
  }

  .modal {
    width: min(40rem, 100%);
    border: 1px solid color-mix(in srgb, var(--color-text-muted) 40%, transparent);
    border-radius: 0.8rem;
    background: #161b22;
    padding: 0.9rem;
    display: flex;
    flex-direction: column;
    gap: 0.8rem;
  }

  h3 {
    margin: 0;
    font-size: 1rem;
  }

  .form-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 0.5rem 0.7rem;
  }

  label {
    color: var(--color-text-muted);
    font-size: 0.76rem;
    align-self: center;
  }

  input {
    border: 1px solid color-mix(in srgb, var(--color-text-muted) 45%, transparent);
    border-radius: 0.4rem;
    background: #0d1117;
    color: var(--color-text);
    font-size: 0.82rem;
    padding: 0.3rem 0.45rem;
  }

  footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.7rem;
  }

  .actions {
    display: inline-flex;
    gap: 0.45rem;
  }

  button {
    border-radius: 0.45rem;
    padding: 0.3rem 0.6rem;
    font-size: 0.8rem;
    border: 1px solid color-mix(in srgb, var(--color-text-muted) 50%, transparent);
    background: #1a212c;
    color: var(--color-text);
    cursor: pointer;
  }

  .actions button:last-child {
    border-color: rgba(97, 218, 124, 0.45);
    background: rgba(30, 140, 66, 0.2);
    color: #8bea9d;
  }

  span {
    color: #8bea9d;
    font-size: 0.75rem;
  }

  span.err {
    color: #ff9e9e;
  }
</style>
