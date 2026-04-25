from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_runner_subagent_message_is_preserved_and_formatted(tmp_path, monkeypatch):
    from scripts.daemon_http import DaemonHTTPServer
    from scripts.emerge_daemon import EmergeDaemon
    from scripts.watch_emerge import _format_event

    state_root = tmp_path / "state"
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(state_root))
    srv = DaemonHTTPServer(
        daemon=EmergeDaemon(root=ROOT),
        port=0,
        pid_path=tmp_path / "daemon.pid",
        state_root=state_root,
        event_root=tmp_path / "operator-events",
    )

    try:
        srv._on_runner_event(
            {
                "type": "runner_subagent_message",
                "message_id": "sub-1",
                "runner_profile": "cad1",
                "machine_id": "m1",
                "kind": "pattern_suggestion",
                "payload": {
                    "intent_signature_hint": "hypermesh.write.automesh",
                    "context_hint": "5 similar automesh commands",
                    "preferred_params": {"density": "coarse"},
                },
            }
        )

        events_path = state_root / "events" / "events-cad1.jsonl"
        events = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]
        assert events[-1]["type"] == "runner_subagent_message"
        assert events[-1]["kind"] == "pattern_suggestion"

        formatted = _format_event(events[-1])
        assert formatted is not None
        assert "[RunnerSubagent:cad1]" in formatted
        assert "hypermesh.write.automesh" in formatted
        assert "density=coarse" in formatted
    finally:
        srv.stop()
