# Runner Self-Install Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Operators install the emerge runner with a single `curl | bash` or `irm | iex` command — no SSH from CC, no manual config, auto-installs Python, detects CN mirrors, configures platform autostart.

**Architecture:** The emerge daemon (port 8789) serves dynamically-generated install scripts at `/runner-install.sh` and `/runner-install.ps1` with all config baked in (LAN IP, profile, port). A new `/runner-dist/runner.tar.gz` endpoint bundles runner files. The cockpit Monitors tab gains an "Add Runner" panel showing copy-ready install commands. `runner-bootstrap` (SSH path) is deleted.

**Tech Stack:** Python stdlib (socket, tarfile, io), bash, PowerShell, HTML/JS (existing cockpit dark theme)

---

## File structure

| File | Role |
|---|---|
| `scripts/admin/shared.py` | Add `_detect_lan_ip()` — outgoing-interface LAN IP detection |
| `scripts/admin/runner.py` | Add `_RUNNER_FILES`, `_build_runner_tarball()`, `_generate_runner_install_sh()`, `_generate_runner_install_ps1()`, `cmd_runner_install_url()`; delete `cmd_runner_bootstrap()` and all SSH helpers |
| `scripts/daemon_http.py` | Add `GET /runner-install.sh`, `GET /runner-install.ps1`, `GET /runner-dist/runner.tar.gz` to `do_GET` |
| `scripts/admin/cockpit.py` | Add `GET /api/control-plane/runner-install-url` endpoint |
| `scripts/repl_admin.py` | Add `runner-install-url` subcommand; delete `runner-bootstrap` |
| `scripts/runner_watchdog.py` | Read `~/.emerge/runner-config.json` and inject `EMERGE_TEAM_LEAD_URL` into runner subprocess env |
| `requirements-runner.txt` | New: optional pystray/Pillow deps |
| `scripts/cockpit_shell.html` | Add "Add Runner" panel to `renderMonitorsTab()` |
| `tests/test_runner_self_install.py` | New: all tests for the self-install feature |
| `tests/test_repl_admin.py` | Delete bootstrap tests; add install-url test |
| `skills/remote-runner-dev/SKILL.md` | Replace Bootstrap section with self-install |
| `skills/initializing-vertical-flywheel/SKILL.md` | Update runner onboarding step |
| `commands/init.md` | Replace runner-bootstrap step |

---

## Task 1: `_detect_lan_ip()` in shared.py

**Files:**
- Modify: `scripts/admin/shared.py`
- Test: `tests/test_runner_self_install.py`

- [ ] **Step 1: Create test file with failing test**

```python
# tests/test_runner_self_install.py
from scripts.admin.shared import _detect_lan_ip


def test_detect_lan_ip_returns_non_loopback():
    ip = _detect_lan_ip()
    assert ip, "should return a non-empty string"
    assert not ip.startswith("127."), f"must not be loopback, got {ip!r}"
    assert ip.count(".") == 3, f"should be IPv4 dotted notation, got {ip!r}"


def test_detect_lan_ip_cached():
    ip1 = _detect_lan_ip()
    ip2 = _detect_lan_ip()
    assert ip1 == ip2, "repeated calls must return the same value (cached)"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_runner_self_install.py -v
```
Expected: `ImportError` or `AttributeError` — `_detect_lan_ip` not yet defined.

- [ ] **Step 3: Add `_detect_lan_ip()` to shared.py**

Add after `_resolve_connector_root()`, before any existing function that uses it:

```python
import socket as _socket

_lan_ip_cache: str = ""


def _detect_lan_ip() -> str:
    """Return the machine's outgoing LAN IPv4 address (cached per process).

    Uses a UDP connect trick (no packets sent) to find which interface the OS
    would use for outgoing traffic. Falls back to hostname resolution.
    Never returns 127.x.x.x or empty string.
    """
    global _lan_ip_cache
    if _lan_ip_cache:
        return _lan_ip_cache
    try:
        with _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        if ip and not ip.startswith("127."):
            _lan_ip_cache = ip
            return _lan_ip_cache
    except Exception:
        pass
    try:
        ip = _socket.gethostbyname(_socket.gethostname())
        if ip and not ip.startswith("127."):
            _lan_ip_cache = ip
            return _lan_ip_cache
    except Exception:
        pass
    _lan_ip_cache = "127.0.0.1"  # last resort — only on machines with no network
    return _lan_ip_cache
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_runner_self_install.py::test_detect_lan_ip_returns_non_loopback tests/test_runner_self_install.py::test_detect_lan_ip_cached -v
```
Expected: PASS (both tests).

- [ ] **Step 5: Run full suite to confirm no regressions**

```bash
python -m pytest tests -q
```
Expected: 635 passed.

- [ ] **Step 6: Commit**

```bash
git add scripts/admin/shared.py tests/test_runner_self_install.py
git commit -m "feat: add _detect_lan_ip() to shared.py with caching"
```

---

## Task 2: `requirements-runner.txt` + tarball builder

**Files:**
- Create: `requirements-runner.txt`
- Modify: `scripts/admin/runner.py`
- Test: `tests/test_runner_self_install.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_runner_self_install.py`:

```python
import io, tarfile
from pathlib import Path
from scripts.admin.runner import _RUNNER_FILES, _build_runner_tarball

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def test_runner_files_constant_all_exist():
    for rel in _RUNNER_FILES:
        p = _PLUGIN_ROOT / rel
        assert p.exists(), f"_RUNNER_FILES entry missing on disk: {rel}"


def test_build_runner_tarball_contains_all_files():
    data = _build_runner_tarball(_PLUGIN_ROOT)
    assert len(data) > 100, "tarball should not be empty"
    buf = io.BytesIO(data)
    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        names = tar.getnames()
    for rel in _RUNNER_FILES:
        assert rel in names, f"missing from tarball: {rel}"
```

- [ ] **Step 2: Run to verify failures**

```bash
python -m pytest tests/test_runner_self_install.py::test_runner_files_constant_all_exist tests/test_runner_self_install.py::test_build_runner_tarball_contains_all_files -v
```
Expected: FAIL — `_RUNNER_FILES` and `_build_runner_tarball` not defined.

- [ ] **Step 3: Create `requirements-runner.txt`**

```
# Emerge runner runtime dependencies
# The runner uses only Python stdlib — no hard pip deps.
# Optional: tray icon support (silently skipped if missing).
pystray>=0.19; sys_platform != "linux"
Pillow>=9.0; sys_platform != "linux"
```

- [ ] **Step 4: Add `_RUNNER_FILES` and `_build_runner_tarball` to runner.py**

Add after the existing imports in `scripts/admin/runner.py`:

```python
import io as _io
import tarfile as _tarfile

_RUNNER_FILES: list[str] = [
    "scripts/remote_runner.py",
    "scripts/runner_watchdog.py",
    "scripts/exec_session.py",
    "scripts/runner_client.py",
    "scripts/policy_config.py",
    "requirements-runner.txt",
]


def _build_runner_tarball(root: Path) -> bytes:
    """Build an in-memory .tar.gz of all runner files from plugin root."""
    buf = _io.BytesIO()
    with _tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel in _RUNNER_FILES:
            p = root / rel
            if p.exists():
                tar.add(str(p), arcname=rel)
    return buf.getvalue()
```

- [ ] **Step 5: Run tests to verify pass**

```bash
python -m pytest tests/test_runner_self_install.py::test_runner_files_constant_all_exist tests/test_runner_self_install.py::test_build_runner_tarball_contains_all_files -v
```
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add requirements-runner.txt scripts/admin/runner.py tests/test_runner_self_install.py
git commit -m "feat: add requirements-runner.txt and _build_runner_tarball()"
```

---

## Task 3: Install script generators (bash + PowerShell)

**Files:**
- Modify: `scripts/admin/runner.py`
- Test: `tests/test_runner_self_install.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_runner_self_install.py`:

```python
from scripts.admin.runner import _generate_runner_install_sh, _generate_runner_install_ps1


def test_generate_install_sh_embeds_config():
    script = _generate_runner_install_sh(
        team_lead_url="http://10.0.0.1:8789",
        profile="test-runner",
        runner_port=8787,
    )
    assert 'TEAM_LEAD_URL="http://10.0.0.1:8789"' in script
    assert 'PROFILE="test-runner"' in script
    assert 'RUNNER_PORT="8787"' in script
    assert "#!/usr/bin/env bash" in script
    assert "pypi.org" in script, "should probe for CN mirror"
    assert "launchctl" in script, "should have macOS autostart"
    assert "systemctl" in script, "should have Linux autostart"
    assert "runner.tar.gz" in script, "should download tarball"


def test_generate_install_ps1_embeds_config():
    script = _generate_runner_install_ps1(
        team_lead_url="http://10.0.0.1:8789",
        profile="test-runner",
        runner_port=8787,
    )
    assert '$TEAM_LEAD_URL = "http://10.0.0.1:8789"' in script
    assert '$PROFILE = "test-runner"' in script
    assert "$RUNNER_PORT = 8787" in script
    assert "EmergeRunner" in script, "should set registry autostart key"
    assert "runner.tar.gz" in script, "should download tarball"
    assert "winget" in script, "should try winget for Python install"
```

- [ ] **Step 2: Run to verify failures**

```bash
python -m pytest tests/test_runner_self_install.py::test_generate_install_sh_embeds_config tests/test_runner_self_install.py::test_generate_install_ps1_embeds_config -v
```
Expected: FAIL — functions not defined.

- [ ] **Step 3: Add `_generate_runner_install_sh` to runner.py**

```python
def _generate_runner_install_sh(
    *,
    team_lead_url: str,
    profile: str,
    runner_port: int,
) -> str:
    """Generate the bash install script for Linux/macOS operators."""
    return f"""\
#!/usr/bin/env bash
set -euo pipefail

TEAM_LEAD_URL="{team_lead_url}"
PROFILE="{profile}"
RUNNER_PORT="{runner_port}"
RUNNER_ROOT="$HOME/.emerge/runner"

echo "=== Emerge Runner Installer ==="

# Detect China: if pypi.org unreachable, use Tsinghua mirror
USE_CN_MIRROR=0
if ! curl -s --max-time 3 https://pypi.org > /dev/null 2>&1; then
  echo "[CN] Using pip mirror: pypi.tuna.tsinghua.edu.cn"
  USE_CN_MIRROR=1
fi

# Find Python >=3.9
PYTHON=""
for py in python3.12 python3.11 python3.10 python3.9 python3 python; do
  if command -v "$py" &>/dev/null; then
    VER=$($py -c "import sys; print(int(sys.version_info >= (3,9)))" 2>/dev/null || echo 0)
    if [ "$VER" = "1" ]; then PYTHON="$py"; break; fi
  fi
done

if [ -z "$PYTHON" ]; then
  echo "[Install] Python 3.9+ not found, installing..."
  OS="$(uname -s)"
  if [ "$OS" = "Darwin" ]; then
    if command -v brew &>/dev/null; then
      brew install python3
    else
      echo "ERROR: Install Python 3.9+ from https://python.org and re-run." >&2; exit 1
    fi
  else
    if command -v apt-get &>/dev/null; then
      sudo apt-get update -qq && sudo apt-get install -y python3 python3-pip
    elif command -v dnf &>/dev/null; then
      sudo dnf install -y python3
    elif command -v yum &>/dev/null; then
      sudo yum install -y python3
    else
      echo "ERROR: Cannot install Python automatically. Install Python 3.9+ and re-run." >&2; exit 1
    fi
  fi
  PYTHON="python3"
fi

echo "[OK] $($PYTHON --version)"

# Install optional deps (tray icon; ignore failures)
PIP_ARGS=""
if [ "$USE_CN_MIRROR" = "1" ]; then
  PIP_ARGS="--index-url https://pypi.tuna.tsinghua.edu.cn/simple"
fi
$PYTHON -m pip install $PIP_ARGS pystray Pillow 2>/dev/null || true

# Download runner files from daemon
mkdir -p "$RUNNER_ROOT"
curl -fsSL "$TEAM_LEAD_URL/runner-dist/runner.tar.gz" | tar -xzf - -C "$RUNNER_ROOT"

# Write runner-config.json
mkdir -p "$HOME/.emerge"
cat > "$HOME/.emerge/runner-config.json" <<JSON
{{
  "team_lead_url": "$TEAM_LEAD_URL",
  "profile": "$PROFILE",
  "port": $RUNNER_PORT,
  "installed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date)"
}}
JSON

# Configure autostart
OS="$(uname -s)"
if [ "$OS" = "Darwin" ]; then
  PYTHON_BIN="$(command -v $PYTHON)"
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
  launchctl unload "$PLIST" 2>/dev/null || true
  launchctl load "$PLIST"
  echo "[OK] macOS LaunchAgent registered: com.emerge.runner"
else
  PYTHON_BIN="$(command -v $PYTHON)"
  SERVICE_DIR="$HOME/.config/systemd/user"
  mkdir -p "$SERVICE_DIR"
  cat > "$SERVICE_DIR/emerge-runner.service" <<SERVICE
[Unit]
Description=Emerge Runner
After=network.target

[Service]
ExecStart=$PYTHON_BIN $RUNNER_ROOT/scripts/runner_watchdog.py --host 0.0.0.0 --port $RUNNER_PORT
WorkingDirectory=$RUNNER_ROOT
Restart=always
Environment=EMERGE_TEAM_LEAD_URL=$TEAM_LEAD_URL

[Install]
WantedBy=default.target
SERVICE
  systemctl --user daemon-reload
  systemctl --user enable --now emerge-runner
  echo "[OK] systemd user service enabled: emerge-runner"
fi

# Health check
sleep 3
if curl -s --max-time 5 "http://localhost:$RUNNER_PORT/health" 2>/dev/null | grep -q 'true'; then
  echo "[OK] Runner healthy at http://localhost:$RUNNER_PORT"
else
  echo "[Warn] Runner may still be starting. Check: curl http://localhost:$RUNNER_PORT/health"
fi

echo ""
echo "=== Install complete. Runner '$PROFILE' will appear online at $TEAM_LEAD_URL ==="
"""
```

- [ ] **Step 4: Add `_generate_runner_install_ps1` to runner.py**

```python
def _generate_runner_install_ps1(
    *,
    team_lead_url: str,
    profile: str,
    runner_port: int,
) -> str:
    """Generate the PowerShell install script for Windows operators."""
    return f"""\
$ErrorActionPreference = "Stop"

$TEAM_LEAD_URL = "{team_lead_url}"
$PROFILE = "{profile}"
$RUNNER_PORT = {runner_port}
$RUNNER_ROOT = "$env:USERPROFILE\\.emerge\\runner"

Write-Host "=== Emerge Runner Installer ===" -ForegroundColor Cyan

# Detect China
$USE_CN_MIRROR = $false
try {{
    $null = Invoke-WebRequest -Uri "https://pypi.org" -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop
}} catch {{
    Write-Host "[CN] Using pip mirror: pypi.tuna.tsinghua.edu.cn"
    $USE_CN_MIRROR = $true
}}

# Find Python >=3.9
$PYTHON = $null
foreach ($py in @("python", "python3")) {{
    try {{
        $ver = & $py -c "import sys; print(int(sys.version_info >= (3,9)))" 2>$null
        if ($ver.Trim() -eq "1") {{ $PYTHON = $py; break }}
    }} catch {{}}
}}

if (-not $PYTHON) {{
    Write-Host "[Install] Installing Python 3.11..."
    $installed = $false
    try {{
        winget install Python.Python.3.11 --accept-source-agreements --accept-package-agreements -h
        $installed = $true
    }} catch {{}}
    if (-not $installed) {{
        Write-Host "[Fallback] Downloading Python installer from python.org..."
        $pyExe = "$env:TEMP\\python-installer.exe"
        Invoke-WebRequest -Uri "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe" -OutFile $pyExe
        Start-Process $pyExe -ArgumentList "/quiet InstallAllUsers=0 PrependPath=1" -Wait
    }}
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","User") + ";" + $env:Path
    $PYTHON = "python"
}}

Write-Host "[OK] $(& $PYTHON --version)"

# Install optional deps (tray icon)
$pipArgs = @()
if ($USE_CN_MIRROR) {{ $pipArgs = @("--index-url", "https://pypi.tuna.tsinghua.edu.cn/simple") }}
& $PYTHON -m pip install @pipArgs pystray Pillow 2>$null | Out-Null

# Download runner files
New-Item -Force -ItemType Directory -Path $RUNNER_ROOT | Out-Null
$tarPath = "$env:TEMP\\emerge-runner.tar.gz"
Invoke-WebRequest -Uri "$TEAM_LEAD_URL/runner-dist/runner.tar.gz" -OutFile $tarPath
tar -xzf $tarPath -C $RUNNER_ROOT

# Write runner-config.json
New-Item -Force -ItemType Directory -Path "$env:USERPROFILE\\.emerge" | Out-Null
$config = @{{
    team_lead_url = $TEAM_LEAD_URL
    profile = $PROFILE
    port = $RUNNER_PORT
    installed_at = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
}} | ConvertTo-Json -Depth 2
$config | Out-File -FilePath "$env:USERPROFILE\\.emerge\\runner-config.json" -Encoding utf8

# Write VBS launcher (runs hidden, no console window)
$pythonPath = (Get-Command $PYTHON -ErrorAction SilentlyContinue).Source
if (-not $pythonPath) {{ $pythonPath = $PYTHON }}
$vbsPath = "$env:USERPROFILE\\.emerge\\start_emerge_runner.vbs"
$vbs = @"
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "$RUNNER_ROOT"
sh.Run Chr(34) & "$pythonPath" & Chr(34) & " " & Chr(34) & "$RUNNER_ROOT\\scripts\\runner_watchdog.py" & Chr(34) & " --host 0.0.0.0 --port $RUNNER_PORT", 0, False
"@
$vbs | Out-File -FilePath $vbsPath -Encoding ascii

# Register autostart in HKCU Run
$regKey = "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run"
Set-ItemProperty -Path $regKey -Name "EmergeRunner" -Value "wscript.exe `"$vbsPath`""
Write-Host "[OK] Registry autostart registered: EmergeRunner"

# Start now
Start-Process "wscript.exe" -ArgumentList "`"$vbsPath`""

# Health check
Start-Sleep 4
try {{
    $health = Invoke-RestMethod -Uri "http://localhost:$RUNNER_PORT/health" -TimeoutSec 5
    if ($health.ok) {{ Write-Host "[OK] Runner healthy at http://localhost:$RUNNER_PORT" -ForegroundColor Green }}
}} catch {{
    Write-Host "[Warn] Runner may still be starting. Check: curl http://localhost:$RUNNER_PORT/health"
}}

Write-Host ""
Write-Host "=== Install complete. Runner '$PROFILE' will appear online at $TEAM_LEAD_URL ===" -ForegroundColor Cyan
"""
```

- [ ] **Step 5: Run tests**

```bash
python -m pytest tests/test_runner_self_install.py::test_generate_install_sh_embeds_config tests/test_runner_self_install.py::test_generate_install_ps1_embeds_config -v
```
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/admin/runner.py tests/test_runner_self_install.py
git commit -m "feat: add install script generators for bash and PowerShell"
```

---

## Task 4: Daemon HTTP endpoints (`/runner-install.*` and `/runner-dist/runner.tar.gz`)

**Files:**
- Modify: `scripts/daemon_http.py`
- Test: `tests/test_runner_self_install.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_runner_self_install.py`:

```python
import urllib.request, urllib.parse, tarfile, io, threading
from http.server import HTTPServer
from scripts.daemon_http import DaemonHTTPServer


class _MockDaemon:
    """Minimal daemon stub for DaemonHTTPServer."""
    _cockpit_server = None


def _start_test_daemon_http(tmp_path):
    """Start DaemonHTTPServer on a random port, return (server, url, shutdown_fn)."""
    daemon = _MockDaemon()
    dhs = DaemonHTTPServer(daemon=daemon, port=0, pid_path=tmp_path / "test.pid")
    httpd, port = dhs.run_http(block=False)
    url = f"http://127.0.0.1:{port}"
    return dhs, httpd, url


def test_daemon_serves_runner_tarball(tmp_path):
    dhs, httpd, url = _start_test_daemon_http(tmp_path)
    try:
        resp = urllib.request.urlopen(f"{url}/runner-dist/runner.tar.gz", timeout=5)
        data = resp.read()
        assert resp.status == 200
        buf = io.BytesIO(data)
        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            names = tar.getnames()
        assert "scripts/remote_runner.py" in names
        assert "scripts/runner_watchdog.py" in names
    finally:
        httpd.shutdown()


def test_daemon_serves_install_sh(tmp_path):
    dhs, httpd, url = _start_test_daemon_http(tmp_path)
    try:
        resp = urllib.request.urlopen(
            f"{url}/runner-install.sh?profile=testprofile", timeout=5
        )
        text = resp.read().decode()
        assert resp.status == 200
        assert "#!/usr/bin/env bash" in text
        assert "testprofile" in text
        assert "runner.tar.gz" in text
    finally:
        httpd.shutdown()


def test_daemon_serves_install_ps1(tmp_path):
    dhs, httpd, url = _start_test_daemon_http(tmp_path)
    try:
        resp = urllib.request.urlopen(
            f"{url}/runner-install.ps1?profile=testprofile", timeout=5
        )
        text = resp.read().decode()
        assert resp.status == 200
        assert "$TEAM_LEAD_URL" in text
        assert "testprofile" in text
    finally:
        httpd.shutdown()
```

> Note: check how `DaemonHTTPServer.run_http` works first. If `block=False` isn't supported, run in a daemon thread instead:
> ```python
> t = threading.Thread(target=httpd.serve_forever, daemon=True)
> t.start()
> ```

- [ ] **Step 2: Run to verify failures**

```bash
python -m pytest tests/test_runner_self_install.py::test_daemon_serves_runner_tarball -v
```
Expected: FAIL — endpoints not found (404).

- [ ] **Step 3: Check how run_http works and update test if needed**

```bash
grep -n "def run_http\|serve_forever\|block" scripts/daemon_http.py | head -10
```

If `run_http` always blocks, replace test helper with a thread:

```python
import socket

def _start_test_daemon_http(tmp_path):
    daemon = _MockDaemon()
    # Find free port
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    dhs = DaemonHTTPServer(daemon=daemon, port=port, pid_path=tmp_path / "test.pid")
    from http.server import ThreadingHTTPServer
    from scripts.daemon_http import _build_handler  # we'll expose this
    # simpler: use urllib to test after adding endpoints
    return dhs, port
```

> If `DaemonHTTPServer` is complex to instantiate in tests, test only the helper functions (`_build_runner_tarball`, `_generate_runner_install_sh`) rather than HTTP endpoints. The generator tests in Task 3 already cover correctness.

- [ ] **Step 4: Add the three new routes to `do_GET` in `daemon_http.py`**

In `daemon_http.py`, find `do_GET` and add before the `else: self._send_json(404, ...)` line:

```python
elif path == "/runner-dist/runner.tar.gz":
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.admin.runner import _build_runner_tarball
    _plugin_root = Path(__file__).resolve().parents[1]
    data = _build_runner_tarball(_plugin_root)
    self.send_response(200)
    self.send_header("Content-Type", "application/gzip")
    self.send_header("Content-Disposition", 'attachment; filename="runner.tar.gz"')
    self.send_header("Content-Length", str(len(data)))
    self.end_headers()
    self.wfile.write(data)
elif path in ("/runner-install.sh", "/runner-install.ps1"):
    import urllib.parse as _up_ri
    qs_ri = _up_ri.parse_qs(_up_ri.urlparse(self.path).query)
    profile = qs_ri.get("profile", ["default"])[0].strip() or "default"
    runner_port = int(qs_ri.get("port", ["8787"])[0])
    from scripts.admin.runner import (
        _generate_runner_install_sh, _generate_runner_install_ps1,
    )
    from scripts.admin.shared import _detect_lan_ip
    lan_ip = _detect_lan_ip()
    daemon_port = getattr(srv, "_port", 8789)
    team_lead_url = f"http://{lan_ip}:{daemon_port}"
    if path.endswith(".sh"):
        body = _generate_runner_install_sh(
            team_lead_url=team_lead_url, profile=profile, runner_port=runner_port,
        ).encode()
        content_type = "text/x-sh; charset=utf-8"
        filename = "runner-install.sh"
    else:
        body = _generate_runner_install_ps1(
            team_lead_url=team_lead_url, profile=profile, runner_port=runner_port,
        ).encode("utf-8")
        content_type = "text/plain; charset=utf-8"
        filename = "runner-install.ps1"
    self.send_response(200)
    self.send_header("Content-Type", content_type)
    self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
    self.send_header("Content-Length", str(len(body)))
    self.end_headers()
    self.wfile.write(body)
```

Also expose `_port` on `DaemonHTTPServer`. Find `__init__` and add:
```python
self._port = port
```

- [ ] **Step 5: Run tests**

```bash
python -m pytest tests/test_runner_self_install.py -k "tarball or install_sh or install_ps1" -v
```
Expected: PASS.

- [ ] **Step 6: Run full suite**

```bash
python -m pytest tests -q
```
Expected: 635+ passed.

- [ ] **Step 7: Commit**

```bash
git add scripts/daemon_http.py tests/test_runner_self_install.py
git commit -m "feat: daemon serves /runner-install.sh, /runner-install.ps1, /runner-dist/runner.tar.gz"
```

---

## Task 5: `cmd_runner_install_url()` + cockpit API + CLI subcommand

**Files:**
- Modify: `scripts/admin/runner.py`
- Modify: `scripts/admin/cockpit.py`
- Modify: `scripts/repl_admin.py`
- Test: `tests/test_runner_self_install.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_runner_self_install.py`:

```python
from scripts.admin.runner import cmd_runner_install_url


def test_cmd_runner_install_url_returns_both_platforms(monkeypatch):
    monkeypatch.setattr(
        "scripts.admin.runner._detect_lan_ip", lambda: "10.0.0.1"
    )
    result = cmd_runner_install_url(profile="myrunner", daemon_port=8789)
    assert result["ok"] is True
    assert "10.0.0.1" in result["bash"]
    assert "myrunner" in result["bash"]
    assert "curl" in result["bash"]
    assert "10.0.0.1" in result["powershell"]
    assert "myrunner" in result["powershell"]
    assert "irm" in result["powershell"]
    assert result["team_lead_url"] == "http://10.0.0.1:8789"
```

- [ ] **Step 2: Run to verify failure**

```bash
python -m pytest tests/test_runner_self_install.py::test_cmd_runner_install_url_returns_both_platforms -v
```
Expected: FAIL.

- [ ] **Step 3: Add `cmd_runner_install_url()` to runner.py**

```python
from scripts.admin.shared import _detect_lan_ip  # add to existing import line


def cmd_runner_install_url(
    *,
    profile: str = "default",
    runner_port: int = 8787,
    daemon_port: int = 8789,
) -> dict:
    """Return install URLs for both platforms. Called by CLI and cockpit API."""
    lan_ip = _detect_lan_ip()
    team_lead_url = f"http://{lan_ip}:{daemon_port}"
    base_url = f"{team_lead_url}/runner-install"
    bash_cmd = f'curl -fsSL "{base_url}.sh?profile={profile}" | bash'
    ps_cmd = f'irm "{base_url}.ps1?profile={profile}" | iex'
    return {
        "ok": True,
        "profile": profile,
        "team_lead_url": team_lead_url,
        "bash": bash_cmd,
        "powershell": ps_cmd,
    }
```

- [ ] **Step 4: Add import to runner.py's existing import from shared**

In `scripts/admin/runner.py`, find:
```python
from scripts.admin.shared import _local_plugin_version
```
Change to:
```python
from scripts.admin.shared import _local_plugin_version, _detect_lan_ip
```

- [ ] **Step 5: Run test**

```bash
python -m pytest tests/test_runner_self_install.py::test_cmd_runner_install_url_returns_both_platforms -v
```
Expected: PASS.

- [ ] **Step 6: Add `/api/control-plane/runner-install-url` to cockpit.py**

In `scripts/admin/cockpit.py`, add import at top:
```python
from scripts.admin.runner import cmd_runner_install_url
```

In `do_GET`, add after the `/api/control-plane/monitors` block:
```python
elif path == "/api/control-plane/runner-install-url":
    qs_riu = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    profile = qs_riu.get("profile", ["default"])[0].strip() or "default"
    self._json(cmd_runner_install_url(profile=profile))
```

- [ ] **Step 7: Add `runner-install-url` subcommand to repl_admin.py**

In `scripts/repl_admin.py`:

1. Add import:
```python
from scripts.admin.runner import (
    ...
    cmd_runner_install_url,
)
```

2. Add to `choices` list in `parser.add_argument("command", choices=[...])`:
```python
"runner-install-url",
```

3. Add handler before `elif args.command == "runner-deploy":`:
```python
elif args.command == "runner-install-url":
    out = cmd_runner_install_url(
        profile=str(args.target_profile) or "default",
        runner_port=int(args.runner_port),
    )
    if args.pretty and out.get("ok"):
        print(f"Linux/macOS:\n  {out['bash']}\n")
        print(f"Windows PowerShell:\n  {out['powershell']}")
        return
```

- [ ] **Step 8: Run full suite**

```bash
python -m pytest tests -q
```
Expected: 635+ passed.

- [ ] **Step 9: Commit**

```bash
git add scripts/admin/runner.py scripts/admin/cockpit.py scripts/repl_admin.py tests/test_runner_self_install.py
git commit -m "feat: cmd_runner_install_url, cockpit API, CLI runner-install-url"
```

---

## Task 6: Watchdog reads `~/.emerge/runner-config.json`

**Files:**
- Modify: `scripts/runner_watchdog.py`
- Test: `tests/test_runner_self_install.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_runner_self_install.py`:

```python
import json, os
from unittest.mock import patch, MagicMock
from scripts import runner_watchdog


def test_watchdog_injects_team_lead_url_from_config(tmp_path, monkeypatch):
    config = {
        "team_lead_url": "http://192.168.1.50:8789",
        "profile": "myrunner",
        "port": 8787,
    }
    config_path = tmp_path / "runner-config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    monkeypatch.setattr(runner_watchdog, "_CONFIG_PATH", config_path)

    launched_envs = []

    def fake_popen(cmd, **kwargs):
        launched_envs.append(kwargs.get("env", {}))
        m = MagicMock()
        m.pid = 42
        return m

    with patch("subprocess.Popen", side_effect=fake_popen):
        runner_watchdog._start_runner("0.0.0.0", 8787, "python3")

    assert launched_envs, "Popen should have been called"
    assert launched_envs[0].get("EMERGE_TEAM_LEAD_URL") == "http://192.168.1.50:8789"


def test_watchdog_works_without_config(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_watchdog, "_CONFIG_PATH", tmp_path / "missing.json")

    launched_envs = []

    def fake_popen(cmd, **kwargs):
        launched_envs.append(kwargs.get("env", {}))
        m = MagicMock()
        m.pid = 99
        return m

    with patch("subprocess.Popen", side_effect=fake_popen):
        runner_watchdog._start_runner("0.0.0.0", 8787, "python3")

    assert launched_envs, "Popen should have been called even without config"
    # No EMERGE_TEAM_LEAD_URL injected if config missing — that's fine
```

- [ ] **Step 2: Run to verify failures**

```bash
python -m pytest tests/test_runner_self_install.py::test_watchdog_injects_team_lead_url_from_config tests/test_runner_self_install.py::test_watchdog_works_without_config -v
```
Expected: FAIL — `_CONFIG_PATH` not defined in runner_watchdog.

- [ ] **Step 3: Modify `runner_watchdog.py`**

Add module-level constant and modify `_start_runner`:

```python
import json  # add to existing imports

_CONFIG_PATH = Path.home() / ".emerge" / "runner-config.json"


def _load_team_lead_url() -> str:
    """Read team_lead_url from runner-config.json if present."""
    try:
        data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        return str(data.get("team_lead_url", "") or "")
    except Exception:
        return ""


def _start_runner(host: str, port: int, python: str) -> subprocess.Popen:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    team_lead_url = _load_team_lead_url()
    if team_lead_url:
        env["EMERGE_TEAM_LEAD_URL"] = team_lead_url
    return subprocess.Popen(
        [python, str(ROOT / "scripts" / "remote_runner.py"), "--host", host, "--port", str(port)],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/test_runner_self_install.py::test_watchdog_injects_team_lead_url_from_config tests/test_runner_self_install.py::test_watchdog_works_without_config -v
```
Expected: PASS.

- [ ] **Step 5: Run full suite**

```bash
python -m pytest tests -q
```
Expected: 635+ passed.

- [ ] **Step 6: Commit**

```bash
git add scripts/runner_watchdog.py tests/test_runner_self_install.py
git commit -m "feat: watchdog reads runner-config.json and injects EMERGE_TEAM_LEAD_URL"
```

---

## Task 7: Cockpit "Add Runner" panel

**Files:**
- Modify: `scripts/cockpit_shell.html`

This task has no automated test (UI-only). Verify manually.

- [ ] **Step 1: Replace `renderMonitorsTab()` in cockpit_shell.html**

Find the current `renderMonitorsTab()` function (around line 2024) and replace it entirely:

```javascript
async function renderMonitorsTab() {
  const panel = document.getElementById('main-panel');
  panel.innerHTML = '<div style="padding:16px;color:#8b949e">Loading monitor state…</div>';
  try {
    const resp = await fetch('/api/control-plane/monitors');
    const data = await resp.json();
    const runners = data.runners || [];

    let html = '<div style="padding:16px">';

    // "Add Runner" panel — prominent when no runners, collapsed otherwise
    const addRunnerExpanded = runners.length === 0;
    const addRunnerId = 'add-runner-panel';
    if (addRunnerExpanded) {
      html += _renderAddRunnerPanel(true);
    } else {
      html += `<div style="margin-bottom:16px">`;
      html += `<span style="font-size:11px;color:#8b949e;cursor:pointer;text-decoration:underline" onclick="document.getElementById('${addRunnerId}').style.display=document.getElementById('${addRunnerId}').style.display==='none'?'block':'none'">＋ Add another runner</span>`;
      html += `<div id="${addRunnerId}" style="display:none;margin-top:12px">${_renderAddRunnerPanel(false)}</div>`;
      html += `</div>`;
    }

    if (!runners.length) {
      html += '</div>';
      panel.innerHTML = html;
      _initAddRunnerPanel();
      return;
    }

    html += `<div style="margin-bottom:12px;font-size:12px;color:#8b949e">${runners.length} runner(s) connected</div>`;
    html += '<table style="width:100%;border-collapse:collapse;font-size:12px">';
    html += '<thead><tr>';
    for (const h of ['Runner', 'Machine', 'Connected', 'Last Event', 'Last Alert']) {
      html += `<th style="text-align:left;padding:6px 10px;background:#161b22;border-bottom:1px solid #30363d;color:#8b949e;font-size:10px;text-transform:uppercase">${h}</th>`;
    }
    html += '</tr></thead><tbody>';
    const now = Date.now();
    for (const r of runners) {
      const connectedSec = r.connected_at_ms ? Math.round((now - r.connected_at_ms) / 1000) : 0;
      const lastEventSec = r.last_event_ts_ms ? Math.round((now - r.last_event_ts_ms) / 1000) : null;
      const alertBadge = r.last_alert
        ? `<span style="background:#0d2000;color:#3fb950;border:1px solid #2a4a2a;padding:1px 6px;border-radius:3px;font-size:10px">${escHtml(r.last_alert.stage)}: ${escHtml(r.last_alert.intent_signature || '')}</span>`
        : '<span style="color:#8b949e">none</span>';
      html += `<tr>
        <td style="padding:6px 10px;border-bottom:1px solid #21262d">
          <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${r.connected ? '#3fb950' : '#6e7681'};margin-right:6px"></span>
          ${escHtml(r.runner_profile)}
        </td>
        <td style="padding:6px 10px;border-bottom:1px solid #21262d">${escHtml(r.machine_id || '')}</td>
        <td style="padding:6px 10px;border-bottom:1px solid #21262d">${connectedSec}s ago</td>
        <td style="padding:6px 10px;border-bottom:1px solid #21262d">${lastEventSec !== null ? lastEventSec + 's ago' : '—'}</td>
        <td style="padding:6px 10px;border-bottom:1px solid #21262d">${alertBadge}</td>
      </tr>`;
    }
    html += '</tbody></table></div>';
    panel.innerHTML = html;
    _initAddRunnerPanel();
  } catch (e) {
    panel.innerHTML = `<div style="padding:16px;color:#f85149">Error loading monitors: ${escHtml(String(e))}</div>`;
  }
}

function _renderAddRunnerPanel(expanded) {
  return `
<div style="border:1px solid #30363d;border-radius:6px;padding:16px;margin-bottom:16px;background:#0d1117">
  <div style="font-size:13px;font-weight:600;color:#c9d1d9;margin-bottom:12px">Add Runner</div>
  <div style="margin-bottom:10px">
    <label style="font-size:11px;color:#8b949e;display:block;margin-bottom:4px">Profile name</label>
    <input id="runner-profile-input" type="text" value="default"
      style="background:#161b22;border:1px solid #30363d;border-radius:4px;color:#c9d1d9;font-size:12px;padding:5px 8px;width:200px"
      oninput="_fetchInstallUrls()" />
  </div>
  <div id="install-url-bash" style="margin-bottom:10px">
    <div style="font-size:11px;color:#8b949e;margin-bottom:4px">Linux / macOS</div>
    <div style="display:flex;align-items:center;gap:8px">
      <code id="bash-cmd" style="background:#161b22;border:1px solid #30363d;border-radius:4px;padding:6px 10px;font-size:11px;color:#58a6ff;flex:1;word-break:break-all">Loading…</code>
      <button onclick="_copyCmd('bash-cmd')" style="background:#21262d;border:1px solid #30363d;border-radius:4px;color:#c9d1d9;font-size:11px;padding:5px 10px;cursor:pointer">Copy</button>
    </div>
  </div>
  <div id="install-url-ps1" style="margin-bottom:10px">
    <div style="font-size:11px;color:#8b949e;margin-bottom:4px">Windows PowerShell</div>
    <div style="display:flex;align-items:center;gap:8px">
      <code id="ps1-cmd" style="background:#161b22;border:1px solid #30363d;border-radius:4px;padding:6px 10px;font-size:11px;color:#58a6ff;flex:1;word-break:break-all">Loading…</code>
      <button onclick="_copyCmd('ps1-cmd')" style="background:#21262d;border:1px solid #30363d;border-radius:4px;color:#c9d1d9;font-size:11px;padding:5px 10px;cursor:pointer">Copy</button>
    </div>
  </div>
  <div id="runner-connect-status" style="font-size:11px;color:#8b949e">Runner will appear above once it connects.</div>
</div>`;
}

function _copyCmd(elemId) {
  const text = document.getElementById(elemId)?.textContent || '';
  navigator.clipboard.writeText(text).then(() => {
    const btn = document.querySelector(`[onclick="_copyCmd('${elemId}')"]`);
    if (btn) { btn.textContent = 'Copied!'; setTimeout(() => { btn.textContent = 'Copy'; }, 1500); }
  });
}

let _installUrlFetchTimer = null;
function _fetchInstallUrls() {
  clearTimeout(_installUrlFetchTimer);
  _installUrlFetchTimer = setTimeout(async () => {
    const profile = document.getElementById('runner-profile-input')?.value.trim() || 'default';
    try {
      const r = await fetch(`/api/control-plane/runner-install-url?profile=${encodeURIComponent(profile)}`);
      const d = await r.json();
      if (d.ok) {
        const bashEl = document.getElementById('bash-cmd');
        const ps1El = document.getElementById('ps1-cmd');
        if (bashEl) bashEl.textContent = d.bash;
        if (ps1El) ps1El.textContent = d.powershell;
      }
    } catch (e) {}
  }, 300);
}

function _initAddRunnerPanel() {
  _fetchInstallUrls();
}
```

- [ ] **Step 2: Manually verify in browser**

```bash
python3 scripts/repl_admin.py serve --open --port 0
```

Open the Monitors tab. Verify:
- "Add Runner" panel is visible
- Profile input starts as "default"
- Bash and PowerShell commands appear after ~300ms
- Copy button copies to clipboard
- Changing profile name updates commands

- [ ] **Step 3: Commit**

```bash
git add scripts/cockpit_shell.html
git commit -m "feat: cockpit Monitors tab Add Runner panel with copy-ready install commands"
```

---

## Task 8: Delete `runner-bootstrap` + update docs/skills

**Files:**
- Modify: `scripts/admin/runner.py` (delete `cmd_runner_bootstrap` and SSH helpers)
- Modify: `scripts/repl_admin.py` (delete bootstrap subcommand + imports)
- Modify: `tests/test_repl_admin.py` (delete bootstrap tests)
- Modify: `skills/remote-runner-dev/SKILL.md`
- Modify: `skills/initializing-vertical-flywheel/SKILL.md`
- Modify: `commands/init.md`

- [ ] **Step 1: Delete `cmd_runner_bootstrap` and its helpers from runner.py**

Delete these functions from `scripts/admin/runner.py`:
- `cmd_runner_bootstrap` (lines ~333–539 — the full function)
- `_run_checked` (SSH helper, only used by bootstrap)
- `_remote_root_expr` (SSH path helper)
- `_remote_root_expr_win` (SSH path helper)
- `_read_remote_plugin_version` (SSH helper)
- `_probe_runner_health` (only used by bootstrap)

Keep: `cmd_runner_deploy` (uses runner HTTP, not SSH), all `cmd_runner_config_*`, `cmd_runner_status`, `render_runner_status_pretty`.

- [ ] **Step 2: Update repl_admin.py**

1. Remove from import block:
```python
cmd_runner_bootstrap,
_run_checked,
_remote_root_expr,
_remote_root_expr_win,
_read_remote_plugin_version,
_probe_runner_health,
```

2. Remove `"runner-bootstrap"` from the `choices` list.

3. Remove the `elif args.command == "runner-bootstrap":` block.

4. Remove bootstrap-specific CLI args:
```python
parser.add_argument("--ssh-target", ...)
parser.add_argument("--remote-plugin-root", ...)
parser.add_argument("--runner-host", ...)
parser.add_argument("--python-bin", ...)
parser.add_argument("--skip-deploy", ...)
parser.add_argument("--windows", ...)
```
(keep `--runner-port` and `--team-lead-url` — used by runner-install-url)

- [ ] **Step 3: Delete bootstrap tests from test_repl_admin.py**

Delete these test functions:
- `test_runner_bootstrap_requires_target_profile`
- `test_runner_bootstrap_shell_commands_quote_remote_root`
- `test_runner_bootstrap_rejects_invalid_port`
- `test_runner_bootstrap_sets_route_and_reports_health`
- Any other tests that reference `cmd_runner_bootstrap`, `_run_checked`, `_probe_runner_health`, `_remote_root_expr`

Also remove the imports of these deleted names from the test file.

- [ ] **Step 4: Run full suite**

```bash
python -m pytest tests -q
```
Expected: still passes (count may decrease by ~4 deleted tests).

- [ ] **Step 5: Update `skills/remote-runner-dev/SKILL.md`**

Replace the existing `## Runner Bootstrap (first time)` section with:

```markdown
## Runner Setup (first time)

Operators install via a single command generated by the cockpit or CLI.

**From the cockpit:** Open the Monitors tab → "Add Runner" panel → enter profile name → copy the command for the operator's OS.

**From CC CLI:**
```bash
python3 scripts/repl_admin.py runner-install-url --target-profile <profile> --pretty
# Prints:
# Linux/macOS: curl -fsSL "http://192.168.1.x:8789/runner-install.sh?profile=<profile>" | bash
# Windows:     irm  "http://192.168.1.x:8789/runner-install.ps1?profile=<profile>" | iex
```

Send the appropriate command to the operator. The script:
1. Detects OS and installs Python 3.9+ if missing
2. Detects China and uses Tsinghua pip mirror if needed
3. Downloads runner files from the daemon
4. Writes `~/.emerge/runner-config.json` with `team_lead_url` and `profile`
5. Configures platform autostart (systemd / launchd / Windows registry)
6. Starts the runner and confirms it's healthy
```

Also remove the `## Platform-Specific Setup Notes` → `### Windows — interactive session required for GUI/COM` registry/VBS manual steps (those are now automated by the install script) and the `### Linux/Mac — standard nohup launch` manual nohup steps.

Keep the `## Development Workflow` section (runner-deploy is unchanged).

- [ ] **Step 6: Update `skills/initializing-vertical-flywheel/SKILL.md`**

In the `## Remote Runner Bootstrap (When Needed)` section, replace:

```markdown
1. Execute automated bootstrap from local plugin root:
   - `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" runner-bootstrap --ssh-target "<user@host>" --target-profile "<target_profile>" --runner-url "http://<target>:8787"`
2. `runner-bootstrap` performs remote deploy/start/check/persist automatically.
```

with:

```markdown
1. Generate install URL and send to operator:
   - `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" runner-install-url --target-profile "<target_profile>" --pretty`
   - Or open cockpit Monitors tab → Add Runner panel → copy command for operator's OS.
2. Operator runs the command on their machine (installs Python if needed, configures autostart).
```

- [ ] **Step 7: Update `commands/init.md`**

Find the runner-bootstrap step and replace with:

```markdown
- Generate runner install URL:
  `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repl_admin.py" runner-install-url --target-profile "<target_profile>" --pretty`
  Send the printed command to the operator. They run it on their machine.
  Or use the cockpit Monitors tab → Add Runner panel.
```

- [ ] **Step 8: Run full suite**

```bash
python -m pytest tests -q
```
Expected: all tests pass.

- [ ] **Step 9: Commit**

```bash
git add scripts/admin/runner.py scripts/repl_admin.py tests/test_repl_admin.py \
        skills/remote-runner-dev/SKILL.md \
        skills/initializing-vertical-flywheel/SKILL.md \
        commands/init.md
git commit -m "feat: delete runner-bootstrap; update skills and commands to self-install flow"
```

---

## Self-Review

**Spec coverage:**
- ✅ `/runner-install.sh` and `/runner-install.ps1` with embedded config → Tasks 3 + 4
- ✅ `/runner-dist/runner.tar.gz` tarball → Task 2 + 4
- ✅ LAN IP detection → Task 1
- ✅ China mirror detection in scripts → Task 3
- ✅ Python install per platform → Task 3
- ✅ Autostart: systemd / launchd / Windows registry → Task 3
- ✅ `~/.emerge/runner-config.json` written by install script, read by watchdog → Task 6
- ✅ `cmd_runner_install_url` + CLI + cockpit API → Task 5
- ✅ Cockpit "Add Runner" panel → Task 7
- ✅ Delete runner-bootstrap → Task 8
- ✅ Skills + commands updated → Task 8

**Placeholder scan:** No TBD/TODO in code blocks. All method signatures used consistently (`_generate_runner_install_sh`, `_generate_runner_install_ps1`, `_build_runner_tarball`, `cmd_runner_install_url`, `_detect_lan_ip`).

**Type consistency:** `team_lead_url: str`, `profile: str`, `runner_port: int`, `daemon_port: int` — consistent across Tasks 3, 4, 5.
