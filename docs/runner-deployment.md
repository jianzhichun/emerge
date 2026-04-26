# Runner Deployment

The runner framework observes, executes, and communicates. It does not decide what to synthesize or promote.

## Install

Generate a one-line installer from the daemon machine:

```bash
python3 scripts/repl_admin.py runner-install-url --target-profile <profile> --pretty
```

The operator runs the printed command on the target machine. The installer writes `~/.emerge/runner-config.json`, installs optional dependencies, and registers the watchdog for startup.

## Lifecycle

1. `runner_watchdog.py` starts `remote_runner.py`.
2. The runner exposes `/health`, `/status`, `/logs`, `/run`, and operator-event endpoints.
3. The runner posts `/runner/online` to the daemon and holds `/runner/sse` for push commands.
4. On crash, the watchdog restarts the runner after `RESTART_DELAY_S`.
5. On deploy, `runner-deploy` writes updated scripts and touches `.watchdog-restart`.

## Execution

The runner accepts only `icc_exec` on `/run`. Pipeline bridge execution stays daemon-owned: the daemon loads connector artifacts locally, builds inline code, and sends one `icc_exec` request to the runner.

Runner HTTP calls bypass system proxies via `scripts.runner_http.no_proxy_urlopen` because runner URLs are direct LAN, Tailscale, or localhost endpoints.

## Outbox Recovery

`runner_emit.py` adds a stable `message_id` and sends events to `/runner/event`. If delivery fails, the payload is appended to `~/.emerge/runner_outbox.jsonl` or `EMERGE_RUNNER_OUTBOX`.

`flush_outbox_once()` renames the outbox to a processing file, retries each row, and writes failed rows back. This avoids losing events across process restarts while keeping duplicate delivery idempotent through message ids.

## Troubleshooting

- `/health` failing: restart the watchdog and check `.runner.log`.
- Events not arriving: run `flush_outbox_once()` on the runner and inspect retained rows.
- GUI popup invisible on Windows: ensure the watchdog starts in an interactive user session, not Session 0.
- Proxy-related 502 or connection errors: verify code paths use `no_proxy_urlopen`.
