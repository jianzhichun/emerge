<script lang="ts">
  import { onMount } from 'svelte';
  import { createEventDispatcher } from 'svelte';
  import type { PolicyIntent, PolicyThresholds } from '../../lib/types';

  export let intents: PolicyIntent[] = [];
  export let thresholds: PolicyThresholds = {};
  export let connectorNames: string[] = [];
  export let queueSize = 0;
  let donutCanvas: HTMLCanvasElement | null = null;

  const dispatch = createEventDispatcher<{
    openConnector: { id: string };
  }>();

  function jumpToSection(sectionId: string): void {
    const section = document.getElementById(sectionId);
    section?.scrollIntoView({ behavior: 'smooth' });
  }

  function drawDonut(): void {
    if (!donutCanvas) {
      return;
    }
    const ctx = donutCanvas.getContext('2d');
    if (!ctx) {
      return;
    }
    const width = 160;
    const height = 160;
    const cx = 80;
    const cy = 80;
    const outerR = 72;
    const innerR = 44;
    const slices = [
      { value: critical.length, color: '#f85149' },
      { value: canary.length, color: '#d29922' },
      { value: stable.length, color: '#3fb950' },
      { value: explore.length, color: '#484f58' }
    ].filter((slice) => slice.value > 0);
    ctx.clearRect(0, 0, width, height);
    if (!slices.length) {
      ctx.beginPath();
      ctx.arc(cx, cy, outerR, 0, Math.PI * 2);
      ctx.strokeStyle = '#21262d';
      ctx.lineWidth = outerR - innerR;
      ctx.stroke();
    } else {
      const total = slices.reduce((sum, slice) => sum + slice.value, 0);
      let startAngle = -Math.PI / 2;
      const gap = slices.length > 1 ? 0.03 : 0;
      for (const slice of slices) {
        const sweep = (slice.value / total) * Math.PI * 2 - gap;
        const from = startAngle + gap / 2;
        const to = from + sweep;
        ctx.beginPath();
        ctx.moveTo(cx + innerR * Math.cos(from), cy + innerR * Math.sin(from));
        ctx.arc(cx, cy, outerR, from, to);
        ctx.arc(cx, cy, innerR, to, from, true);
        ctx.closePath();
        ctx.fillStyle = slice.color;
        ctx.fill();
        startAngle += sweep + gap;
      }
    }
    ctx.fillStyle = '#e6edf3';
    ctx.font = 'bold 22px SF Mono, Consolas, monospace';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(String(intentCount), cx, cy - 7);
    ctx.fillStyle = '#8b949e';
    ctx.font = '10px SF Mono, Consolas, monospace';
    ctx.fillText('intents', cx, cy + 11);
  }

  $: rollbackThreshold = Number(thresholds.rollback_consecutive_failures ?? 2);
  $: critical = intents.filter((intent) => Number(intent.consecutive_failures ?? 0) >= rollbackThreshold);
  $: canary = intents.filter(
    (intent) => Number(intent.consecutive_failures ?? 0) < rollbackThreshold && String(intent.stage ?? 'explore') === 'canary'
  );
  $: stable = intents.filter(
    (intent) => Number(intent.consecutive_failures ?? 0) < rollbackThreshold && String(intent.stage ?? 'explore') === 'stable'
  );
  $: explore = intents.filter((intent) => {
    if (Number(intent.consecutive_failures ?? 0) >= rollbackThreshold) {
      return false;
    }
    const status = String(intent.stage ?? 'explore');
    return status !== 'stable' && status !== 'canary';
  });
  $: intentCount = intents.length;
  $: totalRollbacks = intents.reduce((sum, intent) => sum + Number(intent.rollback_executed_count ?? 0), 0);
  $: totalFailures = intents.reduce((sum, intent) => sum + Number(intent.consecutive_failures ?? 0), 0);
  $: connectorLoad = intents.reduce<Record<string, number>>((acc, intent) => {
    const key = String(intent.key ?? '');
    const connector = key.split('.')[0] || 'unknown';
    acc[connector] = (acc[connector] ?? 0) + 1;
    return acc;
  }, {});
  $: exploreByConnector = explore.reduce<Record<string, number>>((acc, intent) => {
    const key = String(intent.key ?? '');
    const connector = key.split('.')[0] || 'unknown';
    acc[connector] = (acc[connector] ?? 0) + 1;
    return acc;
  }, {});
  $: knownConnectorSet = new Set(connectorNames);
  $: knownExploreConnectors = Object.entries(exploreByConnector)
    .filter(([connector]) => knownConnectorSet.has(connector))
    .sort((a, b) => b[1] - a[1]);
  $: knownCriticalConnectors = Object.entries(
    critical.reduce<Record<string, number>>((acc, intent) => {
      const connector = String(intent.key ?? '').split('.')[0] || 'unknown';
      acc[connector] = (acc[connector] ?? 0) + 1;
      return acc;
    }, {})
  )
    .filter(([connector]) => knownConnectorSet.has(connector))
    .sort((a, b) => b[1] - a[1]);
  $: knownCanaryConnectors = Object.entries(
    canary.reduce<Record<string, number>>((acc, intent) => {
      const connector = String(intent.key ?? '').split('.')[0] || 'unknown';
      acc[connector] = (acc[connector] ?? 0) + 1;
      return acc;
    }, {})
  )
    .filter(([connector]) => knownConnectorSet.has(connector))
    .sort((a, b) => b[1] - a[1]);
  $: knownStableConnectors = Object.entries(
    stable.reduce<Record<string, number>>((acc, intent) => {
      const connector = String(intent.key ?? '').split('.')[0] || 'unknown';
      acc[connector] = (acc[connector] ?? 0) + 1;
      return acc;
    }, {})
  )
    .filter(([connector]) => knownConnectorSet.has(connector))
    .sort((a, b) => b[1] - a[1]);
  $: otherExploreCount = Object.entries(exploreByConnector)
    .filter(([connector]) => !knownConnectorSet.has(connector))
    .reduce((sum, [, count]) => sum + count, 0);
  $: topLoad = Object.entries(connectorLoad).sort((a, b) => b[1] - a[1]).slice(0, 4);
  $: maxLoad = topLoad.length ? topLoad[0][1] : 1;
  $: tracked = [...stable, ...canary];
  $: avgSuccess = (() => {
    const values = tracked
      .map((intent) => (typeof intent.success_rate === 'number' ? intent.success_rate : null))
      .filter((value): value is number => value != null);
    if (!values.length) {
      return null;
    }
    return `${((values.reduce((sum, value) => sum + value, 0) / values.length) * 100).toFixed(1)}%`;
  })();
  $: avgVerify = (() => {
    const values = tracked
      .map((intent) => (typeof intent.verify_rate === 'number' ? intent.verify_rate : null))
      .filter((value): value is number => value != null);
    if (!values.length) {
      return null;
    }
    return `${((values.reduce((sum, value) => sum + value, 0) / values.length) * 100).toFixed(1)}%`;
  })();
  $: connectorCount = Object.keys(connectorLoad).length;
  $: {
    critical.length;
    canary.length;
    stable.length;
    explore.length;
    intentCount;
    drawDonut();
  }

  onMount(() => {
    drawDonut();
  });
</script>

<section class="overview-layout">
  <section class="report overview-grid">
    <div class="report-donut-wrap">
      <canvas bind:this={donutCanvas} width="160" height="160"></canvas>
      <div class="report-donut-legend">
        <button type="button" class="legend-row" on:click={() => jumpToSection('ov-sec-critical')}><span class="dot critical"></span><span class="legend-label">Critical</span><b>{critical.length}</b></button>
        <button type="button" class="legend-row" on:click={() => jumpToSection('ov-sec-canary')}><span class="dot canary"></span><span class="legend-label">Canary</span><b>{canary.length}</b></button>
        <button type="button" class="legend-row" on:click={() => jumpToSection('ov-sec-stable')}><span class="dot stable"></span><span class="legend-label">Stable</span><b>{stable.length}</b></button>
        <button type="button" class="legend-row" on:click={() => jumpToSection('ov-sec-explore')}><span class="dot explore"></span><span class="legend-label">Explore</span><b>{explore.length}</b></button>
      </div>
    </div>

    <div class="report-cards">
      <article class="report-card">
        <div class="report-num">{intentCount}</div>
        <div class="report-label">Total Intents</div>
        <div class="report-sub">{connectorCount} connectors</div>
      </article>
      <article class="report-card {critical.length ? 'critical' : ''}">
        <div class="report-num critical">{critical.length}</div>
        <div class="report-label">Critical</div>
        <div class="report-sub">{critical.length ? 'needs attention' : 'all clear'}</div>
      </article>
      <article class="report-card {canary.length ? 'canary' : ''}">
        <div class="report-num canary">{canary.length}</div>
        <div class="report-label">Canary</div>
        <div class="report-sub">testing in flight</div>
      </article>
      <article class="report-card {stable.length ? 'stable' : ''}">
        <div class="report-num stable">{stable.length}</div>
        <div class="report-label">Stable</div>
        <div class="report-sub">{avgSuccess ?? 'no data yet'}</div>
      </article>
      <article class="report-card">
        <div class="report-num neutral">{avgSuccess ?? '—'}</div>
        <div class="report-label">Avg Success</div>
        <div class="report-sub">stable + canary</div>
      </article>
      <article class="report-card">
        <div class="report-num neutral">{avgVerify ?? '—'}</div>
        <div class="report-label">Avg Verify</div>
        <div class="report-sub">stable + canary</div>
      </article>
      <article class="report-card">
        <div class="report-num {totalRollbacks > 0 ? 'critical' : 'neutral'}">{totalRollbacks}</div>
        <div class="report-label">Rollbacks</div>
        <div class="report-sub">{totalFailures} total active failures</div>
      </article>
      <article class="report-card">
        <div class="report-num {queueSize > 0 ? 'canary' : 'neutral'}">{queueSize}</div>
        <div class="report-label">Queued Actions</div>
        <div class="report-sub">{queueSize ? 'pending submit' : 'queue empty'}</div>
      </article>
      <article class="report-card span-2">
        <div class="report-label">Status Distribution</div>
        <div class="report-sub">critical / canary / stable / explore</div>
        <div class="stat-dist">
          <span class="critical" style={`width:${intentCount ? (critical.length / intentCount) * 100 : 0}%`}></span>
          <span class="canary" style={`width:${intentCount ? (canary.length / intentCount) * 100 : 0}%`}></span>
          <span class="stable" style={`width:${intentCount ? (stable.length / intentCount) * 100 : 0}%`}></span>
          <span class="explore" style={`width:${intentCount ? (explore.length / intentCount) * 100 : 0}%`}></span>
        </div>
      </article>
      <article class="report-card span-2">
        <div class="report-label">Top Connector Load</div>
        <div class="report-load">
          {#if !topLoad.length}
            <p>(no data)</p>
          {:else}
            {#each topLoad as [connector, count]}
              <div class="load-row">
                <span class="load-name">{connector}</span>
                <div class="load-track"><div class="load-bar" style={`width:${Math.max(6, (count / maxLoad) * 100)}%`}></div></div>
                <b>{count}</b>
              </div>
            {/each}
          {/if}
        </div>
      </article>
    </div>
  </section>

  <div class="groups">
    <section id="ov-sec-critical">
      <h3 class="section-header critical">⚠ Critical ({critical.length})</h3>
      <p class="group-note">Use connector tabs for per-intent actions.</p>
      <div class="group-connectors">
        {#each knownCriticalConnectors as [connector, count]}
          <button type="button" class="explore-chip" on:click={() => dispatch('openConnector', { id: connector })}>
            <b>{count}</b> {connector}
          </button>
        {/each}
      </div>
    </section>

    <section id="ov-sec-canary">
      <h3 class="section-header canary">⟳ Canary ({canary.length})</h3>
      <p class="group-note">Use connector tabs for rollout and demotion actions.</p>
      <div class="group-connectors">
        {#each knownCanaryConnectors as [connector, count]}
          <button type="button" class="explore-chip" on:click={() => dispatch('openConnector', { id: connector })}>
            <b>{count}</b> {connector}
          </button>
        {/each}
      </div>
    </section>

    <section id="ov-sec-stable">
      <h3 class="section-header stable">✓ Stable ({stable.length})</h3>
      <p class="group-note">Stable intents are listed by connector tabs.</p>
      <div class="group-connectors">
        {#each knownStableConnectors as [connector, count]}
          <button type="button" class="explore-chip" on:click={() => dispatch('openConnector', { id: connector })}>
            <b>{count}</b> {connector}
          </button>
        {/each}
      </div>
    </section>

    <section id="ov-sec-explore">
      <h3 class="section-header explore">◯ Explore ({explore.length})</h3>
      {#if knownExploreConnectors.length || otherExploreCount > 0}
        <div class="explore-summary">
          <div class="explore-title">Explore intents by connector</div>
          <div class="explore-connectors">
            {#each knownExploreConnectors as [connector, count]}
              <button
                type="button"
                class="explore-chip"
                on:click={() => dispatch('openConnector', { id: connector })}
              >
                <b>{count}</b> {connector}
              </button>
            {/each}
            {#if otherExploreCount > 0}
              <span class="explore-chip disabled"><b>{otherExploreCount}</b> other</span>
            {/if}
          </div>
        </div>
      {/if}
    </section>
  </div>
</section>

<style>
  .overview-layout {
    display: flex;
    flex-direction: column;
    gap: 8px;
    align-items: stretch;
  }

  .report {
    grid-area: report;
    width: 100%;
  }

  .overview-grid {
    display: grid;
    grid-template-columns: 230px 1fr;
    gap: 12px;
    margin: 2px 0 4px;
    align-items: start;
  }

  .report-donut-wrap {
    background: #161b22;
    border: 1px solid #21262d;
    border-radius: 8px;
    padding: 14px;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 8px;
  }

  .report-donut-legend {
    width: 100%;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .legend-row {
    display: flex;
    align-items: center;
    gap: 7px;
    font-size: 11px;
    cursor: pointer;
    border: none;
    background: transparent;
    color: inherit;
    padding: 0;
    text-align: left;
  }

  .legend-row:hover {
    color: #e6edf3;
  }

  .legend-label {
    flex: 1;
    color: #8b949e;
  }

  .legend-row b {
    color: #e6edf3;
    font-weight: 600;
  }

  .dot {
    width: 9px;
    height: 9px;
    border-radius: 999px;
    display: inline-block;
    flex-shrink: 0;
  }

  .dot.critical { background: #f85149; }
  .dot.canary { background: #d29922; }
  .dot.stable { background: #3fb950; }
  .dot.explore { background: #484f58; }

  .report-cards {
    display: grid;
    grid-template-columns: repeat(4, minmax(130px, 1fr));
    gap: 8px;
  }

  .report-card {
    background: #161b22;
    border: 1px solid #21262d;
    border-radius: 6px;
    padding: 9px 10px;
    min-height: 78px;
    display: flex;
    flex-direction: column;
    justify-content: center;
  }

  .report-card.critical {
    border-color: #4a1a1a;
  }

  .report-card.canary {
    border-color: #4a3a10;
  }

  .report-card.stable {
    border-color: #2a4a2a;
  }

  .report-card.span-2 {
    grid-column: span 2;
  }

  .report-num {
    font-size: 26px;
    font-weight: 700;
    line-height: 1;
    color: #e6edf3;
  }

  .report-num.critical {
    color: #f85149;
  }

  .report-num.canary {
    color: #d29922;
  }

  .report-num.stable {
    color: #3fb950;
  }

  .report-num.neutral {
    color: #8b949e;
  }

  .report-label {
    margin-top: 3px;
    font-size: 10px;
    color: #484f58;
    text-transform: uppercase;
    letter-spacing: 0.8px;
  }

  .report-sub {
    margin-top: 5px;
    font-size: 10px;
    color: #8b949e;
  }

  .stat-dist {
    margin-top: 8px;
    height: 8px;
    border-radius: 99px;
    overflow: hidden;
    display: flex;
    background: #0d1117;
    border: 1px solid #21262d;
  }

  .stat-dist > span {
    height: 100%;
    display: inline-block;
    min-width: 2px;
  }

  .stat-dist .critical { background: #f85149; }
  .stat-dist .canary { background: #d29922; }
  .stat-dist .stable { background: #3fb950; }
  .stat-dist .explore { background: #484f58; }

  .report-load {
    display: flex;
    flex-direction: column;
    gap: 5px;
    margin-top: 6px;
  }

  .report-load p {
    margin: 0;
    color: #8b949e;
    font-size: 10px;
  }

  .load-row {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 10px;
  }

  .load-name {
    width: 72px;
    color: #8b949e;
    overflow: hidden;
    white-space: nowrap;
    text-overflow: ellipsis;
  }

  .load-track {
    flex: 1;
    height: 7px;
    border-radius: 99px;
    overflow: hidden;
    background: #0d1117;
    border: 1px solid #21262d;
  }

  .load-bar {
    height: 100%;
    background: #1f6feb;
  }

  .load-row b {
    width: 22px;
    text-align: right;
    color: #e6edf3;
    font-size: 10px;
    font-weight: 600;
  }

  .groups {
    width: 100%;
    display: flex;
    flex-direction: column;
    gap: 0;
  }

  .groups > section {
    border: none;
    border-radius: 0;
    background: transparent;
    padding: 0;
    margin: 0;
  }

  .section-header {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 1px;
    margin: 14px 0 6px;
  }

  .section-header.critical { color: #f85149; }
  .section-header.canary { color: #d29922; }
  .section-header.stable { color: #3fb950; }
  .section-header.explore { color: #8b949e; }

  .explore-summary {
    background: #161b22;
    border: 1px solid #21262d;
    border-radius: 6px;
    padding: 10px 12px;
    margin-bottom: 8px;
  }

  .explore-title {
    font-size: 11px;
    color: #8b949e;
    margin-bottom: 6px;
  }

  .explore-connectors {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }

  .explore-chip {
    background: #1c2128;
    border: 1px solid #30363d;
    border-radius: 12px;
    padding: 2px 9px;
    font-size: 10px;
    color: #8b949e;
    cursor: pointer;
  }

  .explore-chip:hover {
    border-color: #58a6ff;
    color: #58a6ff;
  }

  .explore-chip b {
    color: #e6edf3;
  }

  .explore-chip.disabled {
    opacity: 0.45;
    cursor: default;
  }

  .group-note {
    margin: 0 0 6px;
    font-size: 11px;
    color: #8b949e;
  }

  .group-connectors {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }

  @media (max-width: 1450px) {
    .report-cards {
      grid-template-columns: repeat(2, minmax(130px, 1fr));
    }
  }

  @media (max-width: 70rem) {
    .overview-grid {
      grid-template-columns: 1fr;
    }
  }
</style>
