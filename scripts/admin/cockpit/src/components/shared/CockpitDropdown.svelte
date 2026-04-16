<script lang="ts">
  import { createEventDispatcher, onMount } from 'svelte';

  export let options: { value: string; label: string }[] = [];
  export let value = '';
  /**
   * When set (including while loading), overrides the label resolved from `options` + `value`.
   * Use `undefined` to show the resolved option label.
   */
  export let triggerDisplay: string | undefined = undefined;
  export let emptyMenuLabel = '(empty)';
  export let title = '';
  export let ariaLabel = '';
  /** Optional `id` of a visible label element (e.g. small caps “Session”). */
  export let labelledBy = '';
  /** Stable id for a11y / tests */
  export let dropdownId = '';
  export let minWidth = '180px';
  export let maxWidth = 'min(360px, 38vw)';

  let open = false;
  let root: HTMLDivElement | undefined;

  const dispatch = createEventDispatcher<{ change: { value: string } }>();

  $: triggerLabel =
    triggerDisplay !== undefined
      ? triggerDisplay
      : (() => {
          const hit = options.find((o) => o.value === value);
          return hit?.label ?? (value ? value : '—');
        })();

  function toggle(): void {
    open = !open;
  }

  function pick(next: string): void {
    open = false;
    dispatch('change', { value: next });
  }

  onMount(() => {
    const onDoc = (event: MouseEvent) => {
      if (root && !root.contains(event.target as Node)) {
        open = false;
      }
    };
    document.addEventListener('click', onDoc);
    return () => document.removeEventListener('click', onDoc);
  });
</script>

<div
  id={dropdownId || undefined}
  class="cockpit-dropdown"
  bind:this={root}
  style={`min-width: ${minWidth}; max-width: ${maxWidth}`}
>
  <button
    type="button"
    class="cockpit-dropdown-trigger"
    {title}
    aria-labelledby={labelledBy || undefined}
    aria-label={labelledBy ? undefined : ariaLabel || title}
    aria-expanded={open}
    aria-haspopup="listbox"
    on:click|stopPropagation={() => toggle()}
  >
    {triggerLabel}
  </button>
  <div class="cockpit-dropdown-menu" class:open={open} role="listbox">
    {#if !options.length}
      <div class="cockpit-dropdown-empty">{emptyMenuLabel}</div>
    {:else}
      {#each options as opt (opt.value)}
        <button
          type="button"
          class="cockpit-dropdown-item"
          class:active={opt.value === value}
          role="option"
          aria-selected={opt.value === value}
          on:click|stopPropagation={() => pick(opt.value)}
        >
          {opt.label}
        </button>
      {/each}
    {/if}
  </div>
</div>
