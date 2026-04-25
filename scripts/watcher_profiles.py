from __future__ import annotations

import re
from typing import Any

from scripts.policy_config import PIPELINE_KEY_RE


_CONNECTOR_RE = re.compile(r"^[a-zA-Z0-9_.-]+$")
_SOURCE_TYPES = {"file", "command", "event"}


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
        if not PIPELINE_KEY_RE.match(intent):
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
