<script lang="ts">
  import { api } from '../../lib/api';
  import { goalStore } from '../../stores/goal';

  let draftGoal = '';
  let editing = false;
  let saving = false;
  let feedback: string | null = null;

  function syncDraftFromStore(): void {
    if (editing) {
      return;
    }
    draftGoal = $goalStore.active?.goal ?? '';
  }

  async function saveGoal(): Promise<void> {
    if (saving || $goalStore.loading) {
      return;
    }
    const goal = draftGoal.trim();
    saving = true;
    feedback = null;
    try {
      const response = await api.postGoal({ goal });
      if (response.ok === false) {
        feedback = `Goal rejected: ${response.error ?? response.message ?? 'policy'}`;
      } else {
        feedback = `Goal saved (v${response.goal_version ?? '?'})`;
      }
      editing = false;
      await goalStore.refresh();
    } catch (error) {
      feedback = error instanceof Error ? error.message : String(error);
    } finally {
      saving = false;
    }
  }

  function handleInput(): void {
    editing = true;
  }

  function handleBlur(): void {
    editing = false;
    syncDraftFromStore();
  }

  $: syncDraftFromStore();
  $: goalVersion = $goalStore.active?.goal_version ?? '?';
  $: goalSource = $goalStore.active?.goal_source ?? 'unset';
</script>

<section class="goal-bar">
  <label for="goal-input">Goal</label>
  <input
    id="goal-input"
    type="text"
    bind:value={draftGoal}
    on:input={handleInput}
    on:blur={handleBlur}
    on:keydown={(event) => event.key === 'Enter' && void saveGoal()}
    placeholder="What is CC working on?"
    maxlength="120"
  />
  <button type="button" on:click={() => void saveGoal()} disabled={saving || $goalStore.loading}>
    {saving ? 'Saving...' : 'Save'}
  </button>
  <span class="meta">v{goalVersion} · {goalSource}</span>
  {#if feedback}
    <span class="feedback">{feedback}</span>
  {:else if $goalStore.error}
    <span class="feedback error">{$goalStore.error}</span>
  {/if}
</section>

<style>
  .goal-bar {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
    border: 1px solid color-mix(in srgb, var(--color-text-muted) 40%, transparent);
    border-radius: 0.7rem;
    background: color-mix(in srgb, var(--color-bg) 86%, black);
    padding: 0.5rem 0.7rem;
  }

  label {
    font-size: 0.72rem;
    color: var(--color-text-muted);
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }

  input {
    flex: 1 1 20rem;
    min-width: 12rem;
    border: 1px solid color-mix(in srgb, var(--color-text-muted) 40%, transparent);
    border-radius: 0.45rem;
    background: color-mix(in srgb, var(--color-bg) 92%, black);
    color: var(--color-text);
    padding: 0.35rem 0.45rem;
    font-size: 0.84rem;
  }

  button {
    border: 1px solid rgba(97, 218, 124, 0.45);
    border-radius: 0.45rem;
    background: rgba(30, 140, 66, 0.2);
    color: #8bea9d;
    padding: 0.3rem 0.55rem;
    font-size: 0.8rem;
    cursor: pointer;
  }

  button:disabled {
    opacity: 0.6;
    cursor: default;
  }

  .meta {
    color: var(--color-text-muted);
    font-size: 0.72rem;
    white-space: nowrap;
  }

  .feedback {
    font-size: 0.72rem;
    color: #8bea9d;
  }

  .feedback.error {
    color: #ff9e9e;
  }
</style>
