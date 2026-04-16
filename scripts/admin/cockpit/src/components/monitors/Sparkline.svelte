<script lang="ts">
  export let activity: number[] = [];

  const maxBarHeight = 16;

  function normalizeActivity(values: number[]): number[] {
    return values.filter((value) => Number.isFinite(value) && value >= 0);
  }

  $: points = normalizeActivity(activity);
  $: peak = points.length ? Math.max(...points, 1) : 1;
  $: bars = points.map((count) => {
    const height = Math.max(1, Math.round((count / peak) * maxBarHeight));
    const alpha = height >= 12 ? '' : height >= 8 ? '66' : height >= 4 ? '33' : '22';
    const color = count > 0 ? `#3fb950${alpha}` : '#3fb95022';
    return { height, color };
  });
</script>

<div class="sparkline" aria-label="1h activity">
  {#if bars.length}
    {#each bars as bar}
      <span class="bar" style={`height:${bar.height}px;background:${bar.color}`}></span>
    {/each}
  {/if}
  <span class="label">1h activity</span>
</div>

<style>
  .sparkline {
    display: flex;
    align-items: flex-end;
    gap: 0.15rem;
    min-height: 1rem;
  }

  .bar {
    width: 0.32rem;
    border-radius: 2px 2px 0 0;
    flex: 0 0 auto;
  }

  .label {
    margin-left: 0.3rem;
    align-self: center;
    color: #6e7681;
    font-size: 0.6rem;
    letter-spacing: 0.02em;
    white-space: nowrap;
  }
</style>
