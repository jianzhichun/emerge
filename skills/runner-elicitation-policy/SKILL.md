---
name: runner-elicitation-policy
description: Use when deciding whether a runner should interrupt the operator, how popup text should be phrased, and how timeout/failure classes should be interpreted.
---

# Runner Elicitation Policy

## Purpose

Runner popups are scarce attention. Use them only when operator input changes the outcome and the system cannot safely proceed from existing evidence.

## Interrupt Only When

- The intent is ambiguous and a wrong choice would create or reinforce the wrong pipeline.
- The action is irreversible or high risk and cannot be recovered by rollback.
- A local preference is needed to interpret repeated behavior, such as a target layer or naming convention.

## Do Not Interrupt For

- Started, running, completed, or routine failure notifications.
- Read-only status, health, or sync updates.
- Errors the daemon can retry or classify without user input.
- Asking the operator to approve a pipeline that policy already promoted.

## Popup Copy

- State the decision in one sentence.
- Offer concrete choices, not open-ended explanation requests.
- Include a default only when timeout behavior is safe.
- Mention the affected connector/profile when it prevents ambiguity.

## Failure Classes

- `timeout`: no operator answer; record as no decision, not rejection.
- `dismissed`: operator intentionally declined interruption; do not repeat immediately.
- `upload_failed`: transport issue; retry if the decision is still relevant.
- `render_failed`: local UI unavailable; fall back to event stream and avoid loops.
