---
name: policy-optimization
description: Use when policy health needs tuning: too many explore pipelines, repeated failures, stalled promotions, or noisy rollback behavior. Produces a prioritized optimization plan with safe threshold recommendations.
---

# Policy Optimization

## Overview

Use this skill when `/policy` output shows drift, stalls, or noisy failure patterns.
Goal: improve promotion quality and stability without unsafe threshold changes.

Core principle: diagnose first, tune second. Do not change thresholds without
clear evidence from attempts, success_rate, verify_rate, and failure patterns.

## When to Use

- `explore` count is high and long-lived.
- Any pipeline has `consecutive_failures >= 1`.
- `canary` pipelines fail to reach `stable` despite enough attempts.
- Rollbacks are frequent (`rollback_executed_count` grows).
- The team asks "which policy threshold should we tune next?"

Do not use when:

- The request is only to display status (use `policy` command only).
- There are no meaningful signals (too little data / low attempts).

## Workflow

### 1) Capture policy snapshot

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" policy-status --pretty
```

If parsing is needed:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" policy-status
```

### 2) Classify risk buckets

Classify each pipeline into one bucket:

- **Critical**: `consecutive_failures >= rollback_consecutive_failures`
- **Warning**: `consecutive_failures == 1` or verify_rate materially low
- **Stalled**: high attempts but still `explore`/`canary`
- **Healthy**: stable or trend strongly positive

### 3) Prioritize remediation

Priority order:

1. Fix `Critical` pipelines first (execution correctness and rollback safety)
2. Fix high-volume `Warning` pipelines
3. Promote `Stalled` but healthy candidates (remove lifecycle friction)
4. Leave `Healthy` unchanged

Tie-breakers:
- Higher `consecutive_failures` first
- Then lower `verify_rate`
- Then higher policy traffic (`policy_enforced_count`)

### 4) Propose threshold tuning (guardrailed)

Threshold changes are allowed only when:

- Sample size is credible (attempts near/above promotion thresholds)
- Signal is consistent across multiple pipelines (not one-off noise)
- A specific failure mode is identified

Guardrails:

- Never relax all gates at once.
- Change one threshold group at a time, then observe.
- Keep rollback protection conservative (`rollback_consecutive_failures`).
- Prefer pipeline fixes over threshold relaxation when failures are deterministic.

### 5) Define verification window

After any tuning, run a short observation window and re-check:

- status movement (`explore -> canary -> stable`)
- success/verify trend
- new consecutive failure bursts
- rollback/stop counts

## Output Contract

Return a concise optimization report:

1. **Snapshot**: total pipelines, status distribution, threshold values
2. **Findings**: critical/warning/stalled groups with evidence
3. **Actions**: top 3-5 actions in execution order
4. **Threshold proposal**: optional, with explicit risk statement
5. **Verification plan**: what to watch and pass/fail criteria

## Common Mistakes

| Mistake | Better approach |
|---|---|
| Tune thresholds from one bad run | Wait for enough attempts and consistent pattern |
| Lower multiple promotion gates together | Change one gate group, then observe |
| Ignore verify_rate and focus only on success_rate | Treat verify as first-class gate for safety |
| Keep retrying a broken pipeline in explore | Fix pipeline logic before policy tuning |
| Optimize by intuition | Base every action on measurable policy signals |
