---
name: cockpit-rendering
description: Use when designing or revising Emerge cockpit presentation, summaries, filters, action labels, or empty/error states without changing the HTTP API contract.
---

# Cockpit Rendering

## Purpose

Keep cockpit code focused on serving state and validating actions. Put presentation judgment here: what sections matter, how to label them, and how to keep the operator focused on flywheel progress.

## Rendering Principles

- Lead with actionable flywheel state: stable pipelines, canary risks, blocked synthesis, runner health.
- Treat cockpit copy as operator guidance, not implementation truth. Source data still comes from `/api/*` and resources.
- Do not invent new API fields. If a view needs data, first check existing endpoints and only propose API expansion explicitly.
- Prefer short status labels over narrative prose in tables and cards.
- Expose confidence and verification state when a recommendation could trigger operator action.

## Empty States

- No stable intents: explain that repeated successful spans are needed before zero-inference execution appears.
- No runners: point to `admin-runner-operations` or `runner-install-url` instead of embedding long setup flow in code.
- Synthesis blocked: show the reason and the latest sample/job ID so the operator can inspect the right artifact.

## Action Labels

Use verb-first labels: `Freeze policy`, `Resume policy`, `Open runner`, `Resolve conflict`, `Retry synthesis`. Avoid labels that imply human approval gates for auto-promoted paths.
