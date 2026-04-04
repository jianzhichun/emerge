---
name: remote-runner-dev
description: Use when developing emerge plugin code that targets a remote runner — deploying script changes, managing runner/watchdog lifecycle, debugging execution environment issues (session isolation, tool availability, GUI access), or setting up a new runner on any platform.
---

# Remote Runner Dev

## Overview

Emerge's `remote_runner.py` is a lightweight HTTP execution server that runs on any machine where the **actual tools live** — a Windows VM with CAD software, a Linux HPC node with simulation tools, a GPU workstation, a mobile device emulator host. The dev machine stays clean; all environment-specific execution happens on the runner.

Core principle: **deploy via `icc_exec` HTTP — not SSH exec, not SCP**. The runner is its own deployment channel.

## Architecture

```
Dev machine                       Remote machine (any OS)
────────────────                  ──────────────────────────────────
emerge project dir    deploy ─►   <plugin_root>/scripts/
  scripts/           icc_exec      runner_watchdog.py  (daemon)
  connectors/          HTTP ─►       └─ remote_runner.py (port <N>)
  scripts/repl_admin.py                   └─ executes icc_exec/read/write
                                          └─ has access to local tools,
                                             GUI, COM objects, hardware
```

The runner executes code in its **own process environment** — not in the SSH session that deployed it. This distinction matters when the tool requires:
- A specific user session (Windows COM, GUI automation)
- Local hardware (GPU, USB device, simulator)
- Environment variables set at desktop login (conda env, license server, display)

## Key Source Files

| File | Role |
|---|---|
| `scripts/remote_runner.py` | HTTP server; executes `icc_exec` only (pipeline tools handled by daemon) |
| `scripts/runner_watchdog.py` | Keeps runner alive; restarts on crash or `.watchdog-restart` signal |
| `scripts/repl_admin.py` | CLI: `runner-bootstrap`, `runner-deploy`, `runner-status` |
| `scripts/runner_client.py` | Routes requests to runner by `target_profile` |

## Runner Bootstrap (first time)

```bash
python3 scripts/repl_admin.py runner-bootstrap \
  --ssh-target <user@host> \
  --target-profile <profile-key> \
  --runner-url http://<host>:<port>
  [--windows]          # use PowerShell-compatible commands
```

Bootstrap: deploys plugin files via SSH tar pipe → starts runner → health checks → persists route in `~/.emerge/runner-map.json`.

**Bootstrap skips deploy when runner is already healthy** (idempotent). Use `runner-deploy` for subsequent code updates.

## Development Workflow

```
Edit → Deploy → Verify
```

```bash
# 1. Edit code locally
vim scripts/remote_runner.py
vim connectors/<vertical>/pipelines/read/state.py

# 2. Deploy (runner hot-reloads)
python3 scripts/repl_admin.py runner-deploy --target-profile <profile>

# 3. Verify
python3 scripts/repl_admin.py runner-status --pretty
```

### How runner-deploy works

1. Reads `scripts/*.py` from local project
2. Base64-encodes each file, POSTs to `<runner_url>/run` as `icc_exec` calls
3. Runner writes files to `<plugin_root>/scripts/` in-place
4. Touches `.watchdog-restart` → watchdog restarts runner with new code (~5s)

### Why icc_exec, not SSH/SCP

| Method | Problem |
|---|---|
| SSH exec | Runs in SSH session context, not the runner's execution environment |
| SSH stdin pipe → tar (Windows) | PowerShell `$input` corrupts binary → `Unrecognized archive format` |
| SCP | Hangs on NAT'd / high-latency connections; file locking on Windows |
| `icc_exec` HTTP | Text-safe (base64), runs in correct environment, no SSH dependency |

## Watchdog

`runner_watchdog.py` wraps the runner process:
- **Crash recovery**: if runner exits unexpectedly, restarts after `RESTART_DELAY_S` (default 3s)
- **Signal restart**: polls for `.watchdog-restart` file every `POLL_INTERVAL_S` (default 2s)
- **Log**: writes start/restart events with PID to `.watchdog.log`

Trigger hot-reload manually (without full deploy):

```python
# via icc_exec to the runner
import pathlib
pathlib.Path('<plugin_root>/.watchdog-restart').touch()
```

## Platform-Specific Setup Notes

### Windows — interactive session required for GUI/COM

COM objects (ZWCAD, AutoCAD, Excel, etc.) and GUI automation are **session-scoped**. A process can only access COM objects created in its own Windows session. SSH spawns a new session → COM fails with `RPC server unavailable`.

**Fix**: launch watchdog from the user's **interactive desktop session** (RDP or console):

```vbs
' start_emerge_runner.vbs — double-click from desktop, no window appears
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "<plugin_root>"
sh.Run """<pythonw>"" ""<plugin_root>\scripts\runner_watchdog.py"" --host 0.0.0.0 --port <N>", 0, False
```

Register for auto-start at logon:
```bat
schtasks /Create /F /SC ONLOGON /RU <user> /IT /TN "EmergeRunner" /TR "wscript.exe \"<vbs_path>\""
```

`/IT` = only run when user is interactively logged on (correct session).

### Linux/Mac — standard nohup launch

```bash
cd <plugin_root>
nohup python3 scripts/runner_watchdog.py --host 0.0.0.0 --port <N> \
  > ~/.emerge/watchdog.log 2>&1 &
```

No session isolation issues. `runner-bootstrap` handles this automatically.

## self-contained Import

`remote_runner.py` inserts its own root into `sys.path` — no `PYTHONPATH` needed regardless of working directory:

```python
# scripts/remote_runner.py — top of file
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
```

## Ops Endpoints

```bash
GET  /health        # {"ok": true, "status": "ready", "uptime_s": N}
GET  /status        # pid, python, root, uptime_s
GET  /logs?n=100    # last N lines of .runner.log
POST /run           # {"tool_name": "icc_exec", "arguments": {...}}
POST /operator-event                                      # append one event to local EventBus
GET  /operator-events?machine_id=&since_ms=&limit=        # read events since ts
```

The runner accepts **only `icc_exec`** requests on `/run`. Pipeline operations (`icc_read`, `icc_write`) are handled by the daemon: it loads pipeline `.py` + `.yaml` locally, builds self-contained inline code, and sends it as `icc_exec`. Connector files never need to exist on the runner machine.

Request / response shape for `/run`:
```json
{"tool_name": "icc_exec", "arguments": {"code": "...", "target_profile": "default", "no_replay": false}}
{"ok": true,  "result": {"isError": false, "content": [{"type": "text", "text": "..."}]}}
{"ok": false, "error": "string message"}
```

### EventBus endpoints

`POST /operator-event` — append a single operator event to the local EventBus file.

Request body (JSON):
```json
{
  "ts_ms": 1712345678000,
  "machine_id": "workstation-01",
  "session_role": "operator",
  "event_type": "entity_added",
  "app": "zwcad",
  "payload": {"layer": "标注", "content": "主卧"}
}
```

`machine_id` must be a plain identifier — no path separators or `..` components. Response: `{"ok": true}` or HTTP 400 with `{"ok": false, "error": "..."}`.

---

`GET /operator-events?machine_id=<id>&since_ms=<ts>&limit=<n>` — read events newer than `since_ms` (default 0), up to `limit` (default 200, max 1000).

Response: `{"ok": true, "events": [...]}`.

These two endpoints are consumed by `OperatorMonitor` in the daemon when `EMERGE_OPERATOR_MONITOR=1`.

## Common Mistakes

| Mistake | Reality |
|---|---|
| SSH exec to run tool-dependent code | SSH context ≠ runner environment. Runner was started for a reason. |
| SCP/tar pipe to deploy on Windows | Binary corruption. Use `runner-deploy`. |
| `runner-bootstrap` to push code changes | Bootstrap skips deploy when runner is healthy. Use `runner-deploy`. |
| curl/urllib returns 502 | Local `http_proxy` intercepting. Use `NO_PROXY=<host>` or `ProxyHandler({})`. |
| Windows: runner starts but COM fails | Watchdog was launched via SSH, not from the RDP desktop. Restart from desktop. |
| `ModuleNotFoundError: scripts.pipeline_engine` | Missing `sys.path` self-insert in `remote_runner.py`. |
