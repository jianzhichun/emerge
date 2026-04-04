# Remote Runner Protocol

This document defines the minimum protocol between plugin control-plane and remote execution runner.

## Purpose
- Keep plugin interfaces stable (`icc_read`, `icc_write`, `icc_exec`).
- Move environment-specific execution (GUI/COM, remote shell) into a dedicated runner.
- Preserve flywheel metrics and policy decisions in plugin-side registry files.

## Transport
- HTTP `POST /run`
- Content-Type: `application/json`

## Request Schema
```json
{
  "tool_name": "icc_read | icc_write | icc_exec",
  "arguments": {}
}
```

## Response Schema
```json
{
  "ok": true,
  "result": {
    "isError": false,
    "content": [
      { "type": "text", "text": "..." }
    ]
  }
}
```

Error response:
```json
{
  "ok": false,
  "error": "string message"
}
```

## Plugin Integration
- `scripts/repl_daemon.py` checks runner env in this order:
  - `EMERGE_RUNNER_MAP` (JSON map, per `target_profile` / `runner_id`)
  - `EMERGE_RUNNER_URLS` (comma-separated pool)
  - `EMERGE_RUNNER_URL` (single default URL)
- If any runner config is present, daemon dispatches `icc_*` calls via router.
- If no runner config exists, daemon executes local fallback paths.
- Flywheel bookkeeping remains plugin-side after tool result returns.

Related env vars:
- `EMERGE_RUNNER_URL` (example: `http://127.0.0.1:8787`)
- `EMERGE_RUNNER_MAP` (example: `{"mycader-1.zwcad":"http://10.0.0.11:8787"}`)
- `EMERGE_RUNNER_URLS` (example: `http://10.0.0.11:8787,http://10.0.0.12:8787`)
- `EMERGE_RUNNER_TIMEOUT_S` (default `30`)
- `EMERGE_RUNNER_CONFIG_PATH` (optional; defaults to `~/.emerge/runner-map.json`)

Persisted config file (default `~/.emerge/runner-map.json`):
```json
{
  "default_url": "http://127.0.0.1:8787",
  "map": {
    "mycader-1.zwcad": "http://10.0.0.11:8787"
  },
  "pool": ["http://10.0.0.11:8787", "http://10.0.0.12:8787"]
}
```
`EMERGE_RUNNER_*` env values override persisted config at runtime.

## Remote Runner Responsibilities
- Execute exactly one tool invocation per request.
- Return MCP-compatible result payload for the tool.
- Never mutate plugin registry files directly.
- Keep execution deterministic for testing where possible.

## Operational Notes
- First-time remote setup can use `scp/ssh` to deploy runner files.
- After runner is started, normal operation should use task dispatch (not repeated file copy).
- For GUI/COM workloads, run runner in an interactive user session, not service session.

Recommended bootstrap command (run locally):
- `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" runner-bootstrap --ssh-target "<user@host>" --target-profile "<target_profile>" --runner-url "http://<target>:8787"`
- This command performs remote deploy, remote python check, remote runner start, local health probe, and local runner-map persistence.
- It is idempotent for healthy runners: when runner is already reachable and plugin versions match, it reuses existing runner instead of starting another process.
