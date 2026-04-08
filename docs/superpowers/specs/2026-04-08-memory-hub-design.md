# Memory Hub Design

## Goal

Share connector assets (pipelines, NOTES.md, span policy) across a team via a self-hosted git repo (Gitea/Quasar). A single orphan branch holds all selected verticals. The sync agent handles bidirectional sync automatically: stable promotion triggers push, a background timer drives pull, and AI-assisted conflict resolution surfaces decisions to the user only when necessary.

---

## Architecture

```
emerge_daemon.py          emerge_sync.py (new)        Remote Git (Gitea/Quasar)
─────────────────         ──────────────────────       ─────────────────────────
stable 事件写入 ──────────► sync-queue.jsonl poll       orphan branch: emerge-hub
                           ├─ push: export assets ──►  connectors/
                           ├─ pull: fetch/merge   ◄──    gmail/
                           └─ conflict → CC session       linear/
                                │                         ...
                                ▼
                           ElicitRequest / AskUserQuestion
                                │
                                ▼
                           User decision → resolve
```

**Components:**
- **`emerge_sync.py`** — standalone sync agent process, managed by launchd/systemd
- **`~/.emerge/sync-queue.jsonl`** — daemon writes stable events; sync agent polls and consumes
- **`~/.emerge/hub-config.json`** — remote URL, branch, selected verticals, poll interval
- **`~/.emerge/pending-conflicts.json`** — unresolved conflicts persisted across sessions
- **`icc_hub` MCP tool** — CC-facing command interface for configuration and manual sync
- **orphan branch `emerge-hub`** — isolated from main history, connector assets only

---

## Data Model

### Orphan Branch Layout

```
connectors/
  gmail/
    NOTES.md
    pipelines/
      read/
        fetch_thread.py
        fetch_thread.yaml
      write/
        send_reply.py
        send_reply.yaml
    spans.json
  linear/
    NOTES.md
    pipelines/
      ...
    spans.json
```

Each vertical directory mirrors `~/.emerge/connectors/<name>/` for shareable assets only. **Not synced:** credentials, operator-events, private state.

### hub-config.json

```json
{
  "remote": "git@quasar.internal:team/emerge-hub.git",
  "branch": "emerge-hub",
  "poll_interval_seconds": 300,
  "selected_verticals": ["gmail", "linear"],
  "author": "alice <alice@team.com>"
}
```

### sync-queue.jsonl (event format)

```json
{"event": "stable", "connector": "gmail", "pipeline": "fetch_thread", "ts_ms": 1712345678000}
{"event": "consumed", "connector": "gmail", "pipeline": "fetch_thread", "ts_ms": 1712345679000}
```

### pending-conflicts.json

```json
{
  "conflicts": [
    {
      "connector": "gmail",
      "file": "pipelines/read/fetch_thread.py",
      "ours_ts_ms": 1712300000000,
      "theirs_ts_ms": 1712100000000,
      "status": "pending"
    }
  ]
}
```

---

## Sync Flows

### Push Flow (export → git push)

Triggered by stable event from sync-queue, or manual `icc_hub(action="sync")`.

1. `git fetch origin emerge-hub`
2. Merge remote into local hub worktree — if conflict, enter Conflict Flow
3. **Export**: copy `~/.emerge/connectors/<vertical>/` → hub worktree `connectors/<vertical>/` for each selected vertical
4. `git add -A && git commit -m "hub: sync <vertical> pipelines"`
5. `git push origin emerge-hub`
6. Write consumed marker to sync-queue

### Pull Flow (git fetch → import)

Triggered by poll timer every `poll_interval_seconds`.

1. `git fetch origin emerge-hub`
2. Diff local vs remote — skip if no changes
3. Merge remote into local hub worktree — if conflict, enter Conflict Flow
4. **Import**: copy hub worktree `connectors/<vertical>/` → `~/.emerge/connectors/<vertical>/` for each selected vertical
5. Notify daemon to reload (write `{"event": "reload", "connector": "<vertical>"}` to sync-queue)

### Initial Setup

```
emerge_sync setup
  → prompt: remote URL
  → prompt: branch name (default: emerge-hub)
  → prompt: author name/email
  → list local connectors → multi-select which to include
  → write hub-config.json
  → git clone --orphan emerge-hub OR fetch existing branch
  → run first pull
```

---

## Conflict Resolution Flow

When `git merge` encounters conflicts:

1. `emerge_sync` collects conflict file list and both sides' diffs
2. Sends `ElicitRequest` to CC session: "Found \<N\> conflicts in hub sync — analyzing..."
3. **AI analysis** (in CC session):
   - Compare `success_rate` from pipelines-registry for each conflicting pipeline
   - Compare `last_ts_ms` (recency)
   - Detect semantic divergence (same name, different logic → escalate to user)
   - Generate per-file recommendation with reasoning
4. **AskUserQuestion** per conflict:
   ```
   gmail/fetch_thread has a merge conflict:
     Local:  success_rate=0.92, updated 1 day ago
     Remote: success_rate=0.87, updated 3 days ago
     AI suggests: keep local (higher quality, more recent)

   Your choice:
     A. Keep local
     B. Use remote
     C. Skip this file (resolve later)
     D. Show full diff
   ```
5. User choice → `git checkout --ours` or `--theirs` or write to `pending-conflicts.json`
6. After all conflicts resolved → `git commit`, continue push/import

**Pending conflicts**: written to `~/.emerge/pending-conflicts.json`. SessionStart hook checks for this file and surfaces a reminder via `show_notify` at the start of the next CC session.

---

## CC Command Interface (`icc_hub` MCP tool)

Added to `emerge_daemon.py`. All actions read/write `hub-config.json` and communicate with `emerge_sync.py` via sync-queue events.

| Action | Parameters | Effect |
|--------|-----------|--------|
| `list` | — | Show selected verticals, remote, last sync time |
| `add` | `connector` | Add vertical to selected_verticals |
| `remove` | `connector` | Remove vertical from selected_verticals |
| `sync` | `connector?` | Trigger push+pull for one or all verticals |
| `setup` | — | Interactive initialization wizard |
| `status` | — | Show pending conflicts, queue depth, last sync |

**Example usage:**
```
icc_hub(action="add", connector="slack")
icc_hub(action="list")
icc_hub(action="sync", connector="gmail")
icc_hub(action="status")
```

---

## Import/Export Details

**Export** (local → hub):
- Copy pipeline `.py` + `.yaml` files verbatim
- Copy `NOTES.md` verbatim
- Regenerate `spans.json` from current `span-candidates.json` (filter to stable entries only, strip private fields)

**Import** (hub → local):
- Overwrite pipeline files in `~/.emerge/connectors/<vertical>/pipelines/`
- Overwrite `NOTES.md`
- Merge `spans.json` entries: new entries added, existing entries updated only if remote `last_ts_ms` is newer

**Never imported**: operator-events, credentials, private state, `pipelines-registry.json` (quality metrics are local).

---

## Error Handling

| Scenario | Behavior |
|----------|----------|
| Remote unreachable | Log warning, skip cycle, retry next poll |
| Auth failure | `ElicitRequest` to CC session, pause sync until resolved |
| Corrupt hub-config.json | Log error, disable sync, prompt re-setup |
| Push rejected (non-fast-forward) | Auto-fetch + rebase, retry once; if still fails → conflict flow |
| Partial import failure | Roll back with `git checkout` on hub worktree, log error |

---

## Files Created/Modified

| File | Action |
|------|--------|
| `scripts/emerge_sync.py` | Create — standalone sync agent |
| `scripts/emerge_daemon.py` | Modify — add `icc_hub` tool, write stable events to sync-queue |
| `scripts/hub_config.py` | Create — hub-config.json read/write helpers |
| `tests/test_hub_config.py` | Create — unit tests for config helpers |
| `tests/test_emerge_sync.py` | Create — integration tests for sync flows |
| `README.md` | Modify — add Memory Hub to component table |
| `CLAUDE.md` | Modify — add hub architecture notes |

---

## Out of Scope

- Web UI for conflict resolution (AskUserQuestion in CC session is sufficient)
- Encryption of hub content (handled at git remote / SSH level)
- Automatic merge strategies beyond ours/theirs (manual for initial version)
- Support for multiple remotes per vertical (single repo, single branch)
