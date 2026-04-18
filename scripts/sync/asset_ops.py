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


BRIDGE_DEMOTION_REASONS: frozenset[str] = frozenset({"bridge_broken", "bridge_silent_empty", "bridge_schema_drift"})


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
    from scripts.policy_config import default_state_root, STABLE_MIN_ATTEMPTS, STABLE_MIN_SUCCESS_RATE
    from scripts.intent_registry import IntentRegistry
    state_root = Path(os.environ.get("EMERGE_STATE_ROOT") or str(default_state_root()))
    raw = IntentRegistry.load(state_root)
    result: dict[str, int] = {}
    prefix = connector + "."
    for k, v in raw.get("intents", {}).items():
        if not isinstance(v, dict) or not k.startswith(prefix):
            continue
        stage = str(v.get("stage", "")).strip()
        attempts = int(v.get("attempts_at_transition", v.get("attempts", 0)))
        successes = int(v.get("successes", 0))
        success_rate = float(v.get("success_rate", (successes / max(attempts, 1) if attempts else 0.0)))
        is_stable = stage == "stable" or (
            attempts >= STABLE_MIN_ATTEMPTS and success_rate >= STABLE_MIN_SUCCESS_RATE
        )
        if is_stable:
            result[k] = int(v.get("last_ts_ms", 0))
    return result


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
    """Merge local learning-signal spans into the worktree spans.json.

    Exports three categories of intent (each qualifies independently):
      1. **Stable** — remote machines can trust the pipeline exists.
      2. **synthesis_skipped_reason** — crystallizer refused (e.g. WAL never
         assigned ``__result``/``__action``). Remote machines writing the same
         WAL shape will hit the same refusal; surfacing the reason lets the
         next session fix it preemptively.
      3. **bridge_broken demotion** — a stable pipeline whose runtime broke
         enough times to auto-demote. Other machines shouldn't trust a crystal
         that's locally known bad.

    Remote-only spans are preserved. The projection carries ONLY diagnostic
    fields — never credentials, counters, or per-session state.
    """
    from scripts.policy_config import default_state_root, STABLE_MIN_ATTEMPTS, STABLE_MIN_SUCCESS_RATE
    from scripts.intent_registry import IntentRegistry
    state_root = Path(os.environ.get("EMERGE_STATE_ROOT") or str(default_state_root()))
    all_candidates = IntentRegistry.load(state_root).get("intents", {})

    prefix = connector + "."
    local_spans: dict[str, Any] = {}
    for key, entry in all_candidates.items():
        if not isinstance(entry, dict) or not key.startswith(prefix):
            continue
        stage = str(entry.get("stage", "")).strip()
        attempts = int(entry.get("attempts_at_transition", entry.get("attempts", 0)))
        successes = int(entry.get("successes", 0))
        success_rate = float(entry.get("success_rate", (successes / max(attempts, 1) if attempts else 0.0)))
        is_stable = stage == "stable" or (
            attempts >= STABLE_MIN_ATTEMPTS and success_rate >= STABLE_MIN_SUCCESS_RATE
        )
        skipped_reason = str(entry.get("synthesis_skipped_reason", "") or "").strip()
        demo = entry.get("last_demotion")
        demo_reason = ""
        demo_to_stage = ""
        if isinstance(demo, dict):
            demo_reason = str(demo.get("reason", "") or "").strip()
            demo_to_stage = str(demo.get("to_stage", "") or "").strip()
        has_bridge_demotion = demo_reason in BRIDGE_DEMOTION_REASONS

        if not (is_stable or skipped_reason or has_bridge_demotion):
            continue

        projected: dict[str, Any] = {
            "intent_signature": entry.get("intent_signature", key),
            "stage": stage or "explore",
            "last_ts_ms": entry.get("last_ts_ms", 0),
        }
        if skipped_reason:
            projected["synthesis_skipped_reason"] = skipped_reason
        if has_bridge_demotion:
            projected["last_demotion"] = {
                "reason": demo_reason,
                "to_stage": demo_to_stage,
            }
        local_spans[key] = projected

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
    """Merge remote spans.json into local spans.json. Remote wins on newer last_ts_ms.

    Also propagates diagnostic fields (``synthesis_skipped_reason``,
    ``last_demotion.reason``) from remote into the LOCAL IntentRegistry when
    (a) the intent already exists locally and (b) the remote record is newer
    by ``last_ts_ms``. This is what makes cross-machine lessons reach the next
    session's reflection — spans.json alone is never read by
    ``span_tracker.format_reflection``, which scans IntentRegistry.

    Invariant preserved: never writes ``stage`` or counters — only diagnostic
    fields PolicyEngine never mutates on its own lifecycle path.
    """
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

    _propagate_diagnostics_to_registry(remote_spans)


def _propagate_diagnostics_to_registry(remote_spans: dict[str, Any]) -> None:
    """Write remote diagnostic fields (skipped_reason, bridge_broken demotion)
    into local IntentRegistry entries that already exist. Non-stage, non-counter
    fields only — preserves PolicyEngine's single-writer invariant on ``stage``.
    """
    from scripts.policy_config import default_state_root
    from scripts.intent_registry import IntentRegistry

    state_root = Path(os.environ.get("EMERGE_STATE_ROOT") or str(default_state_root()))
    data = IntentRegistry.load(state_root)
    intents = data.get("intents", {})
    changed = False
    for key, remote_entry in remote_spans.items():
        if not isinstance(remote_entry, dict):
            continue
        local_entry = intents.get(key)
        if not isinstance(local_entry, dict):
            continue  # never synthesize new intents from hub — local must have attempted it
        remote_ts = int(remote_entry.get("last_ts_ms", 0) or 0)
        local_ts = int(local_entry.get("last_ts_ms", 0) or 0)
        if remote_ts <= local_ts:
            continue
        remote_skipped = str(remote_entry.get("synthesis_skipped_reason", "") or "").strip()
        if remote_skipped and remote_skipped != (local_entry.get("synthesis_skipped_reason") or ""):
            local_entry["synthesis_skipped_reason"] = remote_skipped
            changed = True
        remote_demo = remote_entry.get("last_demotion")
        remote_reason = (
            str(remote_demo.get("reason", "") or "") if isinstance(remote_demo, dict) else ""
        )
        if remote_reason in BRIDGE_DEMOTION_REASONS:
            local_demo = local_entry.get("last_demotion")
            local_reason = (
                str(local_demo.get("reason", "") or "") if isinstance(local_demo, dict) else ""
            )
            if local_reason != remote_reason:
                local_entry["last_demotion"] = {
                    "reason": remote_reason,
                    "to_stage": str(remote_demo.get("to_stage", "") or ""),
                    "imported_from_hub": True,
                }
                changed = True
    if changed:
        IntentRegistry.save(state_root, data)


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
