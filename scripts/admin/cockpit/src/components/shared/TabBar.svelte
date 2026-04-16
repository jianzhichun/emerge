<script lang="ts">
  import { createEventDispatcher } from 'svelte';

  interface TabBarItem {
    id: string;
    label: string;
    warn?: boolean;
    subtle?: boolean;
  }

  export let leftTabs: TabBarItem[] = [];
  export let rightTabs: TabBarItem[] = [];
  export let activeTab = '';

  const dispatch = createEventDispatcher<{ select: { id: string } }>();

  function handleSelect(id: string): void {
    dispatch('select', { id });
  }
</script>

<nav class="tab-bar" aria-label="Cockpit tabs">
  {#each leftTabs as tab}
    <button
      type="button"
      class={`tab ${tab.id === activeTab ? 'tab--active' : ''} ${tab.subtle ? 'tab--subtle' : ''}`}
      aria-current={tab.id === activeTab ? 'page' : undefined}
      on:click={() => handleSelect(tab.id)}
    >
      {tab.label}
      {#if tab.warn}
        <span class="warn">⚠</span>
      {/if}
    </button>
  {/each}

  <div class="right-tabs">
    {#each rightTabs as tab}
      <button
        type="button"
        class={`tab ${tab.id === activeTab ? 'tab--active' : ''} ${tab.subtle !== false ? 'tab--subtle' : ''}`}
        aria-current={tab.id === activeTab ? 'page' : undefined}
        on:click={() => handleSelect(tab.id)}
      >
        {tab.label}
        {#if tab.warn}
          <span class="warn">⚠</span>
        {/if}
      </button>
    {/each}
  </div>
</nav>

<style>
  .tab-bar {
    display: flex;
    border-bottom: 1px solid var(--color-border);
    background: var(--color-surface);
    overflow-x: auto;
    flex-shrink: 0;
  }

  .tab {
    appearance: none;
    border: none;
    border-bottom: 2px solid transparent;
    background: transparent;
    color: var(--color-text-muted);
    font-size: 12px;
    font-weight: 400;
    padding: 8px 16px;
    white-space: nowrap;
    flex-shrink: 0;
    cursor: pointer;
    transition: color 0.12s ease;
  }

  .tab--subtle {
    font-size: 11px;
    opacity: 0.7;
  }

  .tab:hover {
    color: var(--color-text-soft);
  }

  .tab--active {
    color: var(--color-text);
    border-bottom-color: var(--color-blue);
  }

  .right-tabs {
    margin-left: auto;
    display: flex;
  }

  .warn {
    color: var(--color-red);
    margin-left: 4px;
  }
</style>
