<script lang="ts">
  import { createEventDispatcher } from 'svelte';

  interface TabBarItem {
    id: string;
    label: string;
  }

  export let tabs: TabBarItem[] = [];
  export let activeTab = '';

  const dispatch = createEventDispatcher<{ select: { id: string } }>();

  function handleSelect(id: string): void {
    dispatch('select', { id });
  }
</script>

<nav class="tab-bar" aria-label="Cockpit tabs">
  {#each tabs as tab}
    <button
      type="button"
      class={`tab ${tab.id === activeTab ? 'tab--active' : ''}`}
      aria-current={tab.id === activeTab ? 'page' : undefined}
      on:click={() => handleSelect(tab.id)}
    >
      {tab.label}
    </button>
  {/each}
</nav>

<style>
  .tab-bar {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
    padding: 0.4rem;
    border: 1px solid color-mix(in srgb, var(--color-text-muted) 45%, transparent);
    border-radius: 0.7rem;
    background: color-mix(in srgb, var(--color-bg) 78%, black);
  }

  .tab {
    appearance: none;
    border: 1px solid transparent;
    border-radius: 0.55rem;
    background: transparent;
    color: var(--color-text-muted);
    font-size: 0.88rem;
    font-weight: 600;
    padding: 0.45rem 0.8rem;
    cursor: pointer;
    transition: border-color 0.12s ease, color 0.12s ease, background 0.12s ease;
  }

  .tab:hover {
    color: var(--color-text);
    border-color: color-mix(in srgb, var(--color-text-muted) 45%, transparent);
  }

  .tab--active {
    color: var(--color-text);
    background: color-mix(in srgb, var(--color-text-muted) 20%, transparent);
    border-color: color-mix(in srgb, var(--color-text-muted) 55%, transparent);
  }
</style>
