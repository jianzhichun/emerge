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

PIPELINE_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]*\.(read|write)\.[a-z][a-z0-9_./-]*$")

REFLECTION_CACHE_TTL_MS = 15 * 60 * 1000  # 15 minutes

USER_CONNECTOR_ROOT = Path("~/.emerge/connectors").expanduser()


def default_emerge_home() -> Path:
    return Path.home() / ".emerge"


def default_exec_root() -> Path:
    """Return the default execution-session state root (``~/.emerge/repl``).

    The on-disk directory name stays ``repl`` for data-compatibility with
    existing installations.
    """
    return default_emerge_home() / "repl"


def _plugin_data_pin_path() -> Path:
    """Path to the file that records where CC set CLAUDE_PLUGIN_DATA at install time."""
    return default_emerge_home() / "plugin_data_path"


def resolve_plugin_data_root() -> Path:
    """Return the directory CC uses for plugin state (CLAUDE_PLUGIN_DATA).

    Priority:
    1. CLAUDE_PLUGIN_DATA env var (set by CC in hook execution context)
    2. Contents of ~/.emerge/plugin_data_path (written by setup hook)
    3. Fallback: ~/.emerge/hook-state (legacy / dev environments)
    """
    env = _os.environ.get("CLAUDE_PLUGIN_DATA", "").strip()
    if env:
        return Path(env)
    pin = _plugin_data_pin_path()
    if pin.exists():
        try:
            pinned = pin.read_text(encoding="utf-8").strip()
            if pinned:
                return Path(pinned)
        except OSError:
            pass
    return default_emerge_home() / "hook-state"


def default_hook_state_root() -> Path:
    """Legacy name kept for compatibility — delegates to resolve_plugin_data_root()."""
    return resolve_plugin_data_root()


def default_goal_snapshot_path() -> Path:
    return default_hook_state_root() / "goal-snapshot.json"


def default_goal_ledger_path() -> Path:
    return default_hook_state_root() / "goal-ledger.jsonl"


def pin_plugin_data_path_if_present() -> None:
    """Keep ~/.emerge/plugin_data_path in sync with current hook env when available."""
    plugin_data = _os.environ.get("CLAUDE_PLUGIN_DATA", "").strip()
    if not plugin_data:
        return
    pin = _plugin_data_pin_path()
    try:
        pin.parent.mkdir(parents=True, exist_ok=True)
        pin.write_text(plugin_data, encoding="utf-8")
    except OSError:
        pass


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
    if not isinstance(runner, dict):
        raise ValueError(f"settings.runner must be an object, got {type(runner).__name__}")
    if "timeout_s" in runner and (not isinstance(runner["timeout_s"], (int, float)) or isinstance(runner["timeout_s"], bool)):
        raise ValueError("settings.runner.timeout_s must be a number")
    if "retry_max_attempts" in runner and (not isinstance(runner["retry_max_attempts"], int) or isinstance(runner["retry_max_attempts"], bool) or runner["retry_max_attempts"] < 1):
        raise ValueError("settings.runner.retry_max_attempts must be a positive integer")
    if "retry_base_delay_s" in runner and (not isinstance(runner["retry_base_delay_s"], (int, float)) or isinstance(runner["retry_base_delay_s"], bool) or runner["retry_base_delay_s"] < 0):
        raise ValueError("settings.runner.retry_base_delay_s must be a non-negative number")
    if "retry_max_delay_s" in runner and (not isinstance(runner["retry_max_delay_s"], (int, float)) or isinstance(runner["retry_max_delay_s"], bool) or runner["retry_max_delay_s"] < 0):
        raise ValueError("settings.runner.retry_max_delay_s must be a non-negative number")
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


def atomic_write_json(
    path: Path,
    data: Any,
    *,
    prefix: str = "",
    suffix: str = "",
    ensure_ascii: bool = True,
    indent: int | None = 2,
) -> None:
    """Atomically write JSON to *path* via temp file + fsync + os.replace.

    All callers in the project should use this instead of rolling their own
    tempfile/mkstemp/rename logic.
    """
    import tempfile as _tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    pfx = prefix or f"{path.stem}-"
    sfx = suffix or ".json"
    fd, tmp_path = _tempfile.mkstemp(prefix=pfx, suffix=sfx, dir=str(path.parent))
    _tmp = tmp_path
    try:
        with _os.fdopen(fd, "w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=ensure_ascii, indent=indent)
            f.flush()
            _os.fsync(f.fileno())
        _os.replace(tmp_path, path)
        _tmp = ""
    finally:
        if _tmp and _os.path.exists(_tmp):
            _os.unlink(_tmp)


def truncate_jsonl_if_needed(path: "Path", max_lines: int, trigger_ratio: float = 1.5) -> None:
    """Truncate a .jsonl file to *max_lines* when it exceeds max_lines * trigger_ratio.

    Reads the file once and rewrites only when the trigger threshold is crossed,
    so the amortised cost per append is O(1) for normal operation.
    Uses mkstemp + fsync + os.replace for crash-safe atomic rewrite (consistent
    with all other write paths in the project).
    Silently ignores all errors (disk full, permissions, etc.) — non-fatal.
    """
    try:
        if not path.exists():
            return
        text = path.read_text(encoding="utf-8")
        lines = [l for l in text.splitlines() if l.strip()]
        if len(lines) <= int(max_lines * trigger_ratio):
            return
        trimmed = "\n".join(lines[-max_lines:]) + "\n"
        import tempfile as _tempfile
        fd, tmp_path_str = _tempfile.mkstemp(
            prefix=f"{path.stem}-", suffix=".jsonl.tmp", dir=str(path.parent)
        )
        _tmp = tmp_path_str
        try:
            with _os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(trimmed)
                f.flush()
                _os.fsync(f.fileno())
            _os.replace(tmp_path_str, path)
            _tmp = ""
        finally:
            if _tmp and _os.path.exists(_tmp):
                _os.unlink(_tmp)
    except Exception:
        pass  # Non-fatal — truncation is a performance optimization only


def resolve_connector_root() -> Path:
    """Return active connector root: EMERGE_CONNECTOR_ROOT env var or ~/.emerge/connectors."""
    env = _os.environ.get("EMERGE_CONNECTOR_ROOT", "").strip()
    return Path(env).expanduser() if env else USER_CONNECTOR_ROOT


def load_json_object(path: "Path", *, root_key: str) -> dict:
    """Load a JSON object file, returning ``{root_key: {}}`` when missing or corrupt.

    Raises ``ValueError`` if the file exists but is not a JSON object.
    """
    if not path.exists():
        return {root_key: {}}
    try:
        data = _json.loads(path.read_text(encoding="utf-8"))
    except _json.JSONDecodeError:
        return {root_key: {}}
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must be a JSON object")
    if root_key not in data or not isinstance(data[root_key], dict):
        data[root_key] = {}
    return data

