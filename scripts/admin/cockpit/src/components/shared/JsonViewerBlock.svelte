<script lang="ts">
  import JsonTreeNode from './JsonTreeNode.svelte';

  export let viewerId: string;
  export let data: unknown = {};

  type ViewMode = 'tree' | 'raw';
  let mode: ViewMode = 'tree';

  function esc(s: string): string {
    return s
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  async function copyJson(): Promise<void> {
    const text = JSON.stringify(data ?? {}, null, 2);
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
    }
  }

  $: rawActive = mode === 'raw';
  $: treeActive = mode === 'tree';
  $: pretty = JSON.stringify(data ?? {}, null, 2);
</script>

<div class="state-json-toolbar">
  <button type="button" class="cp-btn-sm" on:click={() => void copyJson()}>Copy JSON</button>
  <button type="button" class="cp-btn-sm" class:warn={treeActive} on:click={() => (mode = 'tree')}>Tree</button>
  <button type="button" class="cp-btn-sm" class:warn={rawActive} on:click={() => (mode = 'raw')}>Raw</button>
</div>
{#if rawActive}
  <pre class="state-json" id="state-json-view-{viewerId}">{pretty}</pre>
{:else}
  <div class="state-json json-tree" id="state-json-view-{viewerId}">
    <JsonTreeNode value={data} name={null} path="root" depth={0} />
  </div>
{/if}
