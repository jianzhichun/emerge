"""Shared path resolvers for admin sub-modules.

Only functions used by two or more of control_plane / pipeline / api live here.
Module-specific helpers stay in their own module.
"""
from __future__ import annotations

import os
import socket as _socket
import sys
from pathlib import Path

_lan_ip_cache: str = ""

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import json as _json

from scripts.policy_config import default_state_root  # noqa: E402

_SHARED_ROOT = Path(__file__).resolve().parents[2]


def _local_plugin_version() -> str:
    """Return the plugin version from .claude-plugin/plugin.json."""
    manifest = _SHARED_ROOT / ".claude-plugin" / "plugin.json"
    data = _json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return ""
    return str(data.get("version", "") or "").strip()


def _resolve_state_root() -> Path:
    """Return the daemon state root directory (EMERGE_STATE_ROOT or default)."""
    return Path(
        os.environ.get("EMERGE_STATE_ROOT", str(default_state_root()))
    ).expanduser().resolve()


def _detect_lan_ip() -> str:
    """Return the machine's outgoing LAN IPv4 address (cached per process).

    Uses a UDP connect trick (no packets sent) to find which interface the OS
    would use for outgoing traffic. Falls back to hostname resolution.
    Raises OSError if no non-loopback address is found — callers must surface
    this as an actionable error rather than generating a broken install URL.
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
    raise OSError(
        "No routable LAN interface detected. "
        "Connect to a network or set EMERGE_DAEMON_BIND to the machine's LAN IP."
    )


def _resolve_connector_root() -> Path:
    """Return connector root: EMERGE_CONNECTOR_ROOT env var if set, else ~/.emerge/connectors."""
    from scripts.pipeline_engine import _USER_CONNECTOR_ROOT
    env_root = os.environ.get("EMERGE_CONNECTOR_ROOT", "").strip()
    if env_root:
        return Path(env_root).expanduser().resolve()
    return _USER_CONNECTOR_ROOT
