<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '../../lib/api';
  import { goalStore } from '../../stores/goal';
  import type { GoalHistoryEvent } from '../../lib/types';
  import CockpitDropdown from './CockpitDropdown.svelte';

  export let embedded = false;

  let draftGoal = '';
  let editing = false;
  let saving = false;
  let feedback: string | null = null;
  let history: GoalHistoryEvent[] = [];
  let selectedGoalEventId = '';

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
      await refreshGoalHistory();
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

  function goalHistoryLabel(event: GoalHistoryEvent): string {
    const ts = Number(event.ts_ms ?? 0);
    const stamp = ts ? new Date(ts).toLocaleString() : '--';
    const text = String(event.text ?? event.goal ?? '').trim();
    const short = text.length > 68 ? `${text.slice(0, 68)}...` : text || '(empty)';
    return `${stamp} · ${short}`;
  }

  async function refreshGoalHistory(): Promise<void> {
    try {
      const payload = await api.getGoalHistory(30);
      history = (payload.events ?? []).slice().reverse();
      if (!selectedGoalEventId && history.length) {
        selectedGoalEventId = String(history[0].event_id ?? '');
      } else if (selectedGoalEventId && !history.some((item) => String(item.event_id ?? '') === selectedGoalEventId)) {
        selectedGoalEventId = history.length ? String(history[0].event_id ?? '') : '';
      }
    } catch {
      history = [];
      selectedGoalEventId = '';
    }
  }

  async function rollbackGoalToSelected(): Promise<void> {
    if (!selectedGoalEventId) {
      feedback = 'Select a goal event first';
      return;
    }
    if (!window.confirm('Rollback goal to selected history event?')) {
      return;
    }
    try {
      const response = await api.rollbackGoal(selectedGoalEventId);
      if (response.ok === false) {
        feedback = `Rollback failed: ${response.error ?? 'unknown error'}`;
      } else {
        feedback = 'Goal rollback complete';
        await Promise.all([goalStore.refresh(), refreshGoalHistory()]);
      }
    } catch (error) {
      feedback = error instanceof Error ? error.message : String(error);
    }
  }

  $: syncDraftFromStore();
  $: goalVersion = $goalStore.active?.goal_version ?? '?';
  $: goalSource = $goalStore.active?.goal_source ?? 'unset';
  $: goalHistoryOptions = history.map((ev) => ({
    value: String(ev.event_id ?? ''),
    label: goalHistoryLabel(ev)
  }));

  onMount(() => {
    void refreshGoalHistory();
  });
</script>

<section class={`goal-bar ${embedded ? 'embedded' : ''}`}>
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
  <CockpitDropdown
    dropdownId="goal-history-dropdown"
    options={goalHistoryOptions}
    value={selectedGoalEventId}
    triggerDisplay={history.length ? undefined : '(loading goal history...)'}
    emptyMenuLabel="(no history)"
    title="Recent goal events"
    ariaLabel="Recent goal events"
    minWidth="220px"
    maxWidth="320px"
    on:change={(event) => {
      selectedGoalEventId = event.detail.value;
    }}
  />
  <button class="goal-rollback-btn" type="button" on:click={() => void rollbackGoalToSelected()} title="Rollback to selected event">
    Rollback
  </button>
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
    gap: 6px;
    flex-wrap: wrap;
    background: var(--color-surface);
    border-bottom: 1px solid var(--color-border-dim);
    padding: 6px 16px;
  }

  .goal-bar.embedded {
    background: transparent;
    border-bottom: none;
    padding: 0;
    flex: 1;
    min-width: 360px;
  }

  label {
    font-size: 10px;
    color: var(--color-text-faint);
    text-transform: uppercase;
    letter-spacing: 0.8px;
    flex-shrink: 0;
  }

  input {
    flex: 1;
    min-width: 180px;
    border: none;
    border-bottom: 1px solid var(--color-border);
    border-radius: 0;
    background: transparent;
    color: var(--color-text);
    padding: 2px 4px;
    font-size: 12px;
    outline: none;
  }

  input:focus {
    border-bottom-color: var(--color-blue);
  }

  button {
    border: none;
    border-radius: 0;
    background: none;
    color: var(--color-text-faint);
    padding: 0 4px;
    font-size: 11px;
    cursor: pointer;
  }

  button:hover {
    color: var(--color-green);
  }

  button:disabled {
    opacity: 0.6;
    cursor: default;
  }

  .meta {
    color: var(--color-text-muted);
    font-size: 10px;
    white-space: nowrap;
    margin-left: 6px;
  }

  .goal-rollback-btn {
    background: none;
    border: 1px solid var(--color-border);
    color: var(--color-text-muted);
    border-radius: var(--radius-sm);
    font-size: 11px;
    cursor: pointer;
    padding: 2px 6px;
  }

  .goal-rollback-btn:hover {
    color: #f0f6fc;
    border-color: var(--color-text-muted);
  }

  .feedback {
    font-size: 10px;
    color: var(--color-green);
  }

  .feedback.error {
    color: var(--color-red);
  }
</style>
