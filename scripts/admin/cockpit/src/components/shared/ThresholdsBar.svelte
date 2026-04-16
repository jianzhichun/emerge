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
  <button type="button" class="thresh-edit-btn" on:click={emitEdit}>⚙ Thresholds</button>
  <span>promote success >= <b>{thresholds.promote_min_success_rate ?? 'n/a'}</b></span>
  <span>verify >= <b>{thresholds.promote_min_verify_rate ?? 'n/a'}</b></span>
  <span>attempts >= <b>{thresholds.promote_min_attempts ?? 'n/a'}</b></span>
  <span>rollback failures = <b>{thresholds.rollback_consecutive_failures ?? 'n/a'}</b></span>
  <span>stable attempts >= <b>{thresholds.stable_min_attempts ?? 'n/a'}</b></span>
</section>

<style>
  .thresholds-bar {
    background: #0d1117;
    border-bottom: 1px solid #21262d;
    padding: 5px 16px;
    display: flex;
    gap: 16px;
    flex-wrap: wrap;
    font-size: 11px;
    color: #8b949e;
    align-items: center;
  }

  .thresholds-bar span b {
    color: #79c0ff;
  }

  .thresh-edit-btn {
    background: #161b22;
    border: 1px solid #388bfd;
    border-radius: 4px;
    color: #388bfd;
    font-size: 10px;
    padding: 2px 9px;
    cursor: pointer;
    flex-shrink: 0;
    font-family: inherit;
  }

  .thresh-edit-btn:hover {
    background: #1c2a3a;
  }
</style>
