from __future__ import annotations

import json
import sys
import threading
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


def test_suggestion_aggregator_replays_persisted_suggestions(tmp_path):
    from scripts.orchestrator.suggestion_aggregator import SuggestionAggregator

    state_root = tmp_path / "state"
    first = SuggestionAggregator(
        state_root=state_root,
        emit_cockpit_action=lambda _action: None,
        min_runners=3,
    )
    first.on_suggestion({"runner_profile": "a", "intent_signature_hint": "foo.write.bar", "raw_actions": ["a"]})
    first.on_suggestion({"runner_profile": "b", "intent_signature_hint": "foo.write.bar", "raw_actions": ["b"]})

    emitted: list[dict] = []
    second = SuggestionAggregator(
        state_root=state_root,
        emit_cockpit_action=emitted.append,
        min_runners=3,
    )
    result = second.on_suggestion(
        {"runner_profile": "c", "intent_signature_hint": "foo.write.bar", "raw_actions": ["c"]}
    )

    assert result["status"] == "triggered"
    assert len(emitted) == 1
    assert emitted[0]["payload"]["runner_profiles"] == ["a", "b", "c"]


def test_suggestion_aggregator_retriggers_after_new_evidence_threshold(tmp_path):
    from scripts.orchestrator.suggestion_aggregator import SuggestionAggregator

    emitted: list[dict] = []
    aggregator = SuggestionAggregator(
        state_root=tmp_path / "state",
        emit_cockpit_action=emitted.append,
        min_runners=1,
        min_occurrences_single_runner=1,
        retrigger_min_new_evidence=3,
    )

    assert aggregator.on_suggestion(
        {"runner_profile": "a", "intent_signature_hint": "foo.write.bar", "raw_actions": ["1"]}
    )["status"] == "triggered"
    assert aggregator.on_suggestion(
        {"runner_profile": "a", "intent_signature_hint": "foo.write.bar", "raw_actions": ["2"]}
    )["status"] == "stored"
    assert aggregator.on_suggestion(
        {"runner_profile": "a", "intent_signature_hint": "foo.write.bar", "raw_actions": ["3"]}
    )["status"] == "stored"
    assert aggregator.on_suggestion(
        {"runner_profile": "a", "intent_signature_hint": "foo.write.bar", "raw_actions": ["4"]}
    )["status"] == "triggered"
    assert len(emitted) == 2


def test_suggestion_aggregator_concurrent_duplicate_is_stored_once(tmp_path):
    from scripts.orchestrator.suggestion_aggregator import SuggestionAggregator

    aggregator = SuggestionAggregator(
        state_root=tmp_path / "state",
        emit_cockpit_action=lambda _action: None,
        min_runners=2,
    )
    suggestion = {
        "runner_profile": "runner-a",
        "intent_signature_hint": "foo.write.bar",
        "raw_actions": ["same"],
    }
    barrier = threading.Barrier(2)
    statuses: list[str] = []

    def _submit() -> None:
        barrier.wait(timeout=5)
        statuses.append(aggregator.on_suggestion(dict(suggestion))["status"])

    threads = [threading.Thread(target=_submit), threading.Thread(target=_submit)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert sorted(statuses) == ["duplicate", "stored"]
    suggestions_path = tmp_path / "state" / "suggestions" / "suggestions.jsonl"
    assert len(suggestions_path.read_text().splitlines()) == 1
