from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_emit_event_persists_failed_delivery_to_outbox(tmp_path, monkeypatch):
    import urllib.error

    import scripts.runner_emit as runner_emit

    monkeypatch.setenv("EMERGE_RUNNER_OUTBOX", str(tmp_path / "outbox.jsonl"))
    monkeypatch.setenv("EMERGE_TEAM_LEAD_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("EMERGE_RUNNER_PROFILE", "runner-a")
    monkeypatch.setattr(
        runner_emit.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(urllib.error.URLError("down")),
    )

    assert runner_emit.emit_event({"type": "evidence_report", "intent_signature": "foo.write.bar"}) is False

    rows = [json.loads(line) for line in (tmp_path / "outbox.jsonl").read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["message_id"]
    assert rows[0]["runner_profile"] == "runner-a"


def test_flush_outbox_sends_successes_and_retains_failures(tmp_path, monkeypatch):
    import scripts.runner_emit as runner_emit

    outbox = tmp_path / "outbox.jsonl"
    monkeypatch.setenv("EMERGE_RUNNER_OUTBOX", str(outbox))
    events = [
        {"type": "evidence_report", "message_id": "ok-1", "runner_profile": "r"},
        {"type": "evidence_report", "message_id": "fail-1", "runner_profile": "r"},
        {"type": "evidence_report", "message_id": "ok-2", "runner_profile": "r"},
    ]
    outbox.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")

    sent: list[str] = []

    def _send(event, **_kwargs):
        sent.append(event["message_id"])
        return event["message_id"] != "fail-1"

    monkeypatch.setattr(runner_emit, "emit_event_raw", _send)

    result = runner_emit.flush_outbox_once()

    assert result == {"attempted": 3, "sent": 2, "retained": 1}
    assert sent == ["ok-1", "fail-1", "ok-2"]
    retained = [json.loads(line) for line in outbox.read_text().splitlines()]
    assert [row["message_id"] for row in retained] == ["fail-1"]
