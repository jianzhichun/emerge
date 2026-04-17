"""Pipeline, connector, and policy lifecycle operations."""
from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path

import sys

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.admin.shared import _resolve_connector_root, _resolve_state_root  # noqa: E402
from scripts.policy_config import (  # noqa: E402
    PROMOTE_MAX_HUMAN_FIX_RATE,
    PROMOTE_MIN_ATTEMPTS,
    PROMOTE_MIN_SUCCESS_RATE,
    PROMOTE_MIN_VERIFY_RATE,
    ROLLBACK_CONSECUTIVE_FAILURES,
    STABLE_MIN_ATTEMPTS,
    STABLE_MIN_SUCCESS_RATE,
    STABLE_MIN_VERIFY_RATE,
)
from scripts.intent_registry import IntentRegistry, registry_path as intent_registry_path  # noqa: E402

# Connector bundle contract: intent registry payload inside zip is fixed at this
# entry name. This is a transport artifact, not the local state path.
CONNECTOR_PACKAGE_INTENTS_FILENAME = "intents.json"


def _load_registry(state_root: Path) -> tuple[Path, dict]:
    registry_path = intent_registry_path(state_root)
    data = IntentRegistry.load(state_root)
    return registry_path, data


def _save_registry(state_root: Path, data: dict) -> None:
    """Atomic write via IntentRegistry."""
    IntentRegistry.save(state_root, data)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_policy_status(session_id: str | None = None) -> dict:
    from scripts.admin.control_plane import _resolve_session_id
    state_root = _resolve_state_root()
    registry_path = intent_registry_path(state_root)
    intents = []
    data = IntentRegistry.load(state_root)
    raw = data.get("intents", {})
    if isinstance(raw, dict):
        for key, value in raw.items():
            if not isinstance(value, dict):
                continue
            item = {"key": key, **value}
            intents.append(item)
    intents.sort(key=lambda x: (str(x.get("stage", "")), str(x.get("key", ""))))
    return {
        "session_id": _resolve_session_id(session_id=session_id),
        "state_root": str(_resolve_state_root()),
        "registry_exists": registry_path.exists(),
        "registry_corrupt": False,
        "intent_count": len(intents),
        "thresholds": {
            "promote_min_attempts": PROMOTE_MIN_ATTEMPTS,
            "promote_min_success_rate": PROMOTE_MIN_SUCCESS_RATE,
            "promote_min_verify_rate": PROMOTE_MIN_VERIFY_RATE,
            "promote_max_human_fix_rate": PROMOTE_MAX_HUMAN_FIX_RATE,
            "stable_min_attempts": STABLE_MIN_ATTEMPTS,
            "stable_min_success_rate": STABLE_MIN_SUCCESS_RATE,
            "stable_min_verify_rate": STABLE_MIN_VERIFY_RATE,
            "rollback_consecutive_failures": ROLLBACK_CONSECUTIVE_FAILURES,
        },
        "intents": intents,
    }


def cmd_intent_delete(*, key: str) -> dict:
    """Remove an intent entry from the registry."""
    full_key = key.strip()
    state_root = _resolve_state_root()
    _, data = _load_registry(state_root)
    intents = data.get("intents", {})
    if full_key not in intents:
        return {"ok": False, "error": f"intent not found: {full_key}", "key": full_key}
    del intents[full_key]
    data["intents"] = intents
    _save_registry(state_root, data)
    return {"ok": True, "deleted": full_key, "remaining": len(intents)}


def cmd_intent_set(*, key: str, fields: dict) -> dict:
    """Reconcile / patch specific fields on an intent registry entry."""
    PATCHABLE = {
        "stage", "rollout_pct", "consecutive_failures",
        "policy_enforced_count", "stop_triggered_count", "rollback_executed_count",
        "last_policy_action", "success_rate", "verify_rate", "human_fix_rate",
    }
    unknown = set(fields) - PATCHABLE
    if unknown:
        return {"ok": False, "error": f"unknown fields: {sorted(unknown)}", "allowed": sorted(PATCHABLE)}

    full_key = key.strip()
    state_root = _resolve_state_root()
    _, data = _load_registry(state_root)
    intents = data.get("intents", {})

    if full_key not in intents:
        return {"ok": False, "error": f"intent not found: {full_key}", "key": full_key}

    before = dict(intents[full_key])
    intents[full_key].update(fields)
    data["intents"] = intents
    _save_registry(state_root, data)
    return {
        "ok": True,
        "key": full_key,
        "patched": fields,
        "before": {k: before.get(k) for k in fields},
        "after": {k: intents[full_key].get(k) for k in fields},
    }


def cmd_connector_export(
    *,
    connector: str,
    out: str,
    connector_root: Path | None = None,
    state_root: Path | None = None,
) -> dict:
    """Pack a connector directory and its registry entries into a zip file."""
    from scripts.admin.shared import _local_plugin_version
    c_root = connector_root if connector_root is not None else _resolve_connector_root()
    connector_dir = c_root / connector
    if not connector_dir.exists():
        return {"ok": False, "error": f"connector not found: {connector_dir}"}

    s_root = state_root if state_root is not None else _resolve_state_root()
    _, registry_data = _load_registry(s_root)

    prefix = f"{connector}."
    filtered = {
        k: v
        for k, v in registry_data.get("intents", {}).items()
        if k.startswith(prefix)
    }

    out_path = Path(out)
    manifest = {
        "name": connector,
        "emerge_version": _local_plugin_version(),
        "exported_at_ms": int(time.time() * 1000),
    }

    files = sorted(
        f for f in connector_dir.rglob("*")
        if f.is_file() and "__pycache__" not in f.parts
    )

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
        zf.writestr(
            CONNECTOR_PACKAGE_INTENTS_FILENAME,
            json.dumps({"intents": filtered}, indent=2, ensure_ascii=False),
        )
        for f in files:
            arcname = f"connectors/{connector}/{f.relative_to(connector_dir)}"
            zf.write(f, arcname)

    return {
        "ok": True,
        "connector": connector,
        "out": str(out_path),
        "intent_count": len(filtered),
        "file_count": len(files),
    }


def cmd_connector_import(
    *,
    pkg: str,
    overwrite: bool = False,
    connector_root: Path | None = None,
    state_root: Path | None = None,
) -> dict:
    """Unpack a connector asset package and merge its registry entries."""
    pkg_path = Path(pkg)
    if not pkg_path.exists():
        return {"ok": False, "error": f"package not found: {pkg_path}"}

    with zipfile.ZipFile(pkg_path, "r") as zf:
        try:
            manifest = json.loads(zf.read("manifest.json"))
        except KeyError:
            return {"ok": False, "error": "invalid package: missing manifest.json"}

        connector = manifest.get("name", "")
        if not connector:
            return {"ok": False, "error": "invalid manifest: missing name"}

        c_root = connector_root if connector_root is not None else _resolve_connector_root()
        connector_dest = c_root / connector

        if connector_dest.exists() and not overwrite:
            return {
                "ok": False,
                "error": f"connector already exists: {connector_dest}. Use --overwrite to replace.",
            }

        try:
            imported_reg = json.loads(zf.read(CONNECTOR_PACKAGE_INTENTS_FILENAME))
        except KeyError:
            imported_reg = {"intents": {}}

        arc_prefix = f"connectors/{connector}/"
        file_count = 0
        connector_dest_resolved = connector_dest.resolve()
        for item in zf.infolist():
            if not item.filename.startswith(arc_prefix):
                continue
            rel = item.filename[len(arc_prefix):]
            if not rel or rel.endswith("/"):
                continue
            dest = (connector_dest / rel).resolve()
            try:
                dest.relative_to(connector_dest_resolved)
            except ValueError:
                return {"ok": False, "error": f"invalid package: path traversal in entry {item.filename!r}"}
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(zf.read(item.filename))
            file_count += 1

    s_root = state_root if state_root is not None else _resolve_state_root()
    _, existing = _load_registry(s_root)
    existing_pipelines = existing.get("intents", {})
    imported_pipelines = imported_reg.get("intents", {})

    merged: list[str] = []
    skipped: list[str] = []
    for k, v in imported_pipelines.items():
        if k in existing_pipelines and not overwrite:
            skipped.append(k)
        else:
            existing_pipelines[k] = v
            merged.append(k)

    existing["intents"] = existing_pipelines
    _save_registry(s_root, existing)

    return {
        "ok": True,
        "connector": connector,
        "pkg": str(pkg_path),
        "file_count": file_count,
        "intents_merged": merged,
        "intents_skipped": skipped,
    }


