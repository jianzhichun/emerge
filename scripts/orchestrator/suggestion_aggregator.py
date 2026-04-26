from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable


class SuggestionAggregator:
    def __init__(
        self,
        *,
        state_root: Path,
        emit_cockpit_action: Callable[[dict[str, Any]], None],
    ) -> None:
        self._state_root = state_root
        self._emit_cockpit_action = emit_cockpit_action
        self._seen: set[tuple[str, str, str]] = set()
        self._by_intent: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._lock = threading.Lock()
        self._replay_persisted()

    def on_suggestion(self, suggestion: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            intent = str(
                suggestion.get("intent_signature_hint")
                or suggestion.get("intent_signature")
                or suggestion.get("normalized_intent")
                or ""
            ).strip()
            if not intent:
                return {"status": "ignored", "reason": "missing_intent_signature_hint"}
            runner = str(suggestion.get("runner_profile", "") or "").strip() or "unknown"
            raw_hash = _raw_actions_hash(suggestion.get("raw_actions", []))
            dedupe_key = (intent, raw_hash, runner)
            if dedupe_key in self._seen:
                return {"status": "duplicate", "intent_signature_hint": intent}
            self._seen.add(dedupe_key)

            normalized = dict(suggestion)
            normalized["intent_signature_hint"] = intent
            normalized["runner_profile"] = runner
            normalized["raw_actions_hash"] = raw_hash
            normalized.setdefault("ts_ms", int(time.time() * 1000))
            self._by_intent[intent].append(normalized)
            self._persist(normalized)
            fact = self._aggregate_fact(intent)
            self._persist_aggregate(fact)

        self._emit_cockpit_action(fact)
        return {"status": "stored", "intent_signature_hint": intent}

    def _aggregate_fact(self, intent: str) -> dict[str, Any]:
        suggestions = self._by_intent[intent]
        runners = sorted({str(s.get("runner_profile", "unknown")) for s in suggestions})
        return {
            "type": "pattern_aggregated",
            "payload": {
                "intent_signature_hint": intent,
                "runner_profiles": runners,
                "suggestion_count": len(suggestions),
                "suggestions": suggestions,
                "parameter_ranges": _parameter_ranges(suggestions),
                "context_hints": [
                    str(s.get("context_hint", "")).strip()
                    for s in suggestions
                    if str(s.get("context_hint", "")).strip()
                ],
            },
        }

    def _persist(self, suggestion: dict[str, Any]) -> None:
        path = self._state_root / "suggestions" / "suggestions.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(suggestion, ensure_ascii=False) + "\n")

    def _persist_aggregate(self, fact: dict[str, Any]) -> None:
        path = self._state_root / "suggestions" / "aggregated.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(fact, ensure_ascii=False) + "\n")

    def _replay_persisted(self) -> None:
        suggestions_path = self._state_root / "suggestions" / "suggestions.jsonl"
        if suggestions_path.exists():
            try:
                for line in suggestions_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        suggestion = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(suggestion, dict):
                        continue
                    intent = str(suggestion.get("intent_signature_hint") or "").strip()
                    if not intent:
                        continue
                    runner = str(suggestion.get("runner_profile") or "unknown")
                    raw_hash = str(suggestion.get("raw_actions_hash") or _raw_actions_hash(suggestion.get("raw_actions", [])))
                    self._seen.add((intent, raw_hash, runner))
                    self._by_intent[intent].append(suggestion)
            except OSError:
                pass



def _raw_actions_hash(raw_actions: Any) -> str:
    material = json.dumps(raw_actions, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _parameter_ranges(suggestions: list[dict[str, Any]]) -> dict[str, list[Any]]:
    values: dict[str, set[str]] = defaultdict(set)
    original: dict[tuple[str, str], Any] = {}
    for suggestion in suggestions:
        params = suggestion.get("preferred_params")
        if not isinstance(params, dict):
            continue
        for key, value in params.items():
            marker = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
            values[str(key)].add(marker)
            original[(str(key), marker)] = value
    return {
        key: [original[(key, marker)] for marker in sorted(markers)]
        for key, markers in values.items()
    }
