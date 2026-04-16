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
    width: 0.625rem;
    height: 0.625rem;
    border-radius: 999px;
    background: var(--color-text-muted);
    box-shadow: 0 0 0 1px rgba(255, 255, 255, 0.1) inset;
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
    margin-left: 0.4rem;
    color: var(--color-text-muted);
    font-size: 0.75rem;
  }
</style>
