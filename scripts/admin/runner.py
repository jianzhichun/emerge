"""Runner administration — deploy, bootstrap, config, status.

Functions here handle all SSH-based remote runner operations.  They are
called by repl_admin.py (CLI) and by CockpitHTTPServer (HTTP API).

Extracted from repl_admin.py to keep each module focused on a single concern.
"""
from __future__ import annotations

import json
import shlex
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Re-use the canonical implementation from api to avoid duplication.
# Both modules resolve ROOT to the same project root.
from scripts.admin.api import _local_plugin_version  # noqa: E402


# ---------------------------------------------------------------------------
# Runner status
# ---------------------------------------------------------------------------

def cmd_runner_status() -> dict:
    from scripts.runner_client import RunnerRouter
    router = RunnerRouter.from_env()
    if router is None:
        return {
            "runner_configured": False,
            "runner_reachable": False,
            "endpoints": [],
            "error": "runner config is not set (persisted config or EMERGE_RUNNER_*)",
        }
    summary = router.health_summary()
    return {
        "runner_configured": bool(summary.get("configured", False)),
        "runner_reachable": bool(summary.get("any_reachable", False)),
        "endpoint_count": int(summary.get("endpoint_count", 0)),
        "endpoints": summary.get("endpoints", []),
        "error": "",
    }


def render_runner_status_pretty(data: dict) -> str:
    lines: list[str] = []
    lines.append(f"Runner configured: {data.get('runner_configured', False)}")
    lines.append(f"Runner reachable: {data.get('runner_reachable', False)}")
    lines.append(f"Endpoint count: {data.get('endpoint_count', 0)}")
    error = str(data.get("error", "") or "")
    if error:
        lines.append(f"Error: {error}")
    endpoints = data.get("endpoints", [])
    if isinstance(endpoints, list) and endpoints:
        lines.append("Endpoints:")
        for item in endpoints:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"- {item.get('name', '')}: {item.get('url', '')} "
                f"(reachable={item.get('reachable', False)})"
            )
            item_error = str(item.get("error", "") or "")
            if item_error:
                lines.append(f"  error: {item_error}")
            health = item.get("health", {})
            if isinstance(health, dict) and health:
                for key in sorted(health.keys()):
                    lines.append(f"  {key}: {health[key]}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Runner config (persisted runner-map.json)
# ---------------------------------------------------------------------------

def _load_runner_config() -> dict:
    from scripts.runner_client import RunnerRouter
    path = RunnerRouter.persisted_config_path()
    if not path.exists():
        return {"default_url": "", "map": {}, "pool": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("runner config must be a JSON object")
    raw_map = data.get("map", {})
    if not isinstance(raw_map, dict):
        raw_map = {}
    raw_pool = data.get("pool", [])
    if not isinstance(raw_pool, list):
        raw_pool = []
    return {
        "default_url": str(data.get("default_url", "") or ""),
        "map": {str(k): str(v) for k, v in raw_map.items()},
        "pool": [str(x) for x in raw_pool],
    }


def _save_runner_config(data: dict) -> None:
    from scripts.runner_client import RunnerRouter
    from scripts.policy_config import atomic_write_json
    path = RunnerRouter.persisted_config_path()
    atomic_write_json(
        path,
        data,
        prefix="runner-map-",
        suffix=".json",
        ensure_ascii=True,
        indent=2,
    )


def cmd_runner_config_status() -> dict:
    from scripts.runner_client import RunnerRouter
    path = RunnerRouter.persisted_config_path()
    data = _load_runner_config()
    return {
        "config_path": str(path),
        "exists": path.exists(),
        "default_url": data.get("default_url", ""),
        "map": data.get("map", {}),
        "pool": data.get("pool", []),
    }


def cmd_runner_config_set(*, runner_key: str, runner_url: str, as_default: bool = False) -> dict:
    key = runner_key.strip()
    url = runner_url.strip()
    if not url:
        raise ValueError("--runner-url is required")
    data = _load_runner_config()
    if as_default:
        data["default_url"] = url
    else:
        if not key:
            raise ValueError("--runner-key is required unless --as-default is set")
        data.setdefault("map", {})
        data["map"][key] = url
    _save_runner_config(data)
    out = cmd_runner_config_status()
    out["updated"] = True
    return out


def cmd_runner_config_unset(*, runner_key: str, clear_default: bool = False) -> dict:
    key = runner_key.strip()
    data = _load_runner_config()
    changed = False
    if clear_default and data.get("default_url", ""):
        data["default_url"] = ""
        changed = True
    if key:
        mapped = data.get("map", {})
        if isinstance(mapped, dict) and key in mapped:
            del mapped[key]
            changed = True
    _save_runner_config(data)
    out = cmd_runner_config_status()
    out["updated"] = changed
    return out


# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------

def _run_checked(command: list[str], *, timeout_s: int = 90) -> str:
    proc = subprocess.run(
        command,
        capture_output=True,
        timeout=timeout_s,
    )
    proc.stdout = (proc.stdout or b"").decode("utf-8", errors="replace")
    proc.stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        detail = stderr or stdout or f"exit={proc.returncode}"
        raise RuntimeError(f"command failed: {' '.join(command)} :: {detail}")
    return (proc.stdout or "").strip()


def _remote_root_expr(raw_root: str) -> str:
    text = raw_root.strip()
    if text == "~":
        return "$HOME"
    if text.startswith("~/"):
        suffix = text[2:]
        return "$HOME/" + shlex.quote(suffix)
    return shlex.quote(text)


def _remote_root_expr_win(raw_root: str) -> str:
    """Return a PowerShell-safe path expression for Windows SSH targets."""
    text = raw_root.strip()
    if text == "~":
        return "$env:USERPROFILE\\.emerge\\plugin"
    if text.startswith("~/"):
        suffix = text[2:].replace("/", "\\")
        return f"$env:USERPROFILE\\{suffix}"
    # Absolute path — return as-is (backslash-safe for PowerShell)
    return text.replace("/", "\\")


def _read_remote_plugin_version(*, ssh_target: str, remote_root_expr: str) -> str:
    read_cmd = f"cd {remote_root_expr} && cat .claude-plugin/plugin.json"
    raw = _run_checked(["ssh", ssh_target, read_cmd], timeout_s=20)
    data = json.loads(raw)
    if not isinstance(data, dict):
        return ""
    return str(data.get("version", "") or "").strip()


def _probe_runner_health(*, runner_url: str, attempts: int = 5, sleep_s: float = 1.0) -> tuple[dict, str]:
    from scripts.runner_client import RunnerClient
    health_error = ""
    health: dict = {}
    client = RunnerClient(base_url=runner_url.rstrip("/"), timeout_s=8.0)
    for _ in range(max(1, attempts)):
        try:
            health = client.health()
            return health, ""
        except Exception as exc:
            health_error = str(exc)
            time.sleep(max(0.0, sleep_s))
    return {}, health_error


# ---------------------------------------------------------------------------
# Runner deploy
# ---------------------------------------------------------------------------

def cmd_runner_deploy(
    *,
    runner_url: str = "",
    target_profile: str = "default",
    files: list[str] | None = None,
    windows: bool = False,
) -> dict:
    """Push local scripts/ to remote runner via icc_exec (HTTP) and signal hot-reload.

    Only deploys runner-side scripts (excludes dev-machine-only tools like
    repl_admin.py and emerge_daemon.py).  The remote path is derived from the
    runner's own /status endpoint so no path needs to be hardcoded here.
    """
    import base64
    import urllib.error
    import urllib.request

    # Resolve runner URL from config if not provided
    if not runner_url:
        cfg = _load_runner_config()
        runner_url = cfg.get("map", {}).get(target_profile, "") or cfg.get("default_url", "")
    if not runner_url:
        raise ValueError("runner_url required (or configure via runner-config-set)")

    def _http_get(path: str) -> dict:
        no_proxy = urllib.request.ProxyHandler({})
        opener = urllib.request.build_opener(no_proxy)
        with opener.open(runner_url.rstrip("/") + path, timeout=10) as resp:
            return json.loads(resp.read())

    def _http_exec(code: str) -> dict:
        payload = json.dumps({"tool_name": "icc_exec", "arguments": {"code": code}}).encode()
        req = urllib.request.Request(
            runner_url.rstrip("/") + "/run",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        no_proxy = urllib.request.ProxyHandler({})
        opener = urllib.request.build_opener(no_proxy)
        with opener.open(req, timeout=30) as resp:
            return json.loads(resp.read())

    # Fetch runner's actual plugin root from /status (avoids any hardcoded path)
    status = _http_get("/status")
    remote_root = status.get("root", "").strip()
    if not remote_root:
        raise RuntimeError(f"Could not determine remote plugin root from /status: {status}")

    actions: list[str] = []

    # Files to deploy: scripts/*.py + .claude-plugin/plugin.json (for version tracking).
    # Exclude dev-machine-only tools that serve no purpose on the remote runner.
    _DEV_ONLY = {"repl_admin.py", "emerge_daemon.py"}
    scripts_dir = ROOT / "scripts"
    if files:
        deploy_files = [ROOT / f for f in files]
    else:
        deploy_files = [
            p for p in scripts_dir.rglob("*.py")
            if "__pycache__" not in str(p) and p.name not in _DEV_ONLY
        ]
        plugin_json = ROOT / ".claude-plugin" / "plugin.json"
        if plugin_json.exists():
            deploy_files.append(plugin_json)

    for local_path in deploy_files:
        rel = local_path.relative_to(ROOT)
        # Use forward slashes; pathlib on the runner handles both
        rel_posix = rel.as_posix()
        content_b64 = base64.b64encode(local_path.read_bytes()).decode()
        code = (
            f"import base64, pathlib\n"
            f"data = base64.b64decode({repr(content_b64)})\n"
            f"dst = pathlib.Path({repr(remote_root)}) / {repr(rel_posix)}\n"
            f"dst.parent.mkdir(parents=True, exist_ok=True)\n"
            f"dst.write_bytes(data)\n"
            f"print(f'wrote {{len(data)}}b -> {{dst.name}}')"
        )
        resp = _http_exec(code)
        if not resp.get("ok"):
            raise RuntimeError(f"deploy failed for {rel}: {resp}")
    actions.append(f"files_synced ({len(deploy_files)} files)")

    # Touch .watchdog-restart so the watchdog hot-reloads the runner
    signal_code = (
        f"import pathlib\n"
        f"sig = pathlib.Path({repr(remote_root)}) / '.watchdog-restart'\n"
        f"sig.touch()\n"
        f"print('restart signal written')"
    )
    _http_exec(signal_code)
    actions.append("restart_signal_sent")

    return {"ok": True, "runner_url": runner_url, "file_count": len(deploy_files), "actions": actions}


# ---------------------------------------------------------------------------
# Runner bootstrap
# ---------------------------------------------------------------------------

def cmd_runner_bootstrap(
    *,
    ssh_target: str,
    target_profile: str,
    remote_plugin_root: str,
    runner_host: str,
    runner_port: int,
    runner_url: str,
    python_bin: str,
    deploy: bool,
    windows: bool = False,
    team_lead_url: str = "",
) -> dict:
    ssh_target = ssh_target.strip()
    target_profile = target_profile.strip()
    remote_plugin_root = remote_plugin_root.strip()
    python_bin = python_bin.strip() or ("python" if windows else "python3")
    if not ssh_target:
        raise ValueError("--ssh-target is required")
    if not target_profile:
        raise ValueError("--target-profile is required")
    if not remote_plugin_root:
        raise ValueError("--remote-plugin-root is required")

    if not runner_url.strip():
        host_part = ssh_target.split("@")[-1]
        host_part = host_part.split(":")[0]
        runner_url = f"http://{host_part}:{int(runner_port)}"
    if int(runner_port) <= 0 or int(runner_port) > 65535:
        raise ValueError("--runner-port must be in 1..65535")

    actions: list[str] = []
    local_version = _local_plugin_version()
    remote_version = ""
    runner_reused = False
    if windows:
        remote_root = _remote_root_expr_win(remote_plugin_root)
        mkdir_cmd = f'powershell -Command "New-Item -Force -ItemType Directory -Path \\"{remote_root}\\" | Out-Null"'
    else:
        remote_root = _remote_root_expr(remote_plugin_root)
        mkdir_cmd = f"mkdir -p {remote_root}"
    _run_checked(["ssh", ssh_target, mkdir_cmd])
    actions.append("remote_root_ready")

    try:
        remote_version = _read_remote_plugin_version(
            ssh_target=ssh_target,
            remote_root_expr=remote_root,
        )
        actions.append("remote_version_detected")
    except Exception:
        remote_version = ""

    pre_health, pre_health_error = _probe_runner_health(
        runner_url=runner_url,
        attempts=2,
        sleep_s=0.5,
    )
    if not pre_health_error and pre_health:
        if remote_version and local_version and remote_version != local_version:
            raise RuntimeError(
                "runner already reachable but remote plugin version mismatches local version; "
                "stop remote runner first or change runner-url/port before bootstrap"
            )
        cfg = cmd_runner_config_set(
            runner_key=target_profile,
            runner_url=runner_url,
            as_default=False,
        )
        actions.append("runner_already_healthy")
        actions.append("runner_route_persisted")
        runner_reused = True
        return {
            "ok": True,
            "ssh_target": ssh_target,
            "target_profile": target_profile,
            "remote_plugin_root": remote_plugin_root,
            "runner_url": runner_url,
            "runner_pid": "",
            "actions": actions,
            "health": pre_health,
            "config": cfg,
            "reused_existing_runner": runner_reused,
            "local_plugin_version": local_version,
            "remote_plugin_version": remote_version,
            "version_match": (not remote_version) or (remote_version == local_version),
        }

    if deploy:
        tar_args = [
            "tar",
            "-czf",
            "-",
            "--exclude=.git",
            "--exclude=.worktrees",
            "--exclude=.plugin-data",
            "--exclude=.pytest_cache",
            "--exclude=__pycache__",
            "--exclude=.venv",
            ".",
        ]
        if windows:
            # Windows tar (bsdtar) supports piped stdin; mkdir via PowerShell first
            untar_cmd = (
                f'powershell -Command "New-Item -Force -ItemType Directory -Path \\"{remote_root}\\" | Out-Null; '
                f'$input | tar -xzf - -C \\"{remote_root}\\""'
            )
        else:
            untar_cmd = f"mkdir -p {remote_root} && tar -xzf - -C {remote_root}"
        proc_tar = subprocess.Popen(
            tar_args,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
        )
        proc_ssh = subprocess.Popen(
            ["ssh", ssh_target, untar_cmd],
            stdin=proc_tar.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
        )
        assert proc_tar.stdout is not None
        proc_tar.stdout.close()
        ssh_out_b, ssh_err_b = proc_ssh.communicate()
        ssh_out = ssh_out_b.decode("utf-8", errors="replace") if ssh_out_b else ""
        ssh_err = ssh_err_b.decode("utf-8", errors="replace") if ssh_err_b else ""
        tar_err = proc_tar.stderr.read().decode("utf-8", errors="replace") if proc_tar.stderr else ""
        tar_rc = proc_tar.wait()
        if tar_rc != 0:
            raise RuntimeError(f"local tar failed: {tar_err.strip() or f'exit={tar_rc}'}")
        if proc_ssh.returncode != 0:
            detail = (ssh_err or ssh_out or "").strip() or f"exit={proc_ssh.returncode}"
            raise RuntimeError(f"remote deploy failed: {detail}")
        actions.append("remote_assets_deployed")
        remote_version = local_version

    if windows:
        py_check = f'powershell -Command "cd \\"{remote_root}\\"; {python_bin} -V"'
        _run_checked(["ssh", ssh_target, py_check])
        actions.append("remote_python_verified")
        log_path = f"{remote_root}\\remote-runner.log"
        start_cmd = (
            f'powershell -Command "'
            f'cd \\"{remote_root}\\"; '
            f'Start-Process -FilePath {python_bin} '
            f'-ArgumentList \\"scripts/remote_runner.py --host {runner_host} --port {int(runner_port)}\\" '
            f'-RedirectStandardOutput \\"{log_path}\\" '
            f'-WindowStyle Hidden; '
            f'Write-Host started"'
        )
    else:
        py_check = f"cd {shlex.quote(remote_root)} && {shlex.quote(python_bin)} -V"
        _run_checked(["ssh", ssh_target, py_check])
        actions.append("remote_python_verified")
        start_cmd = (
            f"mkdir -p $HOME/.emerge && cd {shlex.quote(remote_root)} && "
            f"nohup {shlex.quote(python_bin)} scripts/remote_runner.py "
            f"--host {shlex.quote(runner_host)} --port {int(runner_port)} "
            "> ~/.emerge/remote-runner.log 2>&1 < /dev/null & echo $!"
        )
    pid_text = _run_checked(["ssh", ssh_target, start_cmd])
    pid = pid_text.splitlines()[-1].strip() if pid_text else ""
    actions.append("remote_runner_started")

    health, health_error = _probe_runner_health(
        runner_url=runner_url,
        attempts=5,
        sleep_s=1.0,
    )
    if health_error:
        raise RuntimeError(f"runner health check failed ({runner_url}): {health_error}")
    actions.append("runner_health_ok")

    cfg = cmd_runner_config_set(
        runner_key=target_profile,
        runner_url=runner_url,
        as_default=False,
    )
    actions.append("runner_route_persisted")

    if team_lead_url:
        import json as _json
        runner_cfg = _json.dumps({
            "team_lead_url": team_lead_url.rstrip("/"),
            "runner_profile": target_profile,
        }, indent=2)
        write_cfg_cmd = f"mkdir -p ~/.emerge && printf '%s' {shlex.quote(runner_cfg)} > ~/.emerge/runner-config.json"
        _run_checked(["ssh", ssh_target, write_cfg_cmd])
        actions.append("runner_config_written")

    return {
        "ok": True,
        "ssh_target": ssh_target,
        "target_profile": target_profile,
        "remote_plugin_root": remote_plugin_root,
        "runner_url": runner_url,
        "runner_pid": pid,
        "actions": actions,
        "health": health,
        "config": cfg,
        "reused_existing_runner": runner_reused,
        "local_plugin_version": local_version,
        "remote_plugin_version": remote_version,
        "version_match": (not remote_version) or (remote_version == local_version),
    }
