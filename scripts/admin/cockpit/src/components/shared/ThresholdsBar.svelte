<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import type { PolicyThresholds } from '../../lib/types';

  export let thresholds: PolicyThresholds = {};

  const dispatch = createEventDispatcher<{ edit: undefined }>();

  function emitEdit(): void {
    dispatch('edit');
  }
</script>

<section class="thresholds-bar">
  <button type="button" on:click={emitEdit}>Thresholds</button>
  <span>promote success >= <b>{thresholds.promote_min_success_rate ?? 'n/a'}</b></span>
  <span>verify >= <b>{thresholds.promote_min_verify_rate ?? 'n/a'}</b></span>
  <span>attempts >= <b>{thresholds.promote_min_attempts ?? 'n/a'}</b></span>
  <span>rollback failures = <b>{thresholds.rollback_consecutive_failures ?? 'n/a'}</b></span>
  <span>stable attempts >= <b>{thresholds.stable_min_attempts ?? 'n/a'}</b></span>
</section>

<style>
  .thresholds-bar {
    display: flex;
    align-items: center;
    gap: 0.8rem;
    flex-wrap: wrap;
    border: 1px solid color-mix(in srgb, var(--color-text-muted) 40%, transparent);
    border-radius: 0.7rem;
    background: color-mix(in srgb, var(--color-bg) 88%, black);
    padding: 0.5rem 0.7rem;
    font-size: 0.75rem;
    color: var(--color-text-muted);
  }

  button {
    border: 1px solid rgba(111, 167, 255, 0.45);
    border-radius: 0.45rem;
    background: rgba(50, 117, 179, 0.2);
    color: #8fd4ff;
    padding: 0.25rem 0.55rem;
    font-size: 0.76rem;
    cursor: pointer;
  }

  b {
    color: #8fd4ff;
  }
</style>
