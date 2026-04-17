"""Cockpit control-plane API — shared business logic layer.

All data-manipulation functions used by both the cockpit HTTP server and the
CLI commands.  No HTTP types imported here so this module can be unit-tested
without a running server.

Control-plane functions live in admin.control_plane; pipeline/connector
operations in admin.pipeline; shared resolvers in admin.shared.
This module keeps SSE, cockpit HTML, settings, and status commands.
"""
from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

import sys
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.policy_config import (  # noqa: E402
    PROMOTE_MAX_HUMAN_FIX_RATE,
    PROMOTE_MIN_ATTEMPTS,
    PROMOTE_MIN_SUCCESS_RATE,
    PROMOTE_MIN_VERIFY_RATE,
    ROLLBACK_CONSECUTIVE_FAILURES,
    STABLE_MIN_ATTEMPTS,
    STABLE_MIN_SUCCESS_RATE,
    STABLE_MIN_VERIFY_RATE,
    atomic_write_json,
)
from scripts.admin.shared import _resolve_state_root, _resolve_connector_root, _local_plugin_version  # noqa: E402
from scripts.admin.control_plane import _session_paths, _resolve_session_id  # noqa: E402
from scripts.admin.actions import ActionContext, ActionRegistry  # noqa: E402
from scripts.state_tracker import StateTracker, load_tracker, save_tracker  # noqa: E402

# ---------------------------------------------------------------------------
# Cockpit HTML injection helpers
# ---------------------------------------------------------------------------

_MAX_INJECTED_PER_CONNECTOR = 50


def _injected_runtime_basename(i: int) -> str:
    return f"injected-runtime-{i}.html"


def _cockpit_inject_html(connector: str, html: str, slot_id: str | None = None,
                         *, store: dict, lock: threading.Lock) -> None:
    """Inject or update an HTML component slot."""
    with lock:
        slots = store.setdefault(connector, [])
        if slot_id is not None:
            for i, s in enumerate(slots):
                if s.get("id") == slot_id:
                    slots[i] = {"id": slot_id, "html": html}
                    return
            slots.append({"id": slot_id, "html": html})
        else:
            slots.append({"id": None, "html": html})
        if len(slots) > _MAX_INJECTED_PER_CONNECTOR:
            store[connector] = slots[-_MAX_INJECTED_PER_CONNECTOR:]


def _cockpit_list_injected_html(connector: str, store: dict) -> list[str]:
    return [s["html"] for s in store.get(connector, [])]


# ---------------------------------------------------------------------------
# CLI data commands
# ---------------------------------------------------------------------------

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


def cmd_assets(injected_html: dict) -> dict:
    """Return per-connector assets: notes content and crystallized components."""
    try:
        connector_root = _resolve_connector_root()
    except Exception:
        return {"connectors": {}}

    connectors: dict = {}
    if not connector_root.exists():
        return {"connectors": connectors}

    for connector_dir in sorted(connector_root.iterdir()):
        if not connector_dir.is_dir() or connector_dir.is_symlink():
            continue
        name = connector_dir.name

        try:
            notes_path = connector_dir / "NOTES.md"
            notes = notes_path.read_text(encoding="utf-8") if notes_path.exists() else None
        except OSError:
            notes = None

        components: list = []
        cockpit_dir = connector_dir / "cockpit"
        if cockpit_dir.exists():
            for html_file in sorted(cockpit_dir.glob("*.html")):
                try:
                    ctx_file = cockpit_dir / f"{html_file.stem}.context.md"
                    components.append({
                        "filename": html_file.name,
                        "context": ctx_file.read_text(encoding="utf-8") if ctx_file.exists() else "",
                    })
                except OSError:
                    components.append({"filename": html_file.name, "context": ""})

        injected = _cockpit_list_injected_html(name, store=injected_html)
        for i in range(len(injected)):
            components.append({
                "filename": _injected_runtime_basename(i),
                "context": "Runtime-injected control (session-only; crystallize to persist on disk).",
            })

        connectors[name] = {"notes": notes, "components": components}

    return {"connectors": connectors}


def _validate_action(action: dict) -> str | None:
    """Return an error string if the action is invalid, else None."""
    return ActionRegistry.validate(action)


def _enrich_actions(actions: list) -> list:
    """Enrich action payloads with context CC needs to execute them intelligently."""
    connector_root = _resolve_connector_root()
    return ActionRegistry.enrich(actions, ActionContext(connector_root=connector_root))


def _cmd_save_settings(patch: dict) -> dict:
    """Merge *patch* into ~/.emerge/settings.json and reset the settings cache."""
    from scripts.policy_config import (
        default_settings_path,
        load_settings,
        _deep_merge,
        _validate_settings,
        _reset_settings_cache,
        _POLICY_INT_KEYS,
        _POLICY_FLOAT_KEYS,
    )

    policy_patch = patch.get("policy")
    if not isinstance(policy_patch, dict):
        return {"ok": False, "error": "request body must have a 'policy' object"}

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

    atomic_write_json(
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


# ---------------------------------------------------------------------------
# Render helpers
# ---------------------------------------------------------------------------

def render_policy_status_pretty(data: dict) -> str:
    lines: list[str] = []
    lines.append(f"Session: {data.get('session_id', '')}")
    lines.append(f"State root: {data.get('state_root', '')}")
    lines.append("")
    lines.append("Thresholds:")
    thresholds = data.get("thresholds", {})
    for key in sorted(thresholds.keys()):
        lines.append(f"- {key}: {thresholds[key]}")
    lines.append("")
    lines.append("Intents:")
    intents = data.get("intents", [])
    if not intents:
        lines.append("- (none)")
    else:
        for item in intents:
            lines.append(f"- key: {item.get('key', '')}")
            desc = item.get("description", "")
            if desc:
                lines.append(f"  description: {desc}")
            lines.append(f"  stage: {item.get('stage', '')}")
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
