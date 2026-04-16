<script lang="ts">
  import { onDestroy } from 'svelte';
  import { createEventDispatcher } from 'svelte';
  import { api } from '../../lib/api';
  import type { PolicyThresholds } from '../../lib/types';

  interface SaveSettingsResponse {
    ok?: boolean;
    error?: string;
  }

interface FieldConfig {
  key: ThresholdField;
  label: string;
  section: string;
  min?: number;
  max?: number;
  step?: number;
  desc: string;
}

type ThresholdField =
  | 'promote_min_attempts'
  | 'promote_min_success_rate'
  | 'promote_min_verify_rate'
  | 'promote_max_human_fix_rate'
  | 'stable_min_attempts'
  | 'stable_min_success_rate'
  | 'stable_min_verify_rate'
  | 'rollback_consecutive_failures';

const FIELD_CONFIGS: FieldConfig[] = [
  {
    key: 'promote_min_attempts',
    label: 'Min attempts',
    section: 'Canary promotion (explore → canary)',
    min: 1,
    step: 1,
    desc: 'Number of executions required before a pipeline can be promoted to canary. Prevents promoting on too little data.'
  },
  {
    key: 'promote_min_success_rate',
    label: 'Min success rate',
    section: 'Canary promotion (explore → canary)',
    min: 0,
    max: 1,
    step: 0.01,
    desc: 'Lower bound on success ratio for canary. Typical range is 0.85–0.95 depending on risk tolerance.'
  },
  {
    key: 'promote_min_verify_rate',
    label: 'Min verify rate',
    section: 'Canary promotion (explore → canary)',
    min: 0,
    max: 1,
    step: 0.01,
    desc: 'Verification pass ratio required for canary promotion. Usually stricter than success rate.'
  },
  {
    key: 'promote_max_human_fix_rate',
    label: 'Max human-fix rate',
    section: 'Canary promotion (explore → canary)',
    min: 0,
    max: 1,
    step: 0.01,
    desc: 'Upper bound on how often a human had to correct the output. Low value means CC must be reliable on its own.'
  },
  {
    key: 'stable_min_attempts',
    label: 'Min attempts',
    section: 'Stable promotion (canary → stable)',
    min: 1,
    step: 1,
    desc: 'Executions required to reach stable. Should be higher than canary threshold as stable bypasses LLM inference.'
  },
  {
    key: 'stable_min_success_rate',
    label: 'Min success rate',
    section: 'Stable promotion (canary → stable)',
    min: 0,
    max: 1,
    step: 0.01,
    desc: 'Success rate gate for stable. Typically stricter than canary (for example 0.97).'
  },
  {
    key: 'stable_min_verify_rate',
    label: 'Min verify rate',
    section: 'Stable promotion (canary → stable)',
    min: 0,
    max: 1,
    step: 0.01,
    desc: 'Verify rate gate for stable (for example 0.99). At stable, output quality must be near-perfect.'
  },
  {
    key: 'rollback_consecutive_failures',
    label: 'Rollback failures',
    section: 'Failure handling',
    min: 1,
    step: 1,
    desc: 'Consecutive failures before a pipeline is flagged Critical and demoted. Triggers rollback/stop policy on writes.'
  }
];

const THRESHOLD_FIELDS = FIELD_CONFIGS.map((field) => field.key);

  const INTEGER_FIELDS = new Set<ThresholdField>([
    'promote_min_attempts',
    'stable_min_attempts',
    'rollback_consecutive_failures'
  ]);

  export let open = false;
  export let thresholds: PolicyThresholds = {};

  const dispatch = createEventDispatcher<{ close: undefined; saved: undefined }>();

  let previousOpen = false;
  let formValues: Record<ThresholdField, string> = {
    promote_min_attempts: '',
    promote_min_success_rate: '',
    promote_min_verify_rate: '',
    promote_max_human_fix_rate: '',
    stable_min_attempts: '',
    stable_min_success_rate: '',
    stable_min_verify_rate: '',
    rollback_consecutive_failures: ''
  };
  let saving = false;
  let message: string | null = null;
  let error = false;
  let closeTimer: ReturnType<typeof setTimeout> | null = null;

  function syncFromThresholds(): void {
    let next: Record<ThresholdField, string> = { ...formValues };
    for (const key of THRESHOLD_FIELDS) {
      const value = thresholds[key];
      next = {
        ...next,
        [key]: value == null ? '' : String(value)
      };
    }
    formValues = next;
  }

  /** True when every field is blank (initial state or before policy loaded). */
  function isFormBlank(): boolean {
    return THRESHOLD_FIELDS.every((key) => !String(formValues[key] ?? '').trim());
  }

  function clearCloseTimer(): void {
    if (closeTimer) {
      clearTimeout(closeTimer);
      closeTimer = null;
    }
  }

  function closeModal(): void {
    clearCloseTimer();
    dispatch('close');
  }

  async function save(): Promise<void> {
    const policy: Record<string, number> = {};
    for (const key of THRESHOLD_FIELDS) {
      const raw = formValues[key].trim();
      if (!raw) {
        continue;
      }
      const parsed = INTEGER_FIELDS.has(key) ? parseInt(raw, 10) : parseFloat(raw);
      if (!Number.isFinite(parsed)) {
        message = `Invalid number for ${key}`;
        error = true;
        return;
      }
      policy[key] = parsed;
    }

    saving = true;
    message = 'Saving...';
    error = false;

    try {
      const response = await api.request<SaveSettingsResponse>('/api/settings', {
        method: 'POST',
        body: { policy }
      });
      if (response.ok === false) {
        message = response.error ?? 'Failed to save settings';
        error = true;
        return;
      }
      message = 'Saved';
      error = false;
      dispatch('saved');
      clearCloseTimer();
      closeTimer = setTimeout(() => {
        closeTimer = null;
        dispatch('close');
      }, 500);
    } catch (saveError) {
      message = saveError instanceof Error ? saveError.message : String(saveError);
      error = true;
    } finally {
      saving = false;
    }
  }

  $: if (open && !previousOpen) {
    clearCloseTimer();
    syncFromThresholds();
    message = null;
    error = false;
  }
  $: if (!open && previousOpen) {
    clearCloseTimer();
  }
  $: previousOpen = open;

  // If the modal opens before policy finishes loading, thresholds arrive later with `{}` →
  // filled data. Re-sync only while the form is still blank so we do not clobber in-progress edits.
  $: if (
    open &&
    THRESHOLD_FIELDS.some((key) => thresholds[key] != null) &&
    isFormBlank()
  ) {
    syncFromThresholds();
  }

  onDestroy(() => {
    clearCloseTimer();
  });
</script>

{#if open}
  <div class="modal-overlay open" role="presentation" on:click={(event) => event.target === event.currentTarget && closeModal()}>
    <div class="modal" role="dialog" aria-modal="true" aria-label="Edit thresholds">
      <button class="close-btn" type="button" aria-label="Close settings" on:click={closeModal}>✕</button>
      <h2>⚙ Policy Thresholds</h2>

      {#each FIELD_CONFIGS as config, index}
        {#if index === 0 || FIELD_CONFIGS[index - 1].section !== config.section}
          <div class="threshold-section-title">{config.section}</div>
        {/if}
        <div class="threshold-row">
          <label for={`cfg-${config.key}`}>{config.label}</label>
          <input
            id={`cfg-${config.key}`}
            type="number"
            min={config.min}
            max={config.max}
            step={config.step}
            bind:value={formValues[config.key]}
          />
          <div class="desc">{config.desc}</div>
        </div>
      {/each}

      <div class="modal-footer">
        <span class="modal-msg" class:err={error}>{message ?? ''}</span>
        <button class="modal-cancel-btn" type="button" on:click={closeModal} disabled={saving}>Cancel</button>
        <button class="modal-save-btn" type="button" on:click={() => void save()} disabled={saving}>
          {saving ? 'Saving...' : 'Save to ~/.emerge/settings.json'}
        </button>
      </div>
    </div>
  </div>
{/if}

<style>
  .modal-overlay {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.65);
    z-index: 100;
    align-items: center;
    justify-content: center;
  }

  .modal-overlay.open {
    display: flex;
  }

  .modal {
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 8px;
    width: 560px;
    max-width: 96vw;
    max-height: 90vh;
    overflow-y: auto;
    padding: 20px 24px;
  }

  .modal h2 {
    font-size: 14px;
    color: #e6edf3;
    margin-bottom: 16px;
  }

  .modal .close-btn {
    float: right;
    background: none;
    border: none;
    color: #8b949e;
    font-size: 18px;
    cursor: pointer;
    line-height: 1;
  }

  .modal .close-btn:hover {
    color: #e6edf3;
  }

  .threshold-row {
    display: grid;
    grid-template-columns: 180px 90px 1fr;
    gap: 10px;
    align-items: start;
    margin-bottom: 14px;
  }

  .threshold-row label {
    font-size: 11px;
    color: #e6edf3;
    font-weight: 600;
    padding-top: 5px;
  }

  .threshold-row input {
    background: #0d1117;
    border: 1px solid #30363d;
    border-radius: 4px;
    color: #e6edf3;
    font-size: 12px;
    padding: 4px 7px;
    width: 100%;
  }

  .threshold-row input:focus {
    outline: none;
    border-color: #58a6ff;
  }

  .threshold-row .desc {
    font-size: 10px;
    color: #8b949e;
    line-height: 1.5;
    padding-top: 5px;
  }

  .threshold-section-title {
    font-size: 10px;
    color: #484f58;
    text-transform: uppercase;
    letter-spacing: 1px;
    margin: 16px 0 8px;
    border-bottom: 1px solid #21262d;
    padding-bottom: 4px;
  }

  .modal-footer {
    display: flex;
    gap: 8px;
    justify-content: flex-end;
    margin-top: 16px;
    border-top: 1px solid #21262d;
    padding-top: 14px;
  }

  .modal-save-btn {
    background: #1a3a2a;
    border: 1px solid #3fb950;
    border-radius: 5px;
    color: #3fb950;
    font-size: 12px;
    padding: 6px 18px;
    cursor: pointer;
  }

  .modal-save-btn:hover {
    background: #2a4a3a;
  }

  .modal-cancel-btn {
    background: none;
    border: 1px solid #30363d;
    border-radius: 5px;
    color: #8b949e;
    font-size: 12px;
    padding: 6px 14px;
    cursor: pointer;
  }

  .modal-cancel-btn:hover {
    color: #e6edf3;
  }

  .modal-msg {
    font-size: 11px;
    color: #8b949e;
    margin-right: auto;
    align-self: center;
  }

  .modal-msg.err {
    color: #f85149;
  }

  .modal button:disabled {
    opacity: 0.6;
    cursor: default;
  }
</style>
