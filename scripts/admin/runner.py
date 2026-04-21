"""Runner administration — deploy, self-install, config, status.

Called by repl_admin.py (CLI) and CockpitHTTPServer (HTTP API).
"""
from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

from scripts.admin.shared import _detect_lan_ip  # noqa: E402


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
# Runner self-install (operator curl | bash / irm | iex)
# ---------------------------------------------------------------------------

_RUNNER_FILES: list[str] = [
    "scripts/remote_runner.py",
    "scripts/runner_watchdog.py",
    "scripts/exec_session.py",
    "scripts/runner_client.py",
    "scripts/policy_config.py",
    "requirements-runner.txt",
    "assets/icon-tray.png",
]


def _build_runner_zip(plugin_root: Path) -> bytes:
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel in _RUNNER_FILES:
            p = plugin_root / rel
            if p.is_file():
                info = zipfile.ZipInfo(rel)
                info.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(info, p.read_bytes())
    return buf.getvalue()


def _build_runner_tarball(plugin_root: Path) -> bytes:
    # Build uncompressed tar first, then gzip with mtime=0 so both the
    # .tar.gz and .sha256 endpoints produce identical bytes across calls.
    raw_buf = io.BytesIO()
    with tarfile.open(fileobj=raw_buf, mode="w") as tar:
        for rel in _RUNNER_FILES:
            p = plugin_root / rel
            if p.is_file():
                ti = tar.gettarinfo(str(p), arcname=rel)
                ti.mtime = 0
                with open(str(p), "rb") as f:
                    tar.addfile(ti, f)
    import gzip as _gzip
    gz_buf = io.BytesIO()
    with _gzip.GzipFile(fileobj=gz_buf, mode="wb", mtime=0) as gz:
        gz.write(raw_buf.getvalue())
    return gz_buf.getvalue()


def _generate_runner_install_sh(
    *,
    team_lead_url: str,
    runner_port: int,
) -> str:
    tl_json = json.dumps(team_lead_url.rstrip("/"))
    return rf"""#!/usr/bin/env bash
set -euo pipefail

TEAM_LEAD_URL={tl_json}
PROFILE="${{EMERGE_PROFILE:-${{PROFILE:-$(hostname -s 2>/dev/null || hostname)}}}}"
RUNNER_PORT="{runner_port}"
RUNNER_ROOT="$HOME/.emerge/runner"
INSTALL_STAGE="init"
trap 'rc=$?; if [ $rc -ne 0 ]; then echo "[Install][$INSTALL_STAGE] failed (exit=$rc)" >&2; fi' EXIT

echo "=== Emerge Runner Installer ==="

USE_CN_MIRROR=0
if ! curl -s --max-time 3 https://pypi.org > /dev/null 2>&1; then
  echo "[CN] Using pip mirror: pypi.tuna.tsinghua.edu.cn"
  USE_CN_MIRROR=1
fi

PYTHON=""
for py in python3.12 python3.11 python3.10 python3.9 python3 python; do
  if command -v "$py" &>/dev/null; then
    VER=$($py -c "import sys; print(int(sys.version_info >= (3,9)))" 2>/dev/null || echo 0)
    if [ "$VER" = "1" ]; then PYTHON="$py"; break; fi
  fi
done

if [ -z "$PYTHON" ]; then
  echo "[Install] Python 3.9+ not found. Attempting to install..."
  if command -v apt-get &>/dev/null; then
    sudo apt-get install -y python3 python3-pip 2>/dev/null || true
  elif command -v dnf &>/dev/null; then
    sudo dnf install -y python3 2>/dev/null || true
  elif command -v yum &>/dev/null; then
    sudo yum install -y python3 2>/dev/null || true
  elif [ "$(uname -s)" = "Darwin" ] && command -v brew &>/dev/null; then
    brew install python3 2>/dev/null || true
  fi
  for py in python3.12 python3.11 python3.10 python3.9 python3 python; do
    if command -v "$py" &>/dev/null; then
      VER=$($py -c "import sys; print(int(sys.version_info >= (3,9)))" 2>/dev/null || echo 0)
      if [ "$VER" = "1" ]; then PYTHON="$py"; break; fi
    fi
  done
fi
if [ -z "$PYTHON" ]; then
  echo "[Install] Python 3.9+ install failed — install python3 manually and re-run." >&2
  exit 1
fi

echo "[OK] $($PYTHON --version)"

INSTALL_STAGE="dependency_check"
if ! command -v curl >/dev/null 2>&1; then
  echo "[Install] curl is required but not found." >&2
  exit 1
fi
if ! command -v tar >/dev/null 2>&1; then
  echo "[Install] tar is required but not found." >&2
  exit 1
fi

INSTALL_STAGE="download"
mkdir -p "$RUNNER_ROOT"
curl -fsSL "$TEAM_LEAD_URL/runner-dist/runner.tar.gz" -o "$RUNNER_ROOT/runner.tar.gz"
curl -fsSL "$TEAM_LEAD_URL/runner-dist/runner.tar.gz.sha256" -o "$RUNNER_ROOT/runner.tar.gz.sha256"
EXPECTED_SHA="$(awk '{{print $1}}' "$RUNNER_ROOT/runner.tar.gz.sha256" | tr -d '\r\n')"
ACTUAL_SHA="$($PYTHON -c "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$RUNNER_ROOT/runner.tar.gz")"
if [ "$EXPECTED_SHA" != "$ACTUAL_SHA" ]; then
  echo "[Install] runner.tar.gz SHA256 mismatch — aborting." >&2
  exit 1
fi
INSTALL_STAGE="extract"
tar -xzf "$RUNNER_ROOT/runner.tar.gz" -C "$RUNNER_ROOT"
rm -f "$RUNNER_ROOT/runner.tar.gz" "$RUNNER_ROOT/runner.tar.gz.sha256"

PIP_ARGS=""
if [ "$USE_CN_MIRROR" = "1" ]; then
  PIP_ARGS="--index-url https://pypi.tuna.tsinghua.edu.cn/simple"
fi
if [ -f "$RUNNER_ROOT/requirements-runner.txt" ]; then
  $PYTHON -m pip install $PIP_ARGS -r "$RUNNER_ROOT/requirements-runner.txt" 2>/dev/null || true
fi

mkdir -p "$HOME/.emerge"
cat > "$HOME/.emerge/runner-config.json" <<JSON
{{
  "team_lead_url": {tl_json},
  "runner_profile": "$PROFILE",
  "port": {runner_port},
  "installed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date)"
}}
JSON

OS="$(uname -s)"
START_MODE="unknown"
INSTALL_STAGE="autostart"
if [ "$OS" = "Darwin" ]; then
  PYTHON_BIN="$(command -v "$PYTHON")"
  PLIST="$HOME/Library/LaunchAgents/com.emerge.runner.plist"
  mkdir -p "$HOME/Library/LaunchAgents"
  cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.emerge.runner</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON_BIN</string>
    <string>$RUNNER_ROOT/scripts/runner_watchdog.py</string>
    <string>--host</string><string>0.0.0.0</string>
    <string>--port</string><string>$RUNNER_PORT</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict><key>EMERGE_TEAM_LEAD_URL</key><string>$TEAM_LEAD_URL</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>WorkingDirectory</key><string>$RUNNER_ROOT</string>
</dict></plist>
PLIST
  launchctl bootout gui/"$(id -u)" "$PLIST" 2>/dev/null || true
  launchctl bootstrap gui/"$(id -u)" "$PLIST"
  echo "[OK] macOS LaunchAgent registered"
  START_MODE="launchctl"
else
  PYTHON_BIN="$(command -v "$PYTHON")"
  start_now() {{
    nohup "$PYTHON_BIN" "$RUNNER_ROOT/scripts/runner_watchdog.py" --host 0.0.0.0 --port "$RUNNER_PORT" >/dev/null 2>&1 &
  }}
  if command -v systemctl >/dev/null 2>&1; then
    SERVICE_DIR="$HOME/.config/systemd/user"
    mkdir -p "$SERVICE_DIR"
    cat > "$SERVICE_DIR/emerge-runner.service" <<SERVICE
[Unit]
Description=Emerge Runner
After=network.target

[Service]
ExecStart="$PYTHON_BIN" "$RUNNER_ROOT/scripts/runner_watchdog.py" --host 0.0.0.0 --port $RUNNER_PORT
WorkingDirectory=$RUNNER_ROOT
Restart=always
Environment="EMERGE_TEAM_LEAD_URL=$TEAM_LEAD_URL"

[Install]
WantedBy=default.target
SERVICE
    if systemctl --user daemon-reload 2>/dev/null && systemctl --user enable --now emerge-runner 2>/dev/null; then
      echo "[OK] systemd user service configured"
      START_MODE="systemd-user"
    else
      echo "[Warn] systemd user service unavailable — falling back to non-systemd autostart." >&2
    fi
  fi
  if [ "$START_MODE" = "unknown" ] && command -v crontab >/dev/null 2>&1; then
    CRON_LINE="@reboot \"$PYTHON_BIN\" \"$RUNNER_ROOT/scripts/runner_watchdog.py\" --host 0.0.0.0 --port $RUNNER_PORT >/dev/null 2>&1"
    (
      crontab -l 2>/dev/null | awk 'index($0,"runner_watchdog.py")==0' || true
      echo "$CRON_LINE"
    ) | crontab -
    start_now
    echo "[OK] cron @reboot configured"
    START_MODE="cron"
  fi
  if [ "$START_MODE" = "unknown" ] && [ -d "$HOME/.config" ]; then
    AUTOSTART_DIR="$HOME/.config/autostart"
    mkdir -p "$AUTOSTART_DIR"
    cat > "$AUTOSTART_DIR/emerge-runner.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Emerge Runner
Exec="$PYTHON_BIN" "$RUNNER_ROOT/scripts/runner_watchdog.py" --host 0.0.0.0 --port $RUNNER_PORT
X-GNOME-Autostart-enabled=true
Terminal=false
DESKTOP
    start_now
    echo "[OK] XDG autostart desktop entry configured"
    START_MODE="xdg-autostart"
  fi
  if [ "$START_MODE" = "unknown" ]; then
    start_now
    START_MODE="manual-no-boot-autostart"
    echo "[Warn] No autostart manager detected — started watchdog only for current session." >&2
  fi
fi

INSTALL_STAGE="health_check"
sleep 2
if curl -s --max-time 5 "http://localhost:$RUNNER_PORT/health" 2>/dev/null | grep -q 'true'; then
  echo "[OK] Runner healthy at http://localhost:$RUNNER_PORT"
else
  echo "[Warn] Check: curl http://localhost:$RUNNER_PORT/health"
fi

INSTALL_STAGE="done"
echo "=== Done. Profile $PROFILE (override: EMERGE_PROFILE=<name>) → $TEAM_LEAD_URL (start_mode=$START_MODE) ==="
"""


def _generate_runner_install_ps1(
    *,
    team_lead_url: str,
    runner_port: int,
) -> str:
    # Use PS1 single-quoted literals: safe for URLs (no interpolation, '' escapes literal ')
    ps1_tl = "'" + team_lead_url.rstrip("/").replace("'", "''") + "'"
    return f"""$ErrorActionPreference = "Stop"

$TEAM_LEAD_URL = {ps1_tl}
$RUNNER_NAME = if ($env:EMERGE_PROFILE) {{ $env:EMERGE_PROFILE }} else {{ $env:COMPUTERNAME }}
$RUNNER_PORT = {runner_port}
$RUNNER_ROOT = "$env:USERPROFILE\\.emerge\\runner"
$INSTALL_STAGE = "init"
$START_MODE = "unknown"
trap {{
    Write-Host "[Install][$INSTALL_STAGE] $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}}

Write-Host "=== Emerge Runner Installer ===" -ForegroundColor Cyan

$USE_CN_MIRROR = $true

$PYTHON = $null
foreach ($py in @("python", "python3")) {{
    try {{
        $ver = & $py -c "import sys; print(int(sys.version_info >= (3,9)))" 2>$null
        if ($ver.Trim() -eq "1") {{ $PYTHON = $py; break }}
    }} catch {{}}
}}

if (-not $PYTHON) {{
    Write-Host "[Install] Python 3.9+ not found. Attempting install via winget..." -ForegroundColor Yellow
    try {{
        winget install --id Python.Python.3.11 --silent --accept-package-agreements --accept-source-agreements 2>$null | Out-Null
        $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("PATH","User")
        foreach ($py in @("python", "python3")) {{
            try {{
                $ver = & $py -c "import sys; print(int(sys.version_info >= (3,9)))" 2>$null
                if ($ver.Trim() -eq "1") {{ $PYTHON = $py; break }}
            }} catch {{}}
        }}
    }} catch {{}}
}}
if (-not $PYTHON) {{
    Write-Host "[Install] Python 3.9+ required. Install from https://www.python.org and re-run." -ForegroundColor Red
    exit 1
}}

Write-Host "[OK] $(& $PYTHON --version)"

$INSTALL_STAGE = "download"
New-Item -Force -ItemType Directory -Path $RUNNER_ROOT | Out-Null
$INSTALL_STAGE = "extract"
& $PYTHON -c "import urllib.request,zipfile,io,sys; url=sys.argv[1]+'/runner-dist/runner.zip'; d=urllib.request.urlopen(url,timeout=60).read(); z=zipfile.ZipFile(io.BytesIO(d)); z.extractall(sys.argv[2]); print('extracted '+str(len(z.namelist()))+' files')" $TEAM_LEAD_URL $RUNNER_ROOT
if ($LASTEXITCODE -ne 0) {{ throw "Download/extract failed (exit $LASTEXITCODE)" }}

$INSTALL_STAGE = "pip_install"
$pipArgs = @()
if ($USE_CN_MIRROR) {{ $pipArgs = @("--index-url", "https://pypi.tuna.tsinghua.edu.cn/simple") }}
$req = Join-Path $RUNNER_ROOT "requirements-runner.txt"
if (Test-Path $req) {{
    $ErrorActionPreference = "Continue"
    & $PYTHON -m pip install @pipArgs -r $req
    if ($LASTEXITCODE -ne 0) {{
        Write-Host "[Warn] pip install failed (non-fatal, tray icon disabled)" -ForegroundColor Yellow
    }}
    $ErrorActionPreference = "Stop"
}}

$INSTALL_STAGE = "config_write"
New-Item -Force -ItemType Directory -Path "$env:USERPROFILE\\.emerge" | Out-Null
$cfg = @{{
    team_lead_url = $TEAM_LEAD_URL
    runner_profile = $RUNNER_NAME
    port = $RUNNER_PORT
    installed_at = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
}} | ConvertTo-Json -Depth 3
$cfg | Out-File -FilePath "$env:USERPROFILE\\.emerge\\runner-config.json" -Encoding utf8

$INSTALL_STAGE = "vbs_write"
$pythonPath = (Get-Command $PYTHON -ErrorAction SilentlyContinue).Source
if (-not $pythonPath) {{ $pythonPath = $PYTHON }}
$vbsPath = "$env:USERPROFILE\\.emerge\\start_emerge_runner.vbs"
$vbs = ('Set sh = CreateObject("WScript.Shell")' + [char]13 + [char]10 +
        'sh.CurrentDirectory = "' + $RUNNER_ROOT + '"' + [char]13 + [char]10 +
        'sh.Run Chr(34) & "' + $pythonPath + '" & Chr(34) & " " & Chr(34) & "' + $RUNNER_ROOT + '\\scripts\\runner_watchdog.py" & Chr(34) & " --host 0.0.0.0 --port ' + $RUNNER_PORT + '", 0, False')
[System.IO.File]::WriteAllText($vbsPath, $vbs, (New-Object System.Text.UTF8Encoding($false)))

$regKey = "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run"
$INSTALL_STAGE = "autostart"
try {{
    Set-ItemProperty -Path $regKey -Name "EmergeRunner" -Value "wscript.exe `"$vbsPath`""
    $START_MODE = "registry-run"
    Write-Host "[OK] Registry autostart: EmergeRunner"
}} catch {{
    $startupDir = [System.Environment]::GetFolderPath("Startup")
    if ($startupDir) {{
        $startupVbs = Join-Path $startupDir "EmergeRunner.vbs"
        Copy-Item -Force $vbsPath $startupVbs
        $START_MODE = "startup-folder"
        Write-Host "[Warn] Registry autostart unavailable — using Startup folder fallback." -ForegroundColor Yellow
    }} else {{
        $START_MODE = "manual-no-boot-autostart"
        Write-Host "[Warn] No autostart target found — current session only." -ForegroundColor Yellow
    }}
}}

$INSTALL_STAGE = "stop_old"
Get-WmiObject Win32_Process -Filter "Name='python.exe' OR Name='python3.exe'" |
    Where-Object {{ $_.CommandLine -like '*runner_watchdog*' -or $_.CommandLine -like '*remote_runner*' }} |
    ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}
Start-Sleep 1

Start-Process "wscript.exe" -ArgumentList "`"$vbsPath`""

$INSTALL_STAGE = "health_check"
Start-Sleep 4
try {{
    $h = Invoke-RestMethod -Uri "http://localhost:$RUNNER_PORT/health" -TimeoutSec 5
    if ($h.ok) {{ Write-Host "[OK] Runner healthy" -ForegroundColor Green }}
}} catch {{
    Write-Host "[Warn] Runner may still be starting."
}}

$INSTALL_STAGE = "done"
Write-Host "=== Done. Profile $RUNNER_NAME (override: `$env:EMERGE_PROFILE=<name>) -> $TEAM_LEAD_URL (start_mode=$START_MODE) ===" -ForegroundColor Cyan
"""


def cmd_runner_install_url(
    *,
    runner_port: int = 8787,
    daemon_port: int = 8789,
) -> dict:
    """Return copy-paste install commands for Linux/macOS and Windows.

    Profile is auto-detected from hostname on the runner machine.
    Override: EMERGE_PROFILE=<name> curl ... | bash
    """
    if runner_port <= 0 or runner_port > 65535:
        raise ValueError("runner_port must be in 1..65535")
    if daemon_port <= 0 or daemon_port > 65535:
        raise ValueError("daemon_port must be in 1..65535")
    lan_ip = _detect_lan_ip()
    team_lead_url = f"http://{lan_ip}:{daemon_port}".rstrip("/")
    from urllib.parse import quote

    qp = quote(str(runner_port), safe="")
    base = f"{team_lead_url}/runner-install"
    bash_cmd = f'curl -fsSL "{base}.sh?port={qp}" | bash'
    ps_cmd = f'irm "{base}.ps1?port={qp}" | iex'
    return {
        "ok": True,
        "runner_port": runner_port,
        "daemon_port": daemon_port,
        "team_lead_url": team_lead_url,
        "bash": bash_cmd,
        "powershell": ps_cmd,
    }

