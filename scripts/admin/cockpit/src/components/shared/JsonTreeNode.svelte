<script lang="ts">
  import JsonTreeNode from './JsonTreeNode.svelte';

  /** Scalar or nested object/array — matches legacy `cockpit_shell` JSON tree. */
  export let value: unknown;
  export let name: string | number | null = null;
  export let path: string;
  export let depth = 0;

  /** Uncontrolled after mount; initial expand matches legacy (depth &lt; 2). */
  let expanded = depth < 2;

  function esc(s: string): string {
    return s
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  $: keyHtml =
    name === null
      ? ''
      : `<span class="json-tree-key">${esc(String(name))}</span>: `;
  $: isObj = value !== null && typeof value === 'object';
  $: nodePath = path;
  $: isArr = Array.isArray(value);
  $: entries = isObj
    ? isArr
      ? (value as unknown[]).map((v, i) => [i, v] as [number, unknown])
      : Object.entries(value as Record<string, unknown>)
    : [];
</script>

{#if !isObj}
  <div class="json-tree-leaf">
    {@html keyHtml}
    {#if value === null}
      <span class="json-tree-value null">null</span>
    {:else if typeof value === 'string'}
      <span class="json-tree-value str">"{esc(value)}"</span>
    {:else if typeof value === 'number'}
      <span class="json-tree-value num">{esc(String(value))}</span>
    {:else if typeof value === 'boolean'}
      <span class="json-tree-value bool">{value ? 'true' : 'false'}</span>
    {:else}
      <span class="json-tree-value">{esc(String(value))}</span>
    {/if}
  </div>
{:else}
  <details class="json-tree-node" data-node-path={nodePath} bind:open={expanded}>
    <summary>
      {@html keyHtml}<span class="json-tree-type">{isArr ? 'Array' : 'Object'}</span><span class="json-tree-count"
        >({entries.length})</span
      >
    </summary>
    <div class="json-tree-children">
      {#if entries.length === 0}
        <div class="json-tree-leaf"><span class="json-tree-type">(empty)</span></div>
      {:else}
        {#each entries as [ek, ev] (String(ek) + nodePath)}
          <JsonTreeNode
            value={ev}
            name={ek}
            path={`${nodePath}.${String(ek)}`}
            depth={depth + 1}
          />
        {/each}
      {/if}
    </div>
  </details>
{/if}
