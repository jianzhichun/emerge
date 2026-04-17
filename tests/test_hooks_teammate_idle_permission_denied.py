# tests/test_hooks_teammate_idle_permission_denied.py
"""Tests for teammate_idle.py and permission_denied.py hooks."""
from __future__ import annotations
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEAMMATE_IDLE_HOOK = ROOT / "hooks" / "teammate_idle.py"
PERMISSION_DENIED_HOOK = ROOT / "hooks" / "permission_denied.py"


def _run(script: Path, payload: dict, data_dir: Path):
    """Run a hook script with JSON payload on stdin. Returns (returncode, stdout, stderr)."""
    env = {**os.environ, "EMERGE_HOOK_STATE_ROOT": str(data_dir)}
    result = subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


# ---------------------------------------------------------------------------
# TeammateIdle tests
# ---------------------------------------------------------------------------

def test_teammate_idle_exits_2_for_watcher_in_monitors_team(tmp_path):
    """A *-watcher in emerge-monitors must get exit 2 to keep it alive."""
    rc, out, err = _run(
        TEAMMATE_IDLE_HOOK,
        {"hook_event_name": "TeammateIdle",
         "team_name": "emerge-monitors",
         "teammate_name": "mycader-1-watcher"},
        tmp_path,
    )
    assert rc == 2
    assert "monitor" in err.lower()


def test_teammate_idle_feedback_mentions_watch_emerge(tmp_path):
    """Feedback message must tell the agent to restart watch_emerge Monitor."""
    rc, out, err = _run(
        TEAMMATE_IDLE_HOOK,
        {"hook_event_name": "TeammateIdle",
         "team_name": "emerge-monitors",
         "teammate_name": "profile-abc-watcher"},
        tmp_path,
    )
    assert rc == 2
    assert "watch_emerge" in err
    assert "profile-abc" in err  # runner-profile correctly stripped from teammate_name


def test_teammate_idle_allows_other_teams(tmp_path):
    """Non-monitors team must be allowed to go idle (exit 0, empty JSON)."""
    rc, out, err = _run(
        TEAMMATE_IDLE_HOOK,
        {"hook_event_name": "TeammateIdle",
         "team_name": "superpowers",
         "teammate_name": "researcher"},
        tmp_path,
    )
    assert rc == 0
    assert json.loads(out) == {}


def test_teammate_idle_allows_non_watcher_in_monitors_team(tmp_path):
    """A non-watcher agent in emerge-monitors (e.g. team-lead) may go idle."""
    rc, out, err = _run(
        TEAMMATE_IDLE_HOOK,
        {"hook_event_name": "TeammateIdle",
         "team_name": "emerge-monitors",
         "teammate_name": "team-lead"},
        tmp_path,
    )
    assert rc == 0
    assert json.loads(out) == {}


def test_teammate_idle_empty_payload_is_safe(tmp_path):
    """Empty payload (no team_name / teammate_name) must not crash; exit 0."""
    rc, out, err = _run(TEAMMATE_IDLE_HOOK, {}, tmp_path)
    assert rc == 0
    assert json.loads(out) == {}


# ---------------------------------------------------------------------------
# PermissionDenied tests
# ---------------------------------------------------------------------------

def test_permission_denied_retry_for_icc_exec(tmp_path):
    """icc_exec denied → retry: true."""
    rc, out, err = _run(
        PERMISSION_DENIED_HOOK,
        {"hook_event_name": "PermissionDenied",
         "tool_name": "mcp__plugin_emerge_emerge__icc_exec"},
        tmp_path,
    )
    assert rc == 0
    assert json.loads(out).get("retry") is True


def test_permission_denied_retry_for_icc_span_open(tmp_path):
    """icc_span_open denied → retry: true."""
    rc, out, err = _run(
        PERMISSION_DENIED_HOOK,
        {"hook_event_name": "PermissionDenied",
         "tool_name": "mcp__plugin_emerge_emerge__icc_span_open"},
        tmp_path,
    )
    assert rc == 0
    assert json.loads(out).get("retry") is True


def test_permission_denied_retry_for_any_icc_variant(tmp_path):
    """Any icc_* tool from any emerge plugin variant → retry: true."""
    rc, out, err = _run(
        PERMISSION_DENIED_HOOK,
        {"hook_event_name": "PermissionDenied",
         "tool_name": "mcp__plugin_test_emerge_test__icc_crystallize"},
        tmp_path,
    )
    assert rc == 0
    assert json.loads(out).get("retry") is True


def test_permission_denied_no_retry_for_bash(tmp_path):
    """Bash denied → no retry opinion (empty dict)."""
    rc, out, err = _run(
        PERMISSION_DENIED_HOOK,
        {"hook_event_name": "PermissionDenied",
         "tool_name": "Bash"},
        tmp_path,
    )
    assert rc == 0
    assert json.loads(out).get("retry") is not True


def test_permission_denied_retry_for_icc_hub(tmp_path):
    """icc_hub IS an icc_ tool — expect retry: true."""
    rc, out, err = _run(
        PERMISSION_DENIED_HOOK,
        {"hook_event_name": "PermissionDenied",
         "tool_name": "mcp__plugin_emerge_emerge__icc_hub"},
        tmp_path,
    )
    assert rc == 0
    assert json.loads(out).get("retry") is True


def test_permission_denied_empty_payload_is_safe(tmp_path):
    """Empty payload must not crash; no retry."""
    rc, out, err = _run(PERMISSION_DENIED_HOOK, {}, tmp_path)
    assert rc == 0
    assert json.loads(out).get("retry") is not True
