<script lang="ts">
  type StatusTone = 'online' | 'offline' | 'warning' | 'unknown';

  export let status: StatusTone = 'unknown';
  export let label: string | null = null;
</script>

<span class={`status-dot status-dot--${status}`} aria-hidden="true"></span>
{#if label}
  <span class="status-label">{label}</span>
{/if}

<style>
  .status-dot {
    display: inline-block;
    width: 6px;
    height: 6px;
    border-radius: 999px;
    background: var(--color-text-muted);
  }

  .status-dot--online {
    background: #61da7c;
  }

  .status-dot--offline {
    background: #ff8b8b;
  }

  .status-dot--warning {
    background: #f2c56f;
  }

  .status-dot--unknown {
    background: var(--color-text-muted);
  }

  .status-label {
    margin-left: 0.3rem;
    color: var(--color-text-muted);
    font-size: 11px;
  }
</style>
