"""Connector asset export and import for Memory Hub.

Handles the file-level copy operations between local connector directories
and the hub worktree. No git operations here — see git_ops.py.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any


def connectors_root() -> Path:
    override = os.environ.get("EMERGE_CONNECTOR_ROOT")
    if override:
        return Path(override)
    return Path.home() / ".emerge" / "connectors"


def file_to_intent_sig(connector: str, rel: Path) -> str:
    """Convert pipelines/read/foo.py → connector.read.foo"""
    parts = rel.parts
    if len(parts) == 2:
        mode = parts[0]
        name = Path(parts[1]).stem
        return f"{connector}.{mode}.{name}"
    return ""


def load_candidate_timestamps(connector: str) -> dict[str, int]:
    """Return {intent_sig: last_ts_ms} for stable span entries belonging to connector."""
    from scripts.policy_config import default_exec_root, STABLE_MIN_ATTEMPTS, STABLE_MIN_SUCCESS_RATE
    state_root = Path(os.environ.get("EMERGE_STATE_ROOT") or str(default_exec_root()))
    p = state_root / "span-candidates.json"
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        result: dict[str, int] = {}
        prefix = connector + "."
        for k, v in raw.get("spans", {}).items():
            if not isinstance(v, dict) or not k.startswith(prefix):
                continue
            attempts = int(v.get("attempts", 0))
            successes = int(v.get("successes", 0))
            if attempts >= STABLE_MIN_ATTEMPTS and (successes / max(attempts, 1)) >= STABLE_MIN_SUCCESS_RATE:
                result[k] = int(v.get("last_ts_ms", 0))
        return result
    except Exception:
        return {}


def load_spans_timestamps(worktree_connector_dir: Path) -> dict[str, int]:
    """Return {intent_sig: last_ts_ms} from spans.json in the hub worktree connector dir."""
    p = worktree_connector_dir / "spans.json"
    if not p.exists():
        return {}
    try:
        spans = json.loads(p.read_text(encoding="utf-8")).get("spans", {})
        return {k: int(v.get("last_ts_ms", 0)) for k, v in spans.items() if isinstance(v, dict)}
    except Exception:
        return {}


def export_vertical(
    connector: str,
    *,
    connectors_root_path: Path | None = None,
    hub_worktree: Path | None = None,
) -> None:
    """Copy connector assets from local connectors dir into the hub worktree."""
    from scripts.hub_config import hub_worktree_path
    src = (connectors_root_path or connectors_root()) / connector
    dst = (hub_worktree or hub_worktree_path()) / "connectors" / connector
    dst.mkdir(parents=True, exist_ok=True)

    src_pipelines = src / "pipelines"
    dst_pipelines = dst / "pipelines"

    if src_pipelines.exists():
        local_ts = load_candidate_timestamps(connector)
        remote_ts = load_spans_timestamps(dst)

        for py_file in src_pipelines.rglob("*.py"):
            rel = py_file.relative_to(src_pipelines)
            intent_sig = file_to_intent_sig(connector, rel)
            if not intent_sig or intent_sig not in local_ts:
                continue
            l_ts = local_ts[intent_sig]
            r_ts = remote_ts.get(intent_sig, 0)
            if l_ts >= r_ts:
                dst_file = dst_pipelines / rel
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(py_file, dst_file)
                yaml_src = py_file.with_suffix(".yaml")
                dst_yaml = dst_file.with_suffix(".yaml")
                if yaml_src.exists():
                    shutil.copy2(yaml_src, dst_yaml)
                elif dst_yaml.exists():
                    dst_yaml.unlink()

    notes_src = src / "NOTES.md"
    if notes_src.exists():
        shutil.copy2(notes_src, dst / "NOTES.md")

    export_spans_json(connector, dst)


def export_spans_json(connector: str, dst: Path) -> None:
    """Merge local stable spans into the worktree spans.json. Remote-only spans are preserved."""
    from scripts.policy_config import default_exec_root, STABLE_MIN_ATTEMPTS, STABLE_MIN_SUCCESS_RATE
    state_root = Path(os.environ.get("EMERGE_STATE_ROOT") or str(default_exec_root()))
    candidates_path = state_root / "span-candidates.json"
    if not candidates_path.exists():
        return
    try:
        raw = json.loads(candidates_path.read_text(encoding="utf-8"))
        all_candidates = raw.get("spans", {})
    except Exception:
        return

    prefix = connector + "."
    local_spans: dict[str, Any] = {}
    for key, entry in all_candidates.items():
        if not isinstance(entry, dict) or not key.startswith(prefix):
            continue
        attempts = int(entry.get("attempts", 0))
        successes = int(entry.get("successes", 0))
        if attempts < STABLE_MIN_ATTEMPTS or (successes / max(attempts, 1)) < STABLE_MIN_SUCCESS_RATE:
            continue
        local_spans[key] = {
            "intent_signature": entry.get("intent_signature", key),
            "status": "stable",
            "last_ts_ms": entry.get("last_ts_ms", 0),
        }

    existing_path = dst / "spans.json"
    existing_spans: dict[str, Any] = {}
    if existing_path.exists():
        try:
            existing_spans = json.loads(existing_path.read_text(encoding="utf-8")).get("spans", {})
        except Exception:
            pass

    merged = dict(existing_spans)
    for key, entry in local_spans.items():
        existing = merged.get(key)
        if not isinstance(existing, dict) or entry.get("last_ts_ms", 0) >= existing.get("last_ts_ms", 0):
            merged[key] = entry

    dst.mkdir(parents=True, exist_ok=True)
    write_json(dst / "spans.json", {"spans": merged})


def import_vertical(
    connector: str,
    *,
    connectors_root_path: Path | None = None,
    hub_worktree: Path | None = None,
) -> None:
    """Copy connector assets from hub worktree into local connectors dir."""
    from scripts.hub_config import hub_worktree_path
    src = (hub_worktree or hub_worktree_path()) / "connectors" / connector
    dst = (connectors_root_path or connectors_root()) / connector

    if not src.exists():
        return

    dst.mkdir(parents=True, exist_ok=True)

    src_pipelines = src / "pipelines"
    if src_pipelines.exists():
        dst_pipelines = dst / "pipelines"
        if dst_pipelines.exists():
            shutil.rmtree(dst_pipelines)
        shutil.copytree(src_pipelines, dst_pipelines)

    notes_src = src / "NOTES.md"
    if notes_src.exists():
        shutil.copy2(notes_src, dst / "NOTES.md")

    import_spans_json(src, dst)


def import_spans_json(src: Path, dst: Path) -> None:
    """Merge remote spans.json into local spans.json. Remote wins on newer last_ts_ms."""
    remote_path = src / "spans.json"
    if not remote_path.exists():
        return
    try:
        remote_spans = json.loads(remote_path.read_text(encoding="utf-8")).get("spans", {})
    except Exception:
        return

    local_path = dst / "spans.json"
    try:
        local_spans = json.loads(local_path.read_text(encoding="utf-8")).get("spans", {}) if local_path.exists() else {}
    except Exception:
        local_spans = {}

    merged = dict(local_spans)
    for key, entry in remote_spans.items():
        if not isinstance(entry, dict):
            continue
        local_entry = merged.get(key)
        if local_entry is None or entry.get("last_ts_ms", 0) > local_entry.get("last_ts_ms", 0):
            merged[key] = entry

    write_json(local_path, {"spans": merged})


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".hub-import-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2, ensure_ascii=False))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
