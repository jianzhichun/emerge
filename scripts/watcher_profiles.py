from __future__ import annotations

import re
import json
from pathlib import Path
from typing import Any

from scripts.policy_config import PIPELINE_KEY_RE, resolve_connector_root


_CONNECTOR_RE = re.compile(r"^[a-zA-Z0-9_.-]+$")
_SOURCE_TYPES = {"file", "command", "event"}


def _valid_intent_hint(intent: str, connector: str) -> bool:
    if PIPELINE_KEY_RE.match(intent):
        return True
    # The repository ships a template connector under connectors/_template/.
    # Real connector intents still use PIPELINE_KEY_RE.
    return connector == "_template" and intent.startswith("_template.") and len(intent.split(".", 2)) == 3


def validate_watcher_profile(profile: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(profile, dict):
        raise ValueError("watcher profile must be an object")
    connector = str(profile.get("connector", "")).strip()
    if not connector or not _CONNECTOR_RE.fullmatch(connector):
        raise ValueError("watcher profile connector is required")

    sources = profile.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("watcher profile sources must be a non-empty list")
    normalized_sources: list[dict[str, Any]] = []
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("watcher profile source must be an object")
        source_type = str(source.get("type", "")).strip()
        if source_type not in _SOURCE_TYPES:
            raise ValueError(f"unsupported watcher source type: {source_type}")
        if source_type in {"file", "command"} and not str(source.get("path" if source_type == "file" else "run", "")).strip():
            raise ValueError(f"watcher {source_type} source is missing required field")
        normalized_sources.append(dict(source))

    hints = profile.get("intent_hints", [])
    if not isinstance(hints, list):
        raise ValueError("watcher profile intent_hints must be a list")
    normalized_hints: list[dict[str, Any]] = []
    for hint in hints:
        if not isinstance(hint, dict):
            raise ValueError("watcher profile intent hint must be an object")
        intent = str(hint.get("intent", "")).strip()
        if not _valid_intent_hint(intent, connector):
            raise ValueError("watcher intent hints must use connector.mode.name")
        if not intent.startswith(connector + "."):
            raise ValueError("watcher intent hint connector must match profile connector")
        normalized_hints.append(dict(hint))

    normalized = dict(profile)
    normalized["connector"] = connector
    normalized["sources"] = normalized_sources
    normalized["intent_hints"] = normalized_hints
    normalized.setdefault("parser", {})
    normalized.setdefault("preference_hints", [])
    normalized.setdefault("redaction", {})
    return normalized


def load_watcher_profile(connector: str, *, connector_root: Path | None = None) -> dict[str, Any] | None:
    """Load and validate a connector's optional watcher profile.

    Missing, malformed, or schema-invalid profiles degrade to ``None`` so a
    runner can idle instead of crashing when a connector has no reverse-flywheel
    watcher configuration.
    """
    connector_name = str(connector).strip()
    if not connector_name:
        return None
    root = connector_root or resolve_connector_root()
    path = root / connector_name / "watcher_profile.yaml"
    if not path.exists():
        return None
    try:
        import yaml  # type: ignore

        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return validate_watcher_profile(raw)
    except ValueError:
        return None


def materialize_active_profiles(state_root: Path, *, connector_root: Path | None = None) -> Path:
    """Write all available watcher profiles to a runner-readable JSON file."""
    root = connector_root or resolve_connector_root()
    profiles: dict[str, dict[str, Any]] = {}
    if root.exists():
        for connector_dir in sorted(root.iterdir(), key=lambda p: p.name):
            if not connector_dir.is_dir():
                continue
            profile = load_watcher_profile(connector_dir.name, connector_root=root)
            if profile is not None:
                profiles[connector_dir.name] = profile
    out = state_root.parent / "runner" / "active_profiles.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"profiles": profiles}, indent=2, ensure_ascii=False), encoding="utf-8")
    return out
