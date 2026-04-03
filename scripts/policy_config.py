from __future__ import annotations

from pathlib import Path

PROMOTE_MIN_ATTEMPTS = 20
PROMOTE_MIN_SUCCESS_RATE = 0.95
PROMOTE_MIN_VERIFY_RATE = 0.98
PROMOTE_MAX_HUMAN_FIX_RATE = 0.05

STABLE_MIN_ATTEMPTS = 40
STABLE_MIN_SUCCESS_RATE = 0.97
STABLE_MIN_VERIFY_RATE = 0.99

ROLLBACK_CONSECUTIVE_FAILURES = 2
WINDOW_SIZE = 20


def default_emerge_home() -> Path:
    return Path.home() / ".emerge"


def default_repl_root() -> Path:
    return default_emerge_home() / "repl"


def default_hook_state_root() -> Path:
    return default_emerge_home() / "hook-state"

