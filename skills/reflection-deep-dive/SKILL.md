---
name: reflection-deep-dive
description: Use when connector flywheel history is large/noisy and lightweight reflection is insufficient. Builds a deep reflection cache consumed by hooks.
---

# Reflection Deep Dive

## Overview

Use this skill to generate a deeper, cached muscle-memory summary for long-running
or noisy connectors. The cache is consumed by `PreCompact`, `PostCompact`, and
`UserPromptSubmit` hooks when fresh, with automatic fallback to lightweight
reflection when stale.

## When to Use

- Connector intent volume is high (for example, 200+ intents).
- Recent failures or rollbacks are increasing.
- The user asks for a full reflection/review of existing flywheel memory.

## Command

Run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/build_reflection_cache.py"
```

Optional:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/build_reflection_cache.py" --max-items 12
```

## Output

The command writes:

- `~/.emerge/repl/reflection-cache/global.json` (or `EMERGE_STATE_ROOT/reflection-cache/global.json`)

with:

- `generated_at_ms`
- `summary_text`
- `meta`

## Notes

- Hooks prefer this cache while it is fresh (TTL-based).
- If missing/stale, hooks fall back to local lightweight reflection.
- The cache path and TTL policy are implementation details in `SpanTracker`.
