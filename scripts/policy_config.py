from __future__ import annotations

import re
from hashlib import sha1
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


def stable_token(raw: str, *, max_prefix: int = 48) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", raw).strip("._-") or "token"
    digest = sha1(raw.encode("utf-8")).hexdigest()[:10]
    return f"{safe[:max_prefix]}-{digest}"


def derive_session_id(explicit: str | None, project_root: Path) -> str:
    if explicit:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", explicit) and explicit not in {
            ".",
            "..",
        }:
            return explicit
        return stable_token(explicit, max_prefix=56)
    project_name = project_root.name or "project"
    project_fingerprint = str(project_root.resolve())
    base = f"{project_name}-{sha1(project_fingerprint.encode('utf-8')).hexdigest()[:10]}"
    return stable_token(base, max_prefix=56)


def derive_profile_token(profile: str) -> str:
    return stable_token(profile or "default", max_prefix=56)

