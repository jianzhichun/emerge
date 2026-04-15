"""Shared path resolvers for admin sub-modules.

Only functions used by two or more of control_plane / pipeline / api live here.
Module-specific helpers stay in their own module.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.policy_config import default_exec_root  # noqa: E402


def _resolve_state_root() -> Path:
    """Return the daemon state root directory (EMERGE_STATE_ROOT or default)."""
    return Path(
        os.environ.get("EMERGE_STATE_ROOT", str(default_exec_root()))
    ).expanduser().resolve()


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


def _resolve_connector_root() -> Path:
    """Return connector root: EMERGE_CONNECTOR_ROOT env var if set, else ~/.emerge/connectors."""
    from scripts.pipeline_engine import _USER_CONNECTOR_ROOT
    env_root = os.environ.get("EMERGE_CONNECTOR_ROOT", "").strip()
    if env_root:
        return Path(env_root).expanduser().resolve()
    return _USER_CONNECTOR_ROOT
