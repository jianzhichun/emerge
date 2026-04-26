from __future__ import annotations

import json
import inspect
import sys
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_suggestion_aggregator_dedupes_persists_and_emits_fact(tmp_path):
    from scripts.orchestrator.suggestion_aggregator import SuggestionAggregator

    emitted: list[dict] = []
    aggregator = SuggestionAggregator(
        state_root=tmp_path / "state",
        emit_cockpit_action=emitted.append,
    )

    base = {
        "intent_signature_hint": "mock.write.automate",
        "raw_actions": ["do thing"],
        "context_hint": "repeated operation",
        "preferred_params": {"level": "coarse"},
    }

    assert aggregator.on_suggestion({**base, "runner_profile": "runner-a"})["status"] == "stored"
    assert aggregator.on_suggestion({**base, "runner_profile": "runner-a"})["status"] == "duplicate"
    assert aggregator.on_suggestion(
        {
            **base,
            "runner_profile": "runner-b",
            "preferred_params": {"level": "fine"},
            "raw_actions": ["do thing differently"],
        }
    )["status"] == "stored"

    assert [event["type"] for event in emitted] == ["pattern_aggregated", "pattern_aggregated"]
    assert emitted[-1]["payload"]["intent_signature_hint"] == "mock.write.automate"
    assert emitted[-1]["payload"]["runner_profiles"] == ["runner-a", "runner-b"]
    assert emitted[-1]["payload"]["suggestion_count"] == 2
    assert emitted[-1]["payload"]["parameter_ranges"]["level"] == ["coarse", "fine"]

    suggestions_path = tmp_path / "state" / "suggestions" / "suggestions.jsonl"
    persisted = [json.loads(line) for line in suggestions_path.read_text().splitlines()]
    assert len(persisted) == 2
    facts_path = tmp_path / "state" / "suggestions" / "aggregated.jsonl"
    facts = [json.loads(line) for line in facts_path.read_text().splitlines()]
    assert [fact["type"] for fact in facts] == ["pattern_aggregated", "pattern_aggregated"]


def test_suggestion_aggregator_constructor_exposes_only_mechanism_dependencies():
    from scripts.orchestrator.suggestion_aggregator import SuggestionAggregator

    params = inspect.signature(SuggestionAggregator).parameters
    assert list(params) == ["state_root", "emit_cockpit_action"]


def test_suggestion_aggregator_replays_persisted_suggestions_without_triggering(tmp_path):
    from scripts.orchestrator.suggestion_aggregator import SuggestionAggregator

    state_root = tmp_path / "state"
    first = SuggestionAggregator(
        state_root=state_root,
        emit_cockpit_action=lambda _action: None,
    )
    first.on_suggestion({"runner_profile": "a", "intent_signature_hint": "foo.write.bar", "raw_actions": ["a"]})
    first.on_suggestion({"runner_profile": "b", "intent_signature_hint": "foo.write.bar", "raw_actions": ["b"]})

    emitted: list[dict] = []
    second = SuggestionAggregator(
        state_root=state_root,
        emit_cockpit_action=emitted.append,
    )
    result = second.on_suggestion(
        {"runner_profile": "c", "intent_signature_hint": "foo.write.bar", "raw_actions": ["c"]}
    )

    assert result["status"] == "stored"
    assert len(emitted) == 1
    assert emitted[0]["type"] == "pattern_aggregated"
    assert emitted[0]["payload"]["runner_profiles"] == ["a", "b", "c"]


def test_suggestion_aggregator_concurrent_duplicate_is_stored_once(tmp_path):
    from scripts.orchestrator.suggestion_aggregator import SuggestionAggregator

    aggregator = SuggestionAggregator(
        state_root=tmp_path / "state",
        emit_cockpit_action=lambda _action: None,
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
