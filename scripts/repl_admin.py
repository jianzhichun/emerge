from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.policy_config import (
    PROMOTE_MAX_HUMAN_FIX_RATE,
    PROMOTE_MIN_ATTEMPTS,
    PROMOTE_MIN_SUCCESS_RATE,
    PROMOTE_MIN_VERIFY_RATE,
    ROLLBACK_CONSECUTIVE_FAILURES,
    STABLE_MIN_ATTEMPTS,
    STABLE_MIN_SUCCESS_RATE,
    STABLE_MIN_VERIFY_RATE,
    derive_profile_token,
    derive_session_id,
    default_hook_state_root,
    default_exec_root,
)
from scripts.runner_client import RunnerClient, RunnerRouter


def _local_plugin_version() -> str:
    manifest = ROOT / ".claude-plugin" / "plugin.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return ""
    return str(data.get("version", "") or "").strip()


def _resolve_state_root() -> Path:
    return Path(os.environ.get("EMERGE_STATE_ROOT", str(default_exec_root()))).expanduser().resolve()


def _resolve_session_id() -> str:
    return derive_session_id(os.environ.get("EMERGE_SESSION_ID"), ROOT)


def _session_paths() -> tuple[Path, Path, Path]:
    state_root = _resolve_state_root()
    session_id = _resolve_session_id()
    target_profile = str(os.environ.get("EMERGE_TARGET_PROFILE", "default")).strip() or "default"
    if target_profile != "default":
        profile_key = derive_profile_token(target_profile)
        session_id = f"{session_id}__{profile_key}"
    session_dir = state_root / session_id
    return session_dir, session_dir / "wal.jsonl", session_dir / "checkpoint.json"


def _load_hook_state_summary() -> dict[str, str]:
    state_path = Path(
        os.environ.get("CLAUDE_PLUGIN_DATA", str(default_hook_state_root()))
    ) / "state.json"
    if not state_path.exists():
        return {"goal": "", "goal_source": "unset"}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return {"goal": "", "goal_source": "unset"}
    if not isinstance(data, dict):
        return {"goal": "", "goal_source": "unset"}
    goal = str(data.get("goal", "") or "")
    goal_source = str(data.get("goal_source", "unset") or "unset")
    return {"goal": goal, "goal_source": goal_source}


def cmd_status() -> dict:
    session_dir, wal_path, checkpoint_path = _session_paths()
    wal_entries = 0
    if wal_path.exists():
        with wal_path.open("r", encoding="utf-8") as f:
            wal_entries = sum(1 for line in f if line.strip())
    return {
        "session_id": _resolve_session_id(),
        "state_root": str(_resolve_state_root()),
        "session_dir": str(session_dir),
        "wal_exists": wal_path.exists(),
        "wal_entries": wal_entries,
        "checkpoint_exists": checkpoint_path.exists(),
    }


def cmd_clear() -> dict:
    session_dir, _, _ = _session_paths()
    existed = session_dir.exists()
    if existed:
        shutil.rmtree(session_dir)
    return {
        "session_id": _resolve_session_id(),
        "session_dir": str(session_dir),
        "cleared": True,
        "existed": existed,
    }


def cmd_policy_status() -> dict:
    state_root = _resolve_state_root()
    registry_path = state_root / "pipelines-registry.json"
    pipelines = []
    registry_corrupt = False
    if registry_path.exists():
        try:
            data = json.loads(registry_path.read_text(encoding="utf-8"))
        except Exception:
            data = {"pipelines": {}}
            registry_corrupt = True
        raw = data.get("pipelines", {})
        if isinstance(raw, dict):
            for key, value in raw.items():
                if not isinstance(value, dict):
                    continue
                item = {"key": key, **value}
                pipelines.append(item)
    pipelines.sort(key=lambda x: (str(x.get("status", "")), str(x.get("key", ""))))
    hook_summary = _load_hook_state_summary()
    return {
        "session_id": _resolve_session_id(),
        "state_root": str(_resolve_state_root()),
        "registry_exists": registry_path.exists(),
        "registry_corrupt": registry_corrupt,
        "goal": hook_summary["goal"],
        "goal_source": hook_summary["goal_source"],
        "pipeline_count": len(pipelines),
        "thresholds": {
            "promote_min_attempts": PROMOTE_MIN_ATTEMPTS,
            "promote_min_success_rate": PROMOTE_MIN_SUCCESS_RATE,
            "promote_min_verify_rate": PROMOTE_MIN_VERIFY_RATE,
            "promote_max_human_fix_rate": PROMOTE_MAX_HUMAN_FIX_RATE,
            "stable_min_attempts": STABLE_MIN_ATTEMPTS,
            "stable_min_success_rate": STABLE_MIN_SUCCESS_RATE,
            "stable_min_verify_rate": STABLE_MIN_VERIFY_RATE,
            "rollback_consecutive_failures": ROLLBACK_CONSECUTIVE_FAILURES,
        },
        "pipelines": pipelines,
    }


def _normalize_pipeline_key(key: str) -> str:
    """Accept 'mock.read.layers' or 'pipeline::mock.read.layers' — always return full key."""
    key = key.strip()
    if not key.startswith("pipeline::"):
        key = f"pipeline::{key}"
    return key


def _load_registry(state_root: Path) -> tuple[Path, dict]:
    registry_path = state_root / "pipelines-registry.json"
    if registry_path.exists():
        data = json.loads(registry_path.read_text(encoding="utf-8"))
    else:
        data = {"pipelines": {}}
    return registry_path, data


def _save_registry(registry_path: Path, data: dict) -> None:
    registry_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def cmd_pipeline_delete(*, key: str) -> dict:
    """Remove a pipeline entry from the registry."""
    full_key = _normalize_pipeline_key(key)
    state_root = _resolve_state_root()
    registry_path, data = _load_registry(state_root)
    pipelines = data.get("pipelines", {})
    if full_key not in pipelines:
        return {"ok": False, "error": f"pipeline not found: {full_key}", "key": full_key}
    del pipelines[full_key]
    data["pipelines"] = pipelines
    _save_registry(registry_path, data)
    return {"ok": True, "deleted": full_key, "remaining": len(pipelines)}


def cmd_pipeline_set(*, key: str, fields: dict) -> dict:
    """Reconcile / patch specific fields on a pipeline registry entry.

    Allowed patchable fields: status, rollout_pct, consecutive_failures,
    policy_enforced_count, stop_triggered_count, rollback_executed_count,
    last_policy_action, success_rate, verify_rate, human_fix_rate.
    """
    PATCHABLE = {
        "status", "rollout_pct", "consecutive_failures",
        "policy_enforced_count", "stop_triggered_count", "rollback_executed_count",
        "last_policy_action", "success_rate", "verify_rate", "human_fix_rate",
    }
    unknown = set(fields) - PATCHABLE
    if unknown:
        return {"ok": False, "error": f"unknown fields: {sorted(unknown)}", "allowed": sorted(PATCHABLE)}

    full_key = _normalize_pipeline_key(key)
    state_root = _resolve_state_root()
    registry_path, data = _load_registry(state_root)
    pipelines = data.get("pipelines", {})

    if full_key not in pipelines:
        return {"ok": False, "error": f"pipeline not found: {full_key}", "key": full_key}

    before = dict(pipelines[full_key])
    pipelines[full_key].update(fields)
    data["pipelines"] = pipelines
    _save_registry(registry_path, data)
    return {
        "ok": True,
        "key": full_key,
        "patched": fields,
        "before": {k: before.get(k) for k in fields},
        "after": {k: pipelines[full_key].get(k) for k in fields},
    }


def cmd_runner_status() -> dict:
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


def _load_runner_config() -> dict:
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
    path = RunnerRouter.persisted_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix="runner-map-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=True, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        tmp_path = ""
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def cmd_runner_config_status() -> dict:
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

    # Files to deploy: scripts/*.py that are needed on the runner.
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


def render_policy_status_pretty(data: dict) -> str:
    lines: list[str] = []
    lines.append(f"Session: {data.get('session_id', '')}")
    lines.append(f"State root: {data.get('state_root', '')}")
    lines.append(f"Goal: {data.get('goal', '')}")
    lines.append(f"Goal source: {data.get('goal_source', 'unset')}")
    lines.append("")
    lines.append("Thresholds:")
    thresholds = data.get("thresholds", {})
    for key in sorted(thresholds.keys()):
        lines.append(f"- {key}: {thresholds[key]}")
    lines.append("")
    lines.append("Pipelines:")
    pipelines = data.get("pipelines", [])
    if not pipelines:
        lines.append("- (none)")
    else:
        for item in pipelines:
            lines.append(f"- key: {item.get('key', '')}")
            lines.append(f"  status: {item.get('status', '')}")
            lines.append(f"  rollout_pct: {item.get('rollout_pct', 0)}")
            lines.append(f"  success_rate: {item.get('success_rate', 0)}")
            lines.append(f"  verify_rate: {item.get('verify_rate', 0)}")
            lines.append(f"  human_fix_rate: {item.get('human_fix_rate', 0)}")
            lines.append(f"  consecutive_failures: {item.get('consecutive_failures', 0)}")
            lines.append(f"  policy_enforced_count: {item.get('policy_enforced_count', 0)}")
            lines.append(f"  stop_triggered_count: {item.get('stop_triggered_count', 0)}")
            lines.append(f"  rollback_executed_count: {item.get('rollback_executed_count', 0)}")
            lines.append(f"  last_policy_action: {item.get('last_policy_action', 'none')}")
            lines.append(f"  transition_reason: {item.get('last_transition_reason', '')}")
    return "\n".join(lines) + "\n"


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Local REPL state admin utility")
    parser.add_argument(
        "command",
        choices=[
            "status",
            "clear",
            "policy-status",
            "runner-status",
            "runner-config-status",
            "runner-config-set",
            "runner-config-unset",
            "runner-bootstrap",
        "runner-deploy",
        "pipeline-delete",
        "pipeline-set",
        ],
    )
    parser.add_argument("--pretty", action="store_true", help="Render human-readable output")
    parser.add_argument("--runner-key", default="", help="Runner key (usually target_profile)")
    parser.add_argument("--runner-url", default="", help="Runner URL")
    parser.add_argument("--as-default", action="store_true", help="Set default runner URL")
    parser.add_argument("--clear-default", action="store_true", help="Clear default runner URL")
    parser.add_argument("--ssh-target", default="", help="SSH target for bootstrap (user@host)")
    parser.add_argument("--target-profile", default="", help="Target profile key")
    parser.add_argument("--remote-plugin-root", default="~/.emerge/plugin", help="Remote plugin root")
    parser.add_argument("--runner-host", default="0.0.0.0", help="Remote runner bind host")
    parser.add_argument("--runner-port", type=int, default=8787, help="Remote runner bind port")
    parser.add_argument("--python-bin", default="python3", help="Remote Python executable")
    parser.add_argument(
        "--skip-deploy",
        action="store_true",
        help="Skip remote deploy and reuse existing remote plugin root",
    )
    parser.add_argument(
        "--windows",
        action="store_true",
        help="Use Windows-compatible (PowerShell) commands for bootstrap (SSH target is Windows)",
    )
    parser.add_argument("--pipeline-key", default="", help="Pipeline key for pipeline-delete/pipeline-set (e.g. mock.read.layers)")
    parser.add_argument("--set", dest="set_fields", action="append", metavar="FIELD=VALUE",
                        help="Field to patch for pipeline-set (repeatable, e.g. --set status=explore --set rollout_pct=0)")
    args = parser.parse_args()

    if args.command == "status":
        out = cmd_status()
    elif args.command == "policy-status":
        out = cmd_policy_status()
    elif args.command == "runner-status":
        out = cmd_runner_status()
    elif args.command == "runner-config-status":
        out = cmd_runner_config_status()
    elif args.command == "runner-config-set":
        out = cmd_runner_config_set(
            runner_key=str(args.runner_key),
            runner_url=str(args.runner_url),
            as_default=bool(args.as_default),
        )
    elif args.command == "runner-config-unset":
        out = cmd_runner_config_unset(
            runner_key=str(args.runner_key),
            clear_default=bool(args.clear_default),
        )
    elif args.command == "runner-bootstrap":
        out = cmd_runner_bootstrap(
            ssh_target=str(args.ssh_target),
            target_profile=str(args.target_profile),
            remote_plugin_root=str(args.remote_plugin_root),
            runner_host=str(args.runner_host),
            runner_port=int(args.runner_port),
            runner_url=str(args.runner_url),
            python_bin=str(args.python_bin),
            deploy=not bool(args.skip_deploy),
            windows=bool(args.windows),
        )
    elif args.command == "runner-deploy":
        out = cmd_runner_deploy(
            runner_url=str(args.runner_url),
            target_profile=str(args.target_profile) or "default",
        )
    elif args.command == "pipeline-delete":
        out = cmd_pipeline_delete(key=str(args.pipeline_key))
    elif args.command == "pipeline-set":
        fields: dict = {}
        for pair in (args.set_fields or []):
            k, _, v = pair.partition("=")
            k = k.strip()
            # coerce numeric-looking values
            try:
                fields[k] = int(v)
            except ValueError:
                try:
                    fields[k] = float(v)
                except ValueError:
                    fields[k] = v
        out = cmd_pipeline_set(key=str(args.pipeline_key), fields=fields)
    else:
        out = cmd_clear()

    if args.pretty and args.command == "policy-status":
        print(render_policy_status_pretty(out), end="")
    elif args.pretty and args.command == "runner-status":
        print(render_runner_status_pretty(out), end="")
    else:
        print(json.dumps(out))


if __name__ == "__main__":
    main()
