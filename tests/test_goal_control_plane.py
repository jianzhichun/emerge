import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.emerge_daemon import EmergeDaemon
from scripts.goal_control_plane import (
    EVENT_HUMAN_EDIT,
    EVENT_SYSTEM_GENERATE,
    GoalControlPlane,
)


def test_goal_control_ingest_and_human_lock_window(tmp_path: Path):
    cp = GoalControlPlane(tmp_path)
    human = cp.ingest(
        event_type=EVENT_HUMAN_EDIT,
        source="cockpit",
        actor="tester",
        text="manual goal",
        rationale="human sets target",
        confidence=1.0,
        lock_window_ms=60_000,
    )
    assert human["accepted"] is True
    snap = cp.read_snapshot()
    assert snap["text"] == "manual goal"
    assert snap["source"] == "cockpit"

    blocked = cp.ingest(
        event_type=EVENT_SYSTEM_GENERATE,
        source="system",
        actor="planner",
        text="system replacement",
        rationale="auto optimize",
        confidence=1.0,
    )
    assert blocked["accepted"] is False
    assert blocked["reason"] == "blocked_by_human_lock_window"
    assert cp.read_snapshot()["text"] == "manual goal"


def test_goal_control_rollback_to_previous_event(tmp_path: Path):
    cp = GoalControlPlane(tmp_path)
    e1 = cp.ingest(
        event_type=EVENT_HUMAN_EDIT,
        source="cockpit",
        actor="tester",
        text="first goal",
        rationale="first",
        confidence=1.0,
        force=True,
    )
    cp.ingest(
        event_type=EVENT_HUMAN_EDIT,
        source="cockpit",
        actor="tester",
        text="second goal",
        rationale="second",
        confidence=1.0,
        force=True,
    )
    rolled = cp.rollback(target_event_id=e1["event_id"], actor="tester")
    assert rolled["accepted"] is True
    assert cp.read_snapshot()["text"] == "first goal"


def test_goal_control_accepts_high_context_risk_system_refine(tmp_path: Path):
    cp = GoalControlPlane(tmp_path)
    cp.ingest(
        event_type=EVENT_HUMAN_EDIT,
        source="hook_payload",
        actor="seed",
        text="generic goal",
        rationale="seed",
        confidence=0.3,
        force=True,
    )
    out = cp.ingest(
        event_type=EVENT_SYSTEM_GENERATE,
        source="system",
        actor="planner",
        text="high-risk focused goal",
        rationale="recent failures point to this",
        confidence=0.9,
        context_match_score=1.0,
        recent_failure_risk=1.0,
    )
    assert out["accepted"] is True
    assert out["snapshot"]["text"] == "high-risk focused goal"
    assert out["decision"]["breakdown"]["candidate_context_match"] == 1.0
    assert out["decision"]["breakdown"]["candidate_recent_failure_risk"] == 1.0


def test_daemon_goal_tools_and_resources(tmp_path: Path):
    os.environ["CLAUDE_PLUGIN_DATA"] = str(tmp_path / "hook-state")
    try:
        daemon = EmergeDaemon(root=ROOT)
        ingest = daemon.call_tool(
            "icc_goal_ingest",
            {
                "event_type": "system_refine",
                "source": "system",
                "actor": "daemon-test",
                "text": "system goal from daemon",
                "rationale": "integration test",
                "confidence": 0.9,
                "force": True,
            },
        )
        assert ingest["isError"] is False
        payload = json.loads(ingest["content"][0]["text"])
        assert payload["snapshot"]["text"] == "system goal from daemon"

        read = daemon.call_tool("icc_goal_read", {"limit": 5})
        assert read["isError"] is False
        read_payload = json.loads(read["content"][0]["text"])
        assert read_payload["snapshot"]["text"] == "system goal from daemon"
        assert isinstance(read_payload["events"], list)

        resource = daemon._read_resource("state://goal")
        parsed = json.loads(resource["text"])
        assert parsed["text"] == "system goal from daemon"
    finally:
        os.environ.pop("CLAUDE_PLUGIN_DATA", None)


def test_file_lock_acquires_and_releases(tmp_path):
    """Basic smoke test: _file_lock acquires without error and releases."""
    from scripts.goal_control_plane import _file_lock
    lock_path = tmp_path / ".test.lock"
    acquired = False
    with _file_lock(lock_path, timeout_ms=500):
        acquired = True
    assert acquired


def test_file_lock_works_without_fcntl(tmp_path, monkeypatch):
    """_file_lock must not hard-crash if fcntl is unavailable (simulates Windows)."""
    import sys
    # Setting sys.modules["fcntl"] = None causes 'import fcntl' to raise ImportError
    # This simulates a Windows environment where fcntl doesn't exist.
    monkeypatch.setitem(sys.modules, "fcntl", None)  # type: ignore[arg-type]

    from scripts.goal_control_plane import _file_lock
    lock_path = tmp_path / ".test2.lock"
    acquired = False
    with _file_lock(lock_path, timeout_ms=500):
        acquired = True

    assert acquired, "_file_lock must work even when fcntl is unavailable"
