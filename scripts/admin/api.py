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


_VALID_ACTION_TYPES = {
    "pipeline-set", "pipeline-delete",
    "notes-comment", "notes-edit",
    "tool-call", "crystallize-component",
    "global-prompt",
}


def _validate_action(action: dict) -> str | None:
    """Return an error string if the action is invalid, else None."""
    if not isinstance(action, dict):
        return "action must be an object"
    atype = action.get("type")
    if not atype:
        return "action missing 'type'"
    if atype not in _VALID_ACTION_TYPES:
        return f"unknown action type '{atype}'"
    if atype == "tool-call":
        call = action.get("call")
        if not isinstance(call, dict):
            return "tool-call action missing 'call' object"
        if not call.get("tool"):
            return "tool-call action missing 'call.tool'"
        if not isinstance(call.get("arguments"), dict):
            return "tool-call action missing 'call.arguments' dict"
    if atype in ("pipeline-set", "pipeline-delete"):
        if not action.get("key"):
            return f"{atype} action missing 'key'"
    return None


def _enrich_actions(actions: list) -> list:
    """Enrich action payloads with context CC needs to execute them intelligently."""
    connector_root = _resolve_connector_root()
    enriched = []
    for action in actions:
        a = dict(action)
        if a.get("type") == "global-prompt":
            a["instruction"] = (
                "The user has queued a free-form instruction via the Emerge Cockpit. "
                "Execute the `prompt` field as a direct user request. "
                "Treat it exactly as if the user had typed it in the chat."
            )
        elif a.get("type") == "notes-comment":
            connector = a.get("connector", "")
            if connector:
                notes_path = connector_root / connector / "NOTES.md"
                try:
                    notes_path.resolve().relative_to(connector_root.resolve())
                    current_notes = notes_path.read_text(encoding="utf-8") if notes_path.exists() else ""
                except (ValueError, OSError):
                    current_notes = ""
                a["current_notes"] = current_notes
                a["notes_path"] = str(notes_path)
                a["instruction"] = (
                    "The user has provided an edit instruction for the connector's NOTES.md. "
                    "Read `current_notes`, apply the `comment` as a natural-language edit "
                    "(e.g. fix a mistake, add a detail, restructure a section, remove stale info). "
                    "Rewrite the file at `notes_path` with your judgment — do NOT blindly append. "
                    "Preserve existing useful content. Keep the file concise and accurate."
                )
        elif a.get("type") == "tool-call":
            call = a.get("call")
            if not isinstance(call, dict):
                a["instruction"] = (
                    "Invalid cockpit tool-call action: missing `call` object. "
                    "Do not improvise; ask user to re-submit."
                )
            else:
                tool_name = str(call.get("tool", "")).strip()
                arguments = call.get("arguments", {})
                if not tool_name or not isinstance(arguments, dict):
                    a["instruction"] = (
                        "Invalid cockpit tool-call payload. "
                        "Expected call.tool (any icc_* tool name) and call.arguments object."
                    )
                else:
                    auto = a.get("auto") if isinstance(a.get("auto"), dict) else {}
                    auto_mode = str(auto.get("mode", "assist"))
                    a["instruction"] = (
                        "Deterministic tool call (no free-form reasoning): "
                        f"call `{tool_name}` exactly once with `call.arguments`; "
                        "return the tool output to the user. "
                        f"intent_signature={a.get('intent_signature', '')}. "
                        f"automation_mode={auto_mode}. "
                        "Only if automation_mode=auto AND flywheel.synthesis_ready=true, "
                        "queue a follow-up crystallization suggestion."
                    )
        enriched.append(a)
    return enriched


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
    lines.append("Pipelines:")
    pipelines = data.get("pipelines", [])
    if not pipelines:
        lines.append("- (none)")
    else:
        for item in pipelines:
            lines.append(f"- key: {item.get('key', '')}")
            desc = item.get("description", "")
            if desc:
                lines.append(f"  description: {desc}")
            lines.append(f"  status: {item.get('status', '')}  source: {item.get('source', 'exec')}")
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
