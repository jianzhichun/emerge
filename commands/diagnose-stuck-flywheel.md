---
description: Diagnose why an Emerge intent is not reaching zero-inference stable execution
---

# Diagnose Stuck Flywheel

1. Read current policy and recent spans with `icc_span_status`, `policy://current`, or `state://deltas`.
2. Check whether `stage` is blocked by low success, low verify rate, frozen policy, or bridge demotion.
3. Inspect synthesis events in `scripts/watch_emerge.py` output, especially `synthesis_job_ready`, `synthesis_blocked`, and bridge failure reasons.
4. If Memory Hub is involved, check `icc_hub(action="status")` before assuming local state is authoritative.
5. Report the smallest next action: run a verified span, fix a blocked synthesis job, unfreeze policy, or repair a broken bridge.

Never write policy state directly; all lifecycle changes must flow through the daemon tools and `PolicyEngine`.
