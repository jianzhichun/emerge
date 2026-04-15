from __future__ import annotations

import io
import json
import sys
import tarfile
import urllib.request
from pathlib import Path
import urllib.parse
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.admin.shared import _detect_lan_ip
from scripts.admin.runner import (
    _RUNNER_FILES,
    _build_runner_tarball,
    _generate_runner_install_ps1,
    _generate_runner_install_sh,
    cmd_runner_install_url,
)


def test_detect_lan_ip_returns_ipv4_dotted():
    ip = _detect_lan_ip()
    assert ip, "should return a non-empty string"
    parts = ip.split(".")
    assert len(parts) == 4, f"expected IPv4 dotted form, got {ip!r}"
    for p in parts:
        assert 0 <= int(p) <= 255


def test_detect_lan_ip_cached():
    ip1 = _detect_lan_ip()
    ip2 = _detect_lan_ip()
    assert ip1 == ip2


def test_runner_files_constant_all_exist():
    for rel in _RUNNER_FILES:
        p = ROOT / rel
        assert p.exists(), f"missing: {rel}"


def test_build_runner_tarball_contains_all_files():
    data = _build_runner_tarball(ROOT)
    assert len(data) > 100
    buf = io.BytesIO(data)
    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        names = tar.getnames()
    for rel in _RUNNER_FILES:
        assert rel in names, f"missing from tarball: {rel}"


def test_generate_install_sh_embeds_config():
    script = _generate_runner_install_sh(
        team_lead_url="http://10.0.0.1:8789",
        runner_port=8787,
    )
    assert 'TEAM_LEAD_URL="http://10.0.0.1:8789"' in script
    # Profile is auto-detected from hostname at install time, not hardcoded
    assert "hostname" in script
    assert "EMERGE_PROFILE" in script
    assert 'RUNNER_PORT="8787"' in script
    assert "#!/usr/bin/env bash" in script
    assert "pypi.org" in script
    assert "launchctl" in script
    assert "systemctl" in script
    assert "runner.tar.gz" in script


def test_generate_install_ps1_embeds_config():
    script = _generate_runner_install_ps1(
        team_lead_url="http://10.0.0.1:8789",
        runner_port=8787,
    )
    # URL is single-quoted in PS1 (no interpolation risk)
    assert "$TEAM_LEAD_URL = 'http://10.0.0.1:8789'" in script
    # Profile is auto-detected from COMPUTERNAME at install time
    assert "COMPUTERNAME" in script
    assert "EMERGE_PROFILE" in script
    assert "$RUNNER_PORT = 8787" in script
    assert "EmergeRunner" in script
    assert "runner.tar.gz" in script
    assert "winget" in script


def test_generate_install_ps1_url_single_quote_escaped():
    """Single quotes in the URL are doubled — no injection possible."""
    script = _generate_runner_install_ps1(
        team_lead_url="http://host:8789/path'with'quotes",
        runner_port=8787,
    )
    # Should contain '' (doubled single quote), not raw ' that would end the PS1 string
    assert "path''with''quotes" in script
    assert "$TEAM_LEAD_URL = 'http://host:8789/path''with''quotes'" in script


def test_detect_lan_ip_raises_when_no_routable_interface(monkeypatch):
    """_detect_lan_ip raises OSError rather than silently returning loopback."""
    import scripts.admin.shared as shared_mod
    monkeypatch.setattr(shared_mod, "_lan_ip_cache", "")

    def bad_connect(*a, **kw):
        raise OSError("network unreachable")

    def loopback_hostname(*a, **kw):
        return "127.0.0.1"

    with patch("scripts.admin.shared._socket.socket") as mock_sock:
        mock_sock.return_value.__enter__.return_value.connect.side_effect = bad_connect
        with patch("scripts.admin.shared._socket.gethostbyname", side_effect=loopback_hostname):
            with patch("scripts.admin.shared._socket.gethostname", return_value="localhost"):
                with pytest.raises(OSError, match="No routable LAN interface"):
                    shared_mod._detect_lan_ip()


def test_cmd_runner_install_url_returns_both_platforms(monkeypatch):
    monkeypatch.setattr("scripts.admin.runner._detect_lan_ip", lambda: "10.0.0.1")
    result = cmd_runner_install_url(daemon_port=8789, runner_port=8787)
    assert result["ok"] is True
    assert "10.0.0.1" in result["bash"]
    assert "curl" in result["bash"]
    assert "10.0.0.1" in result["powershell"]
    assert "irm" in result["powershell"]
    assert result["team_lead_url"] == "http://10.0.0.1:8789"


def test_daemon_serves_runner_tarball(tmp_path):
    from scripts.daemon_http import DaemonHTTPServer

    class _StubDaemon:
        def handle_jsonrpc(self, req):
            return {"jsonrpc": "2.0", "id": req.get("id"), "result": {}}

    srv = DaemonHTTPServer(
        daemon=_StubDaemon(),
        port=0,
        pid_path=tmp_path / "d.pid",
        event_root=tmp_path / "operator-events",
        state_root=tmp_path / "repl",
    )
    srv.start()
    try:
        port = srv.port
        req = urllib.request.Request(f"http://127.0.0.1:{port}/runner-dist/runner.tar.gz")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = resp.read()
        assert resp.status == 200
        buf = io.BytesIO(data)
        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            names = tar.getnames()
        assert "scripts/remote_runner.py" in names
    finally:
        srv.stop()


def test_daemon_serves_install_scripts(tmp_path, monkeypatch):
    from scripts.daemon_http import DaemonHTTPServer

    monkeypatch.setattr("scripts.admin.shared._detect_lan_ip", lambda: "192.168.1.10")

    class _StubDaemon:
        def handle_jsonrpc(self, req):
            return {"jsonrpc": "2.0", "id": req.get("id"), "result": {}}

    srv = DaemonHTTPServer(
        daemon=_StubDaemon(),
        port=0,
        pid_path=tmp_path / "d.pid",
        event_root=tmp_path / "operator-events",
        state_root=tmp_path / "repl",
    )
    srv.start()
    try:
        port = srv.port
        sh_req = urllib.request.Request(
            f"http://127.0.0.1:{port}/runner-install.sh?port=8787"
        )
        with urllib.request.urlopen(sh_req, timeout=5) as resp:
            text = resp.read().decode()
        assert resp.status == 200
        assert "#!/usr/bin/env bash" in text
        assert "hostname" in text
        assert "runner.tar.gz" in text

        ps_req = urllib.request.Request(
            f"http://127.0.0.1:{port}/runner-install.ps1"
        )
        with urllib.request.urlopen(ps_req, timeout=5) as resp:
            ps1 = resp.read().decode()
        assert "$TEAM_LEAD_URL" in ps1
        assert "COMPUTERNAME" in ps1
    finally:
        srv.stop()


def test_watchdog_injects_team_lead_url_from_config(tmp_path, monkeypatch):
    import scripts.runner_watchdog as rw

    config = {
        "team_lead_url": "http://192.168.1.50:8789",
        "runner_profile": "myrunner",
        "port": 8787,
    }
    config_path = tmp_path / "runner-config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(rw, "_CONFIG_PATH", config_path)

    launched_envs: list[dict] = []

    def fake_popen(cmd, **kwargs):
        launched_envs.append(dict(kwargs.get("env") or {}))
        m = MagicMock()
        m.pid = 42
        return m

    with patch("subprocess.Popen", side_effect=fake_popen):
        rw._start_runner("0.0.0.0", 8787, "python3")

    assert launched_envs
    assert launched_envs[0].get("EMERGE_TEAM_LEAD_URL") == "http://192.168.1.50:8789"


def test_watchdog_ok_without_config(tmp_path, monkeypatch):
    import scripts.runner_watchdog as rw

    monkeypatch.setattr(rw, "_CONFIG_PATH", tmp_path / "missing.json")

    launched_envs: list[dict] = []

    def fake_popen(cmd, **kwargs):
        launched_envs.append(dict(kwargs.get("env") or {}))
        m = MagicMock()
        m.pid = 99
        return m

    with patch("subprocess.Popen", side_effect=fake_popen):
        rw._start_runner("0.0.0.0", 8787, "python3")

    assert launched_envs
    assert not launched_envs[0].get("EMERGE_TEAM_LEAD_URL")


def test_cockpit_runner_profiles_endpoint(tmp_path, monkeypatch):
    """GET /api/control-plane/runner-profiles returns list of known runner profiles."""
    import time
    from scripts.repl_admin import CockpitHTTPServer, _StandaloneDaemonStub

    monkeypatch.setenv("EMERGE_REPL_ROOT", str(tmp_path))
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path))

    # Write a runner-monitor-state.json with one known runner
    monitor_state = {
        "runners": [
            {"runner_profile": "prod-runner", "connected": True, "connected_at_ms": 0}
        ],
        "team_active": True,
    }
    (tmp_path / "runner-monitor-state.json").write_text(
        json.dumps(monitor_state), encoding="utf-8"
    )

    cockpit = CockpitHTTPServer(daemon=_StandaloneDaemonStub(), port=0, repl_root=tmp_path)
    url = cockpit.start()
    time.sleep(0.1)
    try:
        port = int(url.rsplit(":", 1)[-1])
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/control-plane/runner-profiles"
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        assert "profiles" in data
        assert isinstance(data["profiles"], list)
        assert "prod-runner" in data["profiles"]
    finally:
        cockpit.stop()
