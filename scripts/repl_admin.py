from __future__ import annotations

import argparse
import http.server
import json
import os
import re
import shlex
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import webbrowser
import zipfile
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
    pin_plugin_data_path_if_present,
)
from scripts.runner_client import RunnerClient, RunnerRouter
from scripts.goal_control_plane import (
    EVENT_HUMAN_EDIT,
    GoalControlPlane,
)


def _local_plugin_version() -> str:
    manifest = ROOT / ".claude-plugin" / "plugin.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return ""
    return str(data.get("version", "") or "").strip()


def _resolve_state_root() -> Path:
    return Path(os.environ.get("EMERGE_STATE_ROOT", str(default_exec_root()))).expanduser().resolve()


def _resolve_repl_root() -> Path:
    """Return the state root used by cockpit submit/listening handshake files.

    Priority keeps compatibility with older setups:
    1) EMERGE_REPL_ROOT (cockpit-specific)
    2) EMERGE_STATE_ROOT (legacy single-root setups)
    3) default_exec_root() (~/.emerge/repl)
    """
    repl_root = os.environ.get("EMERGE_REPL_ROOT", "").strip()
    if repl_root:
        return Path(repl_root).expanduser().resolve()
    state_root = os.environ.get("EMERGE_STATE_ROOT", "").strip()
    if state_root:
        return Path(state_root).expanduser().resolve()
    return default_exec_root().expanduser().resolve()


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
    pin_plugin_data_path_if_present()
    root = Path(default_hook_state_root())
    cp = GoalControlPlane(root)
    legacy_state = root / "state.json"
    if legacy_state.exists():
        try:
            raw = json.loads(legacy_state.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cp.migrate_legacy_goal(
                    legacy_goal=str(raw.get("goal", "")),
                    legacy_source=str(raw.get("goal_source", "legacy")),
                )
        except Exception:
            pass
    snap = cp.read_snapshot()
    return {
        "goal": str(snap.get("text", "") or ""),
        "goal_source": str(snap.get("source", "unset") or "unset"),
        "goal_version": str(snap.get("version", 0)),
        "goal_updated_at_ms": str(snap.get("updated_at_ms", 0)),
    }


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
    """Key is the plain intent signature (e.g. 'mock.read.layers'). Strip legacy prefixes."""
    key = key.strip()
    for prefix in ("pipeline::", "flywheel::", "default::"):
        if key.startswith(prefix):
            key = key[len(prefix):]
            break
    return key


def _load_registry(state_root: Path) -> tuple[Path, dict]:
    registry_path = state_root / "pipelines-registry.json"
    if registry_path.exists():
        data = json.loads(registry_path.read_text(encoding="utf-8"))
    else:
        data = {"pipelines": {}}
    return registry_path, data


def _atomic_write_json(
    path: Path,
    data: dict,
    *,
    prefix: str,
    suffix: str = "",
    ensure_ascii: bool,
    indent: int | None,
) -> None:
    """Atomically write JSON to path (temp file + fsync + replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=ensure_ascii, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        tmp_path = ""
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _save_registry(registry_path: Path, data: dict) -> None:
    """Atomic write: temp file + os.replace to prevent half-written state on crash."""
    _atomic_write_json(
        registry_path,
        data,
        prefix=".registry-",
        ensure_ascii=False,
        indent=2,
    )


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


def _resolve_connector_root() -> Path:
    """Return connector root: EMERGE_CONNECTOR_ROOT env var if set, else ~/.emerge/connectors."""
    from scripts.pipeline_engine import _USER_CONNECTOR_ROOT
    env_root = os.environ.get("EMERGE_CONNECTOR_ROOT", "").strip()
    if env_root:
        return Path(env_root).expanduser().resolve()
    return _USER_CONNECTOR_ROOT


def cmd_connector_export(
    *,
    connector: str,
    out: str,
    connector_root: Path | None = None,
    state_root: Path | None = None,
) -> dict:
    """Pack a connector directory and its registry entries into a zip file."""
    c_root = connector_root if connector_root is not None else _resolve_connector_root()
    connector_dir = c_root / connector
    if not connector_dir.exists():
        return {"ok": False, "error": f"connector not found: {connector_dir}"}

    s_root = state_root if state_root is not None else _resolve_state_root()
    _, registry_data = _load_registry(s_root)

    prefix = f"{connector}."
    filtered = {
        k: v
        for k, v in registry_data.get("pipelines", {}).items()
        if k.startswith(prefix)
    }

    out_path = Path(out)
    manifest = {
        "name": connector,
        "emerge_version": _local_plugin_version(),
        "exported_at_ms": int(time.time() * 1000),
    }

    files = sorted(
        f for f in connector_dir.rglob("*")
        if f.is_file() and "__pycache__" not in f.parts
    )

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
        zf.writestr(
            "pipelines-registry.json",
            json.dumps({"pipelines": filtered}, indent=2, ensure_ascii=False),
        )
        for f in files:
            arcname = f"connectors/{connector}/{f.relative_to(connector_dir)}"
            zf.write(f, arcname)

    return {
        "ok": True,
        "connector": connector,
        "out": str(out_path),
        "pipeline_count": len(filtered),
        "file_count": len(files),
    }


def cmd_connector_import(
    *,
    pkg: str,
    overwrite: bool = False,
    connector_root: Path | None = None,
    state_root: Path | None = None,
) -> dict:
    """Unpack a connector asset package and merge its registry entries."""
    pkg_path = Path(pkg)
    if not pkg_path.exists():
        return {"ok": False, "error": f"package not found: {pkg_path}"}

    with zipfile.ZipFile(pkg_path, "r") as zf:
        try:
            manifest = json.loads(zf.read("manifest.json"))
        except KeyError:
            return {"ok": False, "error": "invalid package: missing manifest.json"}

        connector = manifest.get("name", "")
        if not connector:
            return {"ok": False, "error": "invalid manifest: missing name"}

        c_root = connector_root if connector_root is not None else _resolve_connector_root()
        connector_dest = c_root / connector

        if connector_dest.exists() and not overwrite:
            return {
                "ok": False,
                "error": f"connector already exists: {connector_dest}. Use --overwrite to replace.",
            }

        try:
            imported_reg = json.loads(zf.read("pipelines-registry.json"))
        except KeyError:
            imported_reg = {"pipelines": {}}

        arc_prefix = f"connectors/{connector}/"
        file_count = 0
        connector_dest_resolved = connector_dest.resolve()
        for item in zf.infolist():
            if not item.filename.startswith(arc_prefix):
                continue
            rel = item.filename[len(arc_prefix):]
            if not rel or rel.endswith("/"):
                continue
            dest = (connector_dest / rel).resolve()
            try:
                dest.relative_to(connector_dest_resolved)
            except ValueError:
                return {"ok": False, "error": f"invalid package: path traversal in entry {item.filename!r}"}
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(zf.read(item.filename))
            file_count += 1

    s_root = state_root if state_root is not None else _resolve_state_root()
    registry_path, existing = _load_registry(s_root)
    existing_pipelines = existing.get("pipelines", {})
    imported_pipelines = imported_reg.get("pipelines", {})

    merged: list[str] = []
    skipped: list[str] = []
    for k, v in imported_pipelines.items():
        if k in existing_pipelines and not overwrite:
            skipped.append(k)
        else:
            existing_pipelines[k] = v
            merged.append(k)

    existing["pipelines"] = existing_pipelines
    _save_registry(registry_path, existing)

    return {
        "ok": True,
        "connector": connector,
        "pkg": str(pkg_path),
        "file_count": file_count,
        "pipelines_merged": merged,
        "pipelines_skipped": skipped,
    }


def _normalize_intent_signature(value: str) -> str:
    """Normalize legacy intent format read.<connector>.<name> to <connector>.read.<name>."""
    sig = str(value or "").strip().strip("'\"")
    m = re.fullmatch(r"(read|write)\.([a-z][a-z0-9_-]*)\.([a-z][a-z0-9_./-]*)", sig)
    if not m:
        return sig
    mode, connector, name = m.groups()
    return f"{connector}.{mode}.{name}"


def cmd_normalize_intents(*, connector: str = "", connector_root: Path | None = None) -> dict:
    """Normalize legacy intent_signature values in connector pipeline YAML files."""
    c_root = connector_root if connector_root is not None else _resolve_connector_root()
    if not c_root.exists():
        return {
            "ok": True,
            "connector_root": str(c_root),
            "connector": connector or "*",
            "normalized_files": 0,
            "scanned_files": 0,
            "changes": [],
        }

    connector = str(connector or "").strip()
    if connector:
        connectors = [c_root / connector]
    else:
        connectors = [p for p in sorted(c_root.iterdir()) if p.is_dir()]

    changes: list[dict[str, str]] = []
    scanned = 0
    for conn_dir in connectors:
        if not conn_dir.exists():
            continue
        for yaml_path in sorted((conn_dir / "pipelines").glob("*/*.yaml")):
            scanned += 1
            text = yaml_path.read_text(encoding="utf-8")
            lines = text.splitlines()
            updated = False
            for i, line in enumerate(lines):
                m = re.match(r"^(\s*intent_signature\s*:\s*)(.+?)\s*$", line)
                if not m:
                    continue
                prefix, raw_val = m.groups()
                normalized = _normalize_intent_signature(raw_val)
                raw_clean = raw_val.strip().strip("'\"")
                if normalized != raw_clean:
                    lines[i] = f"{prefix}{normalized}"
                    updated = True
                    changes.append({"file": str(yaml_path), "from": raw_clean, "to": normalized})
                break
            if updated:
                yaml_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return {
        "ok": True,
        "connector_root": str(c_root),
        "connector": connector or "*",
        "normalized_files": len(changes),
        "scanned_files": scanned,
        "changes": changes,
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
    _atomic_write_json(
        path,
        data,
        prefix="runner-map-",
        suffix=".json",
        ensure_ascii=True,
        indent=2,
    )


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
            desc = item.get('description', '')
            if desc:
                lines.append(f"  description: {desc}")
            lines.append(f"  status: {item.get('status', '')}  source: {item.get('source', 'exec')}")
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


def _extract_scenario_args(scenario_data: dict) -> list:
    """Return required arg names by scanning {{ token }} in scenario YAML."""
    text = json.dumps(scenario_data)
    tokens = set(re.findall(r"\{\{\s*([\w]+)\s*(?:\|[^}]*)?\}\}", text))
    derived: set = set()
    for step in scenario_data.get("steps", []):
        if step.get("type") == "derive":
            derived.update(step.get("compute", {}).keys())
    # Remove derived keys, skip_if_arg values, and boolean/null literals
    skip_args: set = set()
    for step in scenario_data.get("steps", []):
        for k in ("skip_if_arg", "skip_if_arg_missing"):
            if step.get(k):
                skip_args.add(step[k])
    return sorted(tokens - derived - skip_args - {"true", "false", "null"})


def _safe_float(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _load_pipeline_registry_index(state_root: Path) -> dict:
    registry_path = state_root / "pipelines-registry.json"
    if not registry_path.exists():
        return {}
    try:
        data = json.loads(registry_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    raw = data.get("pipelines", {})
    return raw if isinstance(raw, dict) else {}


def _parse_scenario_execution(
    *,
    connector: str,
    scenario_file: str,
    scenario_data: dict,
    registry_index: dict,
) -> dict:
    """Parse strict execution config for cockpit scenarios (initial-version protocol).

    Required YAML shape:
      execution:
        tool: icc_read | icc_write
        pipeline: <pipeline name>
        intent_signature: <optional, defaults to connector.mode.pipeline>
        auto:
          mode: manual | assist | auto
          crystallize_when_synthesis_ready: <bool, default true>
    """
    execution = scenario_data.get("execution")
    if not isinstance(execution, dict):
        raise ValueError(f"{scenario_file}: missing required 'execution' mapping")

    tool = str(execution.get("tool", "")).strip()
    if tool not in {"icc_read", "icc_write"}:
        raise ValueError(f"{scenario_file}: execution.tool must be icc_read or icc_write")
    mode = "read" if tool == "icc_read" else "write"

    pipeline = str(execution.get("pipeline", "")).strip()
    if not pipeline:
        raise ValueError(f"{scenario_file}: execution.pipeline is required")

    intent_signature = str(execution.get("intent_signature", "")).strip() or f"{connector}.{mode}.{pipeline}"

    auto = execution.get("auto") or {}
    if not isinstance(auto, dict):
        raise ValueError(f"{scenario_file}: execution.auto must be a mapping")
    auto_mode = str(auto.get("mode", "assist")).strip().lower() or "assist"
    if auto_mode not in {"manual", "assist", "auto"}:
        raise ValueError(f"{scenario_file}: execution.auto.mode must be manual|assist|auto")
    auto_crystallize = bool(auto.get("crystallize_when_synthesis_ready", True))

    entry = registry_index.get(intent_signature, {}) if isinstance(registry_index, dict) else {}
    if not isinstance(entry, dict):
        entry = {}

    flywheel = {
        "status": str(entry.get("status", "explore")),
        "synthesis_ready": bool(entry.get("synthesis_ready", False)),
        "rollout_pct": int(entry.get("rollout_pct", 0) or 0),
        "success_rate": round(_safe_float(entry.get("success_rate", 0.0)), 4),
        "verify_rate": round(_safe_float(entry.get("verify_rate", 0.0)), 4),
        "human_fix_rate": round(_safe_float(entry.get("human_fix_rate", 0.0)), 4),
        "bridge_ready": str(entry.get("status", "explore")) == "stable",
    }

    return {
        "tool": tool,
        "mode": mode,
        "pipeline": pipeline,
        "intent_signature": intent_signature,
        "auto": {
            "mode": auto_mode,
            "crystallize_when_synthesis_ready": auto_crystallize,
        },
        "flywheel": flywheel,
    }


def cmd_assets() -> dict:
    """Return per-connector assets: notes content, scenario metadata, crystallized components."""
    try:
        connector_root = _resolve_connector_root()
        state_root = _resolve_state_root()
    except Exception:
        return {"connectors": {}}

    registry_index = _load_pipeline_registry_index(state_root)
    connectors: dict = {}
    if not connector_root.exists():
        return {"connectors": connectors}

    for connector_dir in sorted(connector_root.iterdir()):
        if not connector_dir.is_dir() or connector_dir.is_symlink():
            continue
        name = connector_dir.name

        notes_path = connector_dir / "NOTES.md"
        notes = notes_path.read_text(encoding="utf-8") if notes_path.exists() else None

        scenarios: list = []
        scenarios_dir = connector_dir / "scenarios"
        if scenarios_dir.exists():
            for f in sorted(scenarios_dir.iterdir()):
                if f.suffix not in (".yaml", ".yml"):
                    continue
                try:
                    import yaml as _yaml
                    data = _yaml.safe_load(f.read_text(encoding="utf-8"))
                except ImportError:
                    import sys
                    print("[cmd_assets] pyyaml not installed; scenario files skipped", file=sys.stderr)
                    break  # no point iterating further scenario files
                except Exception:
                    data = None
                if not isinstance(data, dict):
                    continue
                try:
                    execution = _parse_scenario_execution(
                        connector=name,
                        scenario_file=f.name,
                        scenario_data=data,
                        registry_index=registry_index,
                    )
                except ValueError as exc:
                    scenarios.append(
                        {
                            "name": data.get("name", f.stem),
                            "description": (data.get("description") or "").strip(),
                            "filename": f.name,
                            "error": str(exc),
                        }
                    )
                    continue
                scenarios.append({
                    "name": data.get("name", f.stem),
                    "description": (data.get("description") or "").strip(),
                    "filename": f.name,
                    "step_count": len(data.get("steps", [])),
                    "has_rollback": bool(data.get("rollback")),
                    "required_args": _extract_scenario_args(data),
                    "execution": execution,
                })

        components: list = []
        cockpit_dir = connector_dir / "cockpit"
        if cockpit_dir.exists():
            for html_file in sorted(cockpit_dir.glob("*.html")):
                ctx_file = cockpit_dir / f"{html_file.stem}.context.md"
                components.append({
                    "filename": html_file.name,
                    "context": ctx_file.read_text(encoding="utf-8") if ctx_file.exists() else "",
                })

        connectors[name] = {"notes": notes, "scenarios": scenarios, "components": components}

    return {"connectors": connectors}


def cmd_submit_actions(actions: list) -> dict:
    """Atomically write pending-actions.json to trigger PendingActionMonitor."""
    state_root = _resolve_repl_root()
    state_root.mkdir(parents=True, exist_ok=True)
    pending_path = state_root / "pending-actions.json"
    tmp_p = state_root / "pending-actions.json.tmp"
    payload = {
        "submitted_at": int(time.time() * 1000),
        "actions": actions,
    }
    tmp_p.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        tmp_p.rename(pending_path)
    except Exception:
        try:
            tmp_p.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return {"ok": True, "action_count": len(actions), "pending_path": str(pending_path)}


def _cmd_set_goal(body: dict) -> dict:
    """Submit a human_edit goal event and return the active snapshot."""
    goal = str(body.get("goal", "")).strip()
    if len(goal) > 120:
        return {"ok": False, "error": "goal must be ≤ 120 characters"}
    lock_window_ms = int(body.get("lock_window_ms", 10 * 60 * 1000) or 0)
    force = bool(body.get("force", False))
    cp = GoalControlPlane(Path(default_hook_state_root()))
    outcome = cp.ingest(
        event_type=EVENT_HUMAN_EDIT,
        source="cockpit",
        actor="cockpit",
        text=goal,
        rationale="cockpit manual goal update",
        confidence=1.0,
        lock_window_ms=lock_window_ms,
        force=force,
    )
    snap = outcome.get("snapshot", {})
    return {
        "ok": True,
        "accepted": bool(outcome.get("accepted", False)),
        "reason": str(outcome.get("reason", "")),
        "goal": str(snap.get("text", "")),
        "goal_source": str(snap.get("source", "unset")),
        "goal_version": int(snap.get("version", 0)),
        "goal_updated_at_ms": int(snap.get("updated_at_ms", 0)),
        "event_id": str(outcome.get("event_id", "")),
    }


def _cmd_goal_history(limit: int = 50) -> dict:
    cp = GoalControlPlane(Path(default_hook_state_root()))
    return {"ok": True, "events": cp.read_ledger(limit=limit)}


def _cmd_goal_rollback(body: dict) -> dict:
    target_event_id = str(body.get("target_event_id", "")).strip()
    if not target_event_id:
        return {"ok": False, "error": "target_event_id is required"}
    cp = GoalControlPlane(Path(default_hook_state_root()))
    try:
        result = cp.rollback(
            target_event_id=target_event_id,
            actor="cockpit",
            rationale="cockpit rollback request",
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    snap = result.get("snapshot", {})
    return {
        "ok": True,
        "accepted": bool(result.get("accepted", False)),
        "reason": str(result.get("reason", "")),
        "goal": str(snap.get("text", "")),
        "goal_source": str(snap.get("source", "unset")),
        "goal_version": int(snap.get("version", 0)),
        "goal_updated_at_ms": int(snap.get("updated_at_ms", 0)),
        "event_id": str(result.get("event_id", "")),
    }


def _cmd_save_settings(patch: dict) -> dict:
    """Merge *patch* into ~/.emerge/settings.json and reset the settings cache.

    Only the ``policy`` sub-object is accepted from the cockpit to limit blast
    radius.  Values are validated by ``_validate_settings`` before writing.
    """
    from scripts.policy_config import (
        default_settings_path,
        load_settings,
        _deep_merge,
        _validate_settings,
        _reset_settings_cache,
        _POLICY_INT_KEYS,
        _POLICY_FLOAT_KEYS,
    )
    import copy

    policy_patch = patch.get("policy")
    if not isinstance(policy_patch, dict):
        return {"ok": False, "error": "request body must have a 'policy' object"}

    # Coerce values to correct types
    coerced: dict = {}
    for k, v in policy_patch.items():
        if k in _POLICY_INT_KEYS:
            try:
                coerced[k] = int(v)
            except (TypeError, ValueError):
                return {"ok": False, "error": f"policy.{k} must be an integer"}
        elif k in _POLICY_FLOAT_KEYS:
            try:
                coerced[k] = float(v)
            except (TypeError, ValueError):
                return {"ok": False, "error": f"policy.{k} must be a number"}
        else:
            return {"ok": False, "error": f"Unknown policy key: {k!r}"}

    path = default_settings_path()
    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
        except Exception:
            existing = {}

    merged = _deep_merge(existing, {"policy": coerced})
    try:
        _validate_settings(merged)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    _atomic_write_json(
        path,
        merged,
        prefix="settings-",
        suffix=".json",
        ensure_ascii=False,
        indent=2,
    )

    _reset_settings_cache()
    updated = load_settings()
    return {"ok": True, "policy": updated.get("policy", {})}


class _ReuseAddrTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True


class _CockpitHandler(http.server.BaseHTTPRequestHandler):
    _shell_path: "Path" = Path(__file__).parent / "cockpit_shell.html"
    _injected: dict = {}  # connector -> list[str html]

    def log_message(self, fmt: str, *args: object) -> None:  # suppress request logs
        pass

    def _cors_origin(self) -> str:
        """Return a safe CORS origin — only allow localhost, never wildcard."""
        origin = self.headers.get("Origin", "")
        if origin.startswith("http://localhost") or origin.startswith("http://127.0.0.1"):
            return origin
        return "null"  # deny cross-origin requests from other sites

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", self._cors_origin())
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._serve_shell()
        elif path == "/api/policy":
            self._json(cmd_policy_status())
        elif path == "/api/assets":
            self._json(cmd_assets())
        elif path == "/api/status":
            state_root = _resolve_repl_root()
            pending = (state_root / "pending-actions.json").exists()
            cc_listening = False
            try:
                lp = _cc_listening_path()
                if lp.exists():
                    d = json.loads(lp.read_text(encoding="utf-8"))
                    # Stale if last_ping more than 6 s ago or deadline passed
                    age = time.time() - d.get("last_ping_ms", 0) / 1000
                    cc_listening = age < 6 and time.time() < d.get("deadline", 0)
            except Exception:
                pass
            self._json({"ok": True, "pending": pending, "cc_listening": cc_listening})
        elif path == "/api/settings":
            from scripts.policy_config import load_settings, default_settings_path
            s = load_settings()
            self._json({"ok": True, "settings": s, "path": str(default_settings_path())})
        elif path == "/api/goal":
            self._json({"ok": True, **_load_hook_state_summary()})
        elif path == "/api/goal-history":
            raw_limit = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("limit", ["50"])[0]
            try:
                limit = max(1, min(500, int(raw_limit)))
            except Exception:
                limit = 50
            self._json(_cmd_goal_history(limit=limit))
        elif path.startswith("/api/components/"):
            self._serve_component(path)
        else:
            self._err(404)

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        body: dict = json.loads(self.rfile.read(length)) if length else {}
        if path == "/api/submit":
            self._json(cmd_submit_actions(body.get("actions", [])))
        elif path == "/api/settings":
            self._json(_cmd_save_settings(body))
        elif path == "/api/goal":
            self._json(_cmd_set_goal(body))
        elif path == "/api/goal/rollback":
            self._json(_cmd_goal_rollback(body))
        elif path == "/api/inject-component":
            connector = str(body.get("connector", ""))
            html = str(body.get("html", ""))
            if connector and html:
                _CockpitHandler._injected.setdefault(connector, []).append(html)
            self._json({"ok": True})
        else:
            self._err(404)

    def _serve_shell(self) -> None:
        if not self._shell_path.exists():
            # Fallback until cockpit_shell.html is created
            body = b"<html><body><h1>Emerge Cockpit</h1><p>cockpit_shell.html not found</p></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = self._shell_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_component(self, path: str) -> None:
        parts = path.strip("/").split("/")  # ["api", "components", "connector", "filename"]
        if len(parts) != 4 or ".." in parts[2] or ".." in parts[3]:
            self._err(404)
            return
        connector, filename = parts[2], parts[3]
        if not filename.endswith(".html"):
            self._err(404)
            return
        try:
            connector_root = _resolve_connector_root()
            fpath = connector_root / connector / "cockpit" / filename
            fpath.resolve().relative_to(connector_root.resolve())
        except (ValueError, Exception):
            self._err(404)
            return
        if not fpath.exists():
            self._err(404)
            return
        body = fpath.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", self._cors_origin())
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code: int) -> None:
        self.send_response(code)
        self.end_headers()


def _cockpit_pid_path() -> "Path":
    return _resolve_repl_root() / "cockpit.pid"


def cmd_serve(port: int = 0, open_browser: bool = False) -> dict:
    """Start the cockpit HTTP server. Idempotent — returns existing instance if already running."""
    pid_path = _cockpit_pid_path()

    # Reuse existing instance if alive
    if pid_path.exists():
        try:
            info = json.loads(pid_path.read_text(encoding="utf-8"))
            existing_pid = int(info["pid"])
            existing_port = int(info["port"])
            os.kill(existing_pid, 0)  # raises OSError if process is gone
            url = f"http://localhost:{existing_port}"
            if open_browser:
                webbrowser.open(url)
            return {"ok": True, "port": existing_port, "url": url, "reused": True}
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            pid_path.unlink(missing_ok=True)

    server = _ReuseAddrTCPServer(("127.0.0.1", port), _CockpitHandler)
    actual_port = server.server_address[1]
    url = f"http://localhost:{actual_port}"

    # Write pid file so CC (and serve-stop) can manage lifecycle
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(
        json.dumps({"pid": os.getpid(), "port": actual_port}), encoding="utf-8"
    )

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    if open_browser:
        webbrowser.open(url)
    return {"ok": True, "port": actual_port, "url": url, "reused": False}


def cmd_serve_stop() -> dict:
    """Stop the running cockpit server by killing the pid in cockpit.pid."""
    import signal as _signal
    pid_path = _cockpit_pid_path()
    if not pid_path.exists():
        return {"ok": False, "reason": "no cockpit.pid found — server may not be running"}
    try:
        info = json.loads(pid_path.read_text(encoding="utf-8"))
        pid = int(info["pid"])
        port = int(info["port"])
        os.kill(pid, _signal.SIGTERM)
        pid_path.unlink(missing_ok=True)
        return {"ok": True, "stopped_pid": pid, "port": port}
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as e:
        pid_path.unlink(missing_ok=True)
        return {"ok": False, "reason": str(e)}


def _enrich_actions(actions: list) -> list:
    """Enrich action payloads with context CC needs to execute them intelligently.

    For ``notes-comment`` actions: inject the connector's current NOTES.md content
    so CC can treat the comment as an edit instruction (not a literal append).

    For ``global-prompt`` actions: inject an instruction telling CC to execute
    the ``prompt`` field as a direct user request.

    For ``tool-call`` actions: inject deterministic execution guidance so
    framework-level tool invocations remain explicit and auditable.
    """
    connector_root = _resolve_connector_root()
    enriched = []
    for action in actions:
        a = dict(action)
        if a.get("type") == "global-prompt":
            a["instruction"] = (
                "The user has queued a free-form instruction via the Emerge Cockpit. "
                "Execute the `prompt` field as a direct user request. "
                "Treat it exactly as if the user had typed it in the chat."
            )
        elif a.get("type") == "notes-comment":
            connector = a.get("connector", "")
            if connector:
                notes_path = connector_root / connector / "NOTES.md"
                try:
                    notes_path.resolve().relative_to(connector_root.resolve())
                    current_notes = notes_path.read_text(encoding="utf-8") if notes_path.exists() else ""
                except (ValueError, OSError):
                    current_notes = ""
                a["current_notes"] = current_notes
                a["notes_path"] = str(notes_path)
                a["instruction"] = (
                    "The user has provided an edit instruction for the connector's NOTES.md. "
                    "Read `current_notes`, apply the `comment` as a natural-language edit "
                    "(e.g. fix a mistake, add a detail, restructure a section, remove stale info). "
                    "Rewrite the file at `notes_path` with your judgment — do NOT blindly append. "
                    "Preserve existing useful content. Keep the file concise and accurate."
                )
        elif a.get("type") == "tool-call":
            call = a.get("call")
            if not isinstance(call, dict):
                a["instruction"] = (
                    "Invalid cockpit tool-call action: missing `call` object. "
                    "Do not improvise; ask user to re-submit."
                )
            else:
                tool_name = str(call.get("tool", "")).strip()
                arguments = call.get("arguments", {})
                if tool_name not in {"icc_read", "icc_write"} or not isinstance(arguments, dict):
                    a["instruction"] = (
                        "Invalid cockpit tool-call payload. "
                        "Expected call.tool in {icc_read, icc_write} and call.arguments object."
                    )
                else:
                    auto = a.get("auto") if isinstance(a.get("auto"), dict) else {}
                    auto_mode = str(auto.get("mode", "assist"))
                    a["instruction"] = (
                        "Deterministic tool call (no free-form reasoning): "
                        f"call `{tool_name}` exactly once with `call.arguments`; "
                        "return the tool output to the user. "
                        f"intent_signature={a.get('intent_signature', '')}. "
                        f"automation_mode={auto_mode}. "
                        "Only if automation_mode=auto AND flywheel.synthesis_ready=true, "
                        "queue a follow-up crystallization suggestion."
                    )
        enriched.append(a)
    return enriched


def _cc_listening_path() -> "Path":
    return _resolve_repl_root() / "cc-listening.json"


def _write_cc_listening(deadline: float) -> None:
    """Write/refresh the CC-listening heartbeat file (atomic)."""
    body = {
        "pid": os.getpid(),
        "deadline": deadline,
        "last_ping_ms": int(time.time() * 1000),
    }
    p = _cc_listening_path()
    _atomic_write_json(
        p,
        body,
        prefix="cc-listening-",
        suffix=".json",
        ensure_ascii=True,
        indent=None,
    )


def _remove_cc_listening() -> None:
    try:
        _cc_listening_path().unlink(missing_ok=True)
    except OSError:
        pass


def cmd_wait_for_submit(timeout_seconds: int = 600) -> dict:
    """Block until pending-actions.json appears, then return its actions.

    Used by the /cockpit command's dispatch loop: CC calls this, blocks until
    the user submits from the browser, then CC executes the returned actions.
    Renames the file to .processed.json before returning so the next call
    starts fresh.

    Actions are enriched before return: ``notes-comment`` actions receive the
    connector's current NOTES.md content so CC can edit intelligently rather
    than appending blindly.

    While waiting, writes/refreshes ``cc-listening.json`` every poll cycle so
    the cockpit UI can show a live "CC ready" indicator and disable the submit
    button when CC is not listening.
    """
    state_root = _resolve_repl_root()
    state_root.mkdir(parents=True, exist_ok=True)
    pending_path = state_root / "pending-actions.json"
    deadline = time.time() + timeout_seconds
    _write_cc_listening(deadline)
    _last_ping = time.time()
    try:
        while time.time() < deadline:
            if pending_path.exists():
                try:
                    data = json.loads(pending_path.read_text(encoding="utf-8"))
                    actions = _enrich_actions(data.get("actions", []))
                    submitted_at = data.get("submitted_at", 0)
                    processed = pending_path.with_name("pending-actions.processed.json")
                    pending_path.rename(processed)
                    return {"ok": True, "actions": actions, "submitted_at": submitted_at, "action_count": len(actions)}
                except (json.JSONDecodeError, OSError):
                    pass
            time.sleep(0.5)
            # Refresh heartbeat every 2 s so the page sees a live signal.
            if time.time() - _last_ping >= 2:
                _write_cc_listening(deadline)
                _last_ping = time.time()
    finally:
        _remove_cc_listening()
    return {"ok": False, "timeout": True, "actions": []}


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
            "connector-export",
            "connector-import",
            "normalize-intents",
            "serve",
            "serve-stop",
            "wait-for-submit",
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
    parser.add_argument("--connector", default="", help="Connector name for connector-export")
    parser.add_argument("--out", default="", help="Output zip path for connector-export")
    parser.add_argument("--pkg", default="", help="Package zip path for connector-import")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing connector/registry on import")
    parser.add_argument("--open", action="store_true", help="Open browser after starting cockpit server")
    parser.add_argument("--port", type=int, default=0, help="Port for cockpit serve (0 = auto-assign free port)")
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
    elif args.command == "connector-export":
        out = cmd_connector_export(
            connector=str(args.connector),
            out=str(args.out) if args.out else f"{args.connector}-emerge-pkg.zip",
        )
    elif args.command == "connector-import":
        out = cmd_connector_import(
            pkg=str(args.pkg),
            overwrite=bool(args.overwrite),
        )
    elif args.command == "normalize-intents":
        out = cmd_normalize_intents(
            connector=str(args.connector),
        )
    elif args.command == "serve":
        port = getattr(args, "port", 0) or 0
        open_b = getattr(args, "open", False)
        result = cmd_serve(port=port, open_browser=open_b)
        status = "reused existing" if result.get("reused") else "started"
        print(f"Cockpit running at {result['url']} ({status})")
        if not result.get("reused"):
            print("Press Ctrl-C to stop.")
            import time as _time
            try:
                while True:
                    _time.sleep(1)
            except KeyboardInterrupt:
                cmd_serve_stop()
        sys.exit(0)
    elif args.command == "serve-stop":
        out = cmd_serve_stop()
        print(json.dumps(out))
        sys.exit(0)
    elif args.command == "wait-for-submit":
        timeout = int(getattr(args, "runner_port", None) or 600)  # reuse --runner-port as timeout if needed
        out = cmd_wait_for_submit(timeout_seconds=timeout)
        print(json.dumps(out, ensure_ascii=False))
        sys.exit(0)
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
