from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_suggestion_aggregator_dedupes_and_emits_cross_runner_action(tmp_path):
    from scripts.orchestrator.suggestion_aggregator import SuggestionAggregator

    emitted: list[dict] = []
    aggregator = SuggestionAggregator(
        state_root=tmp_path / "state",
        emit_cockpit_action=emitted.append,
        min_runners=2,
        min_occurrences_single_runner=3,
    )

    base = {
        "intent_signature_hint": "hypermesh.write.automesh",
        "raw_actions": ["*automesh density=coarse"],
        "context_hint": "automesh repeated",
        "preferred_params": {"density": "coarse"},
    }

    assert aggregator.on_suggestion({**base, "runner_profile": "runner-a"})["status"] == "stored"
    assert aggregator.on_suggestion({**base, "runner_profile": "runner-a"})["status"] == "duplicate"
    result = aggregator.on_suggestion(
        {
            **base,
            "runner_profile": "runner-b",
            "preferred_params": {"density": "fine"},
            "raw_actions": ["*automesh density=fine"],
        }
    )

    assert result["status"] == "triggered"
    assert len(emitted) == 1
    action = emitted[0]
    assert action["type"] == "crystallize.from-suggestions"
    assert action["payload"]["intent_signature_hint"] == "hypermesh.write.automesh"
    assert action["payload"]["parameter_ranges"]["density"] == ["coarse", "fine"]

    suggestions_path = tmp_path / "state" / "suggestions" / "suggestions.jsonl"
    persisted = [json.loads(line) for line in suggestions_path.read_text().splitlines()]
    assert len(persisted) == 2


def test_suggestion_aggregator_triggers_on_single_runner_occurrences(tmp_path):
    from scripts.orchestrator.suggestion_aggregator import SuggestionAggregator

    emitted: list[dict] = []
    aggregator = SuggestionAggregator(
        state_root=tmp_path / "state",
        emit_cockpit_action=emitted.append,
        min_runners=3,
        min_occurrences_single_runner=2,
    )

    first = {
        "runner_profile": "runner-a",
        "intent_signature_hint": "zwcad.write.rooms",
        "raw_actions": ["draw room 1"],
    }
    second = {
        "runner_profile": "runner-a",
        "intent_signature_hint": "zwcad.write.rooms",
        "raw_actions": ["draw room 2"],
    }

    assert aggregator.on_suggestion(first)["status"] == "stored"
    assert aggregator.on_suggestion(second)["status"] == "triggered"
    assert emitted[0]["payload"]["trigger_reason"] == "single_runner_occurrences"
