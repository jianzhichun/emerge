"""Pipeline, connector, and policy lifecycle operations."""
from __future__ import annotations

import json
import re
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
    atomic_write_json,
)


# ---------------------------------------------------------------------------
# Pipeline registry helpers
# ---------------------------------------------------------------------------

def _normalize_pipeline_key(key: str) -> str:
    """Key is the plain intent signature (e.g. 'mock.read.layers'). Strip legacy prefixes."""
    key = key.strip()
    for prefix in ("pipeline::", "flywheel::", "default::"):
        if key.startswith(prefix):
            key = key[len(prefix):]
            break
    return key


def _load_registry(state_root: Path) -> tuple[Path, dict]:
    registry_path = state_root / "pipelines-registry.json"
    if registry_path.exists():
        data = json.loads(registry_path.read_text(encoding="utf-8"))
    else:
        data = {"pipelines": {}}
    return registry_path, data


def _save_registry(registry_path: Path, data: dict) -> None:
    """Atomic write: temp file + os.replace to prevent half-written state on crash."""
    atomic_write_json(
        registry_path,
        data,
        prefix=".registry-",
        suffix=".json",
        ensure_ascii=False,
        indent=2,
    )


def _normalize_intent_signature(value: str) -> str:
    """Normalize legacy intent format read.<connector>.<name> to <connector>.read.<name>."""
    sig = str(value or "").strip().strip("'\"")
    m = re.fullmatch(r"(read|write)\.([a-z][a-z0-9_-]*)\.([a-z][a-z0-9_./-]*)", sig)
    if not m:
        return sig
    mode, connector, name = m.groups()
    return f"{connector}.{mode}.{name}"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_policy_status(session_id: str | None = None) -> dict:
    from scripts.admin.control_plane import _resolve_session_id
    state_root = _resolve_state_root()
    registry_path = state_root / "pipelines-registry.json"
    pipelines = []
    registry_corrupt = False
    if registry_path.exists():
        try:
            data = json.loads(registry_path.read_text(encoding="utf-8"))
        except Exception:
            data = {"pipelines": {}}
            registry_corrupt = True
        raw = data.get("pipelines", {})
        if isinstance(raw, dict):
            for key, value in raw.items():
                if not isinstance(value, dict):
                    continue
                item = {"key": key, **value}
                pipelines.append(item)
    pipelines.sort(key=lambda x: (str(x.get("status", "")), str(x.get("key", ""))))
    return {
        "session_id": _resolve_session_id(session_id=session_id),
        "state_root": str(_resolve_state_root()),
        "registry_exists": registry_path.exists(),
        "registry_corrupt": registry_corrupt,
        "pipeline_count": len(pipelines),
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
        "pipelines": pipelines,
    }


def cmd_pipeline_delete(*, key: str) -> dict:
    """Remove a pipeline entry from the registry."""
    full_key = _normalize_pipeline_key(key)
    state_root = _resolve_state_root()
    registry_path, data = _load_registry(state_root)
    pipelines = data.get("pipelines", {})
    if full_key not in pipelines:
        return {"ok": False, "error": f"pipeline not found: {full_key}", "key": full_key}
    del pipelines[full_key]
    data["pipelines"] = pipelines
    _save_registry(registry_path, data)
    return {"ok": True, "deleted": full_key, "remaining": len(pipelines)}


def cmd_pipeline_set(*, key: str, fields: dict) -> dict:
    """Reconcile / patch specific fields on a pipeline registry entry."""
    PATCHABLE = {
        "status", "rollout_pct", "consecutive_failures",
        "policy_enforced_count", "stop_triggered_count", "rollback_executed_count",
        "last_policy_action", "success_rate", "verify_rate", "human_fix_rate",
    }
    unknown = set(fields) - PATCHABLE
    if unknown:
        return {"ok": False, "error": f"unknown fields: {sorted(unknown)}", "allowed": sorted(PATCHABLE)}

    full_key = _normalize_pipeline_key(key)
    state_root = _resolve_state_root()
    registry_path, data = _load_registry(state_root)
    pipelines = data.get("pipelines", {})

    if full_key not in pipelines:
        return {"ok": False, "error": f"pipeline not found: {full_key}", "key": full_key}

    before = dict(pipelines[full_key])
    pipelines[full_key].update(fields)
    data["pipelines"] = pipelines
    _save_registry(registry_path, data)
    return {
        "ok": True,
        "key": full_key,
        "patched": fields,
        "before": {k: before.get(k) for k in fields},
        "after": {k: pipelines[full_key].get(k) for k in fields},
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
        for k, v in registry_data.get("pipelines", {}).items()
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
            "pipelines-registry.json",
            json.dumps({"pipelines": filtered}, indent=2, ensure_ascii=False),
        )
        for f in files:
            arcname = f"connectors/{connector}/{f.relative_to(connector_dir)}"
            zf.write(f, arcname)

    return {
        "ok": True,
        "connector": connector,
        "out": str(out_path),
        "pipeline_count": len(filtered),
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
            imported_reg = json.loads(zf.read("pipelines-registry.json"))
        except KeyError:
            imported_reg = {"pipelines": {}}

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
    registry_path, existing = _load_registry(s_root)
    existing_pipelines = existing.get("pipelines", {})
    imported_pipelines = imported_reg.get("pipelines", {})

    merged: list[str] = []
    skipped: list[str] = []
    for k, v in imported_pipelines.items():
        if k in existing_pipelines and not overwrite:
            skipped.append(k)
        else:
            existing_pipelines[k] = v
            merged.append(k)

    existing["pipelines"] = existing_pipelines
    _save_registry(registry_path, existing)

    return {
        "ok": True,
        "connector": connector,
        "pkg": str(pkg_path),
        "file_count": file_count,
        "pipelines_merged": merged,
        "pipelines_skipped": skipped,
    }


def cmd_normalize_intents(*, connector: str = "", connector_root: Path | None = None) -> dict:
    """Normalize legacy intent_signature values in connector pipeline YAML files."""
    c_root = connector_root if connector_root is not None else _resolve_connector_root()
    if not c_root.exists():
        return {
            "ok": True,
            "connector_root": str(c_root),
            "connector": connector or "*",
            "normalized_files": 0,
            "scanned_files": 0,
            "changes": [],
        }

    connector = str(connector or "").strip()
    if connector:
        connectors = [c_root / connector]
    else:
        connectors = [p for p in sorted(c_root.iterdir()) if p.is_dir()]

    changes: list[dict[str, str]] = []
    scanned = 0
    for conn_dir in connectors:
        if not conn_dir.exists():
            continue
        for yaml_path in sorted((conn_dir / "pipelines").glob("*/*.yaml")):
            scanned += 1
            text = yaml_path.read_text(encoding="utf-8")
            lines = text.splitlines()
            updated = False
            for i, line in enumerate(lines):
                m = re.match(r"^(\s*intent_signature\s*:\s*)(.+?)\s*$", line)
                if not m:
                    continue
                prefix, raw_val = m.groups()
                normalized = _normalize_intent_signature(raw_val)
                raw_clean = raw_val.strip().strip("'\"")
                if normalized != raw_clean:
                    lines[i] = f"{prefix}{normalized}"
                    updated = True
                    changes.append({"file": str(yaml_path), "from": raw_clean, "to": normalized})
                break
            if updated:
                yaml_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return {
        "ok": True,
        "connector_root": str(c_root),
        "connector": connector or "*",
        "normalized_files": len(changes),
        "scanned_files": scanned,
        "changes": changes,
    }
