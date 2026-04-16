<script lang="ts">
  type BadgeVariant = 'neutral' | 'info' | 'success' | 'warning' | 'danger';

  export let label = '';
  export let variant: BadgeVariant = 'neutral';
</script>

<span class={`badge badge--${variant}`}>
  {label}
</span>

<style>
  .badge {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    border: 1px solid transparent;
    border-radius: var(--radius-sm);
    padding: 1px 6px;
    font-size: 11px;
    line-height: 1.2;
    font-weight: 400;
    white-space: nowrap;
  }

  .badge--neutral {
    color: var(--color-text);
    background: color-mix(in srgb, var(--color-text-muted) 14%, transparent);
    border-color: color-mix(in srgb, var(--color-text-muted) 45%, transparent);
  }

  .badge--info {
    color: #8fd4ff;
    background: rgba(50, 117, 179, 0.2);
    border-color: rgba(109, 184, 255, 0.45);
  }

  .badge--success {
    color: #8bea9d;
    background: rgba(30, 140, 66, 0.2);
    border-color: rgba(104, 216, 137, 0.45);
  }

  .badge--warning {
    color: #f9d27d;
    background: rgba(163, 121, 24, 0.2);
    border-color: rgba(242, 190, 90, 0.45);
  }

  .badge--danger {
    color: #ff9e9e;
    background: rgba(150, 45, 45, 0.2);
    border-color: rgba(255, 138, 138, 0.45);
  }
</style>
