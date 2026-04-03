from __future__ import annotations

import json as _json
import os as _os
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


_SETTINGS_CACHE: dict | None = None

_DEFAULTS: dict = {
    "policy": {
        "promote_min_attempts": PROMOTE_MIN_ATTEMPTS,
        "promote_min_success_rate": PROMOTE_MIN_SUCCESS_RATE,
        "promote_min_verify_rate": PROMOTE_MIN_VERIFY_RATE,
        "promote_max_human_fix_rate": PROMOTE_MAX_HUMAN_FIX_RATE,
        "rollback_consecutive_failures": ROLLBACK_CONSECUTIVE_FAILURES,
        "stable_min_attempts": STABLE_MIN_ATTEMPTS,
        "stable_min_success_rate": STABLE_MIN_SUCCESS_RATE,
        "stable_min_verify_rate": STABLE_MIN_VERIFY_RATE,
        "window_size": WINDOW_SIZE,
    },
    "connector_root": "~/.emerge/connectors",
    "runner": {
        "timeout_s": 30,
        "retry_max_attempts": 3,
        "retry_base_delay_s": 0.5,
        "retry_max_delay_s": 10.0,
    },
    "metrics_sink": "local_jsonl",
}

_POLICY_INT_KEYS = {
    "promote_min_attempts", "rollback_consecutive_failures",
    "stable_min_attempts", "window_size",
}
_POLICY_FLOAT_KEYS = {
    "promote_min_success_rate", "promote_min_verify_rate",
    "promote_max_human_fix_rate", "stable_min_success_rate", "stable_min_verify_rate",
}


def default_settings_path() -> Path:
    raw = _os.environ.get("EMERGE_SETTINGS_PATH", "").strip()
    if raw:
        return Path(raw).expanduser()
    return default_emerge_home() / "settings.json"


def _reset_settings_cache() -> None:
    global _SETTINGS_CACHE
    _SETTINGS_CACHE = None


def _deep_merge(base: dict, override: dict) -> dict:
    import copy
    result = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = copy.deepcopy(v)
    return result


def _validate_settings(s: dict) -> None:
    policy = s.get("policy", {})
    for key in _POLICY_INT_KEYS:
        if key in policy and (not isinstance(policy[key], int) or isinstance(policy[key], bool)):
            raise ValueError(f"settings.policy.{key} must be an integer, got {policy[key]!r}")
    for key in _POLICY_FLOAT_KEYS:
        if key in policy and not isinstance(policy[key], (int, float)):
            raise ValueError(f"settings.policy.{key} must be a number, got {policy[key]!r}")
    runner = s.get("runner", {})
    if "timeout_s" in runner and not isinstance(runner["timeout_s"], (int, float)):
        raise ValueError(f"settings.runner.timeout_s must be a number")
    sink = s.get("metrics_sink", "local_jsonl")
    if sink not in ("local_jsonl", "null"):
        raise ValueError(f"settings.metrics_sink must be 'local_jsonl' or 'null', got {sink!r}")


def load_settings() -> dict:
    global _SETTINGS_CACHE
    if _SETTINGS_CACHE is not None:
        return _SETTINGS_CACHE
    path = default_settings_path()
    if path.exists():
        raw = _json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"settings file must be a JSON object: {path}")
        merged = _deep_merge(_DEFAULTS, raw)
    else:
        import copy
        merged = copy.deepcopy(_DEFAULTS)
    _validate_settings(merged)
    _SETTINGS_CACHE = merged
    return _SETTINGS_CACHE

