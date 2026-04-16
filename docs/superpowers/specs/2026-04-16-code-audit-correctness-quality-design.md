# Emerge Code Audit — Correctness & Quality
**Date:** 2026-04-16  
**Scope:** Post-Svelte-rewrite audit focused on correctness fixes and Svelte component decomposition

---

## Background

Since the last audit (2026-04-15, Plans 1–4 + deep audit = 605→651 tests), the following major changes landed:
- Cockpit fully rewritten in Svelte (replacing cockpit_shell.html)
- Goal system tests removed
- Runner profile auto-detection
- New hooks: `cwd_changed`, `elicitation`, `elicitation_result`
- New API: `/api/control-plane/runner-events`

This audit addresses correctness gaps and code quality issues introduced by those changes.

---

## Section 1 — Correctness Fixes

### 1a. Commit untracked files

Two files exist on disk and are already used in code but not committed:

| File | Why it matters |
|------|----------------|
| `scripts/admin/cockpit/src/lib/markdown.ts` | Imported by `ConnectorTab.svelte:4` — missing from git means fresh clones break |
| `tests/connectors/zwcad/NOTES.md` | Test fixture referenced by zwcad connector tests |

**Action:** `git add` both files, commit.

### 1b. CLAUDE.md hookSpecificOutput allowed-list gap

Current text (Key Invariants → Hook output schema) lists these events as accepting `hookSpecificOutput`:
> PreToolUse, PostToolUse, PostToolUseFailure, UserPromptSubmit, SessionStart, FileChanged, Setup, SubagentStart, Notification, PermissionRequest

`Elicitation` is absent, but `hooks/elicitation.py` correctly uses `hookSpecificOutput` with the CC-documented format:
```json
{
  "hookSpecificOutput": {
    "hookEventName": "Elicitation",
    "action": "accept | decline | delegate",
    "content": {}
  }
}
```

**Action:** Add `Elicitation` to the allowed list in CLAUDE.md with its specific schema note. Also document that `ElicitationResult` is a pure notification (returns `{}`, cannot influence the result).

### 1c. ElicitationResult — no change needed

`hooks/elicitation_result.py` returns `{}`. This is correct: `ElicitationResult` fires after the user has already responded, it is read-only. Document this explicitly in CLAUDE.md alongside 1b.

---

## Section 2 — StateTab Decomposition (752L → 4 files)

### Problem

`StateTab.svelte` conflates four independent concerns:
- Data fetching + row normalization
- Statistics strip (counts)
- Virtual-scroll list + filter/search bar
- Detail / diagnostics panel (reconcile, snooze, write actions)

Script 355L + template 396L. Impossible to reason about or test in isolation.

### Target structure

```
components/state/
  StateTab.svelte     ~150L   coordinator: data fetch, stats strip, wires children
  StateList.svelte    ~280L   filter bar + virtual scroll list
  StateDiag.svelte    ~220L   detail panel, reconcile/snooze/write actions
lib/
  state-helpers.ts    ~80L    pure functions (no Svelte reactivity)
```

### Interfaces

**`state-helpers.ts` exports:**
```ts
buildRows(payload): StateRow[]
relatedRows(all, selected): StateRow[]
toText(value): string
icon(kind: StateKind): string
label(kind: StateKind): string
statusBadgeCls(status): string
rowStatusCls(status): string
```

**`StateList.svelte` props/events:**
```
props:  rows, filterKind, queryText, selectedKey
events: select(key), filterChange(kind), queryChange(text)
```

**`StateDiag.svelte` props/events:**
```
props:  selected, related, writing, sessionId
events: reconcile(key, outcome), snooze(key), write(key, value), clearSelection
```

**`StateTab.svelte` keeps:**
- `loadStateTabData()` + refresh signal handling
- Statistics strip (4 `cp-stat-card` elements computed from `rows`)
- Coordinates `StateList` ↔ `StateDiag` via `selectedKey`

### Virtual scroll

The existing virtual scroll math stays in `StateList.svelte` as-is — no logic changes, pure relocation.

---

## Section 3 — App.svelte Queue Store Extraction (659L → ~520L)

### Problem

Queue management logic (60L) is inlined in App.svelte alongside SSE lifecycle, routing, data refresh, and template. The `stores/` directory already has policy/session/state/monitors/ui — queue belongs there.

### Target structure

```
stores/queue.ts     new — writable store + actions
App.svelte          script drops from ~398L to ~310L
```

### `stores/queue.ts` design

```ts
export interface QueueItem { id: number; data: QueueDraft }

interface QueueStore {
  subscribe: Readable<QueueState>['subscribe'];
  enqueue(draft: QueueDraft): void;
  dequeue(id: number): void;
  clear(): void;
  submit(api: Api): Promise<SubmitResponse>;  // handles queueSubmitting flag internally
}

interface QueueState {
  items: QueueItem[];
  submitting: boolean;
  idSeq: number;
}
```

`createQueue()` returns a `QueueStore`. App.svelte calls `createQueue()` on init, replaces the 6 inline state vars + 4 functions with `const queue = createQueue()`.

### What stays in App.svelte

- SSE lifecycle (`onMount` setup + cleanup)
- Data refresh (`refreshStatus`, `refreshAssets`, `refreshShellData`)
- Tab routing + URL sync (already delegated to `lib/router.ts`)
- Session dropdown handling
- Template (necessarily complex — renders all tabs)

---

## Scope boundaries

**Not in scope:**
- `control_plane.py` (690L) — each `cmd_*` function is independent; no architectural issue
- `ConnectorTab.svelte` (517L) — complex but coherent; no natural split identified
- `App.svelte` template (260L) — necessarily reflects all tabs; no split warranted

**Tests:**
- Section 1: no test changes
- Section 2: existing `test_cockpit_api.py` / `test_cockpit_sse.py` unaffected (backend-only); Svelte unit tests if present
- Section 3: same — no backend test impact

---

## Implementation order

1. Section 1 (correctness) — commit untracked files + CLAUDE.md patch  
2. Section 2 (StateTab) — extract helpers → StateList → StateDiag → thin StateTab  
3. Section 3 (queue store) — new store → wire App.svelte  

Each section is independently shippable.
