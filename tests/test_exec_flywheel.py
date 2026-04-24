import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.emerge_daemon import EmergeDaemon


def _registry_path(state_root: Path) -> Path:
    path = state_root / "registry" / "intents.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def test_icc_exec_script_ref_mode_runs_file_with_args(tmp_path: Path):
    script = tmp_path / "double.py"
    script.write_text("print(__args['n'] * 2)\n", encoding="utf-8")

    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_SESSION_ID"] = "flywheel"
    os.environ["EMERGE_SCRIPT_ROOTS"] = str(tmp_path)
    try:
        daemon = EmergeDaemon(root=ROOT)
        out = daemon.call_tool(
            "icc_exec",
            {
                "mode": "script_ref",
                "script_ref": str(script),
                "script_args": {"n": 4},
            },
        )
        assert out.get("isError") is not True
        assert "8" in out["content"][0]["text"]
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)
        os.environ.pop("EMERGE_SCRIPT_ROOTS", None)


def test_icc_exec_script_ref_rejects_path_outside_allowlist(tmp_path: Path):
    outside = tmp_path / "outside.py"
    outside.write_text("print('x')\n", encoding="utf-8")
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_SESSION_ID"] = "flywheel"
    os.environ["EMERGE_SCRIPT_ROOTS"] = str(tmp_path / "allowed")
    try:
        daemon = EmergeDaemon(root=ROOT)
        out = daemon.call_tool(
            "icc_exec",
            {"mode": "script_ref", "script_ref": str(outside)},
        )
        assert out["isError"] is True
        assert "outside allowed roots" in out["content"][0]["text"]
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)
        os.environ.pop("EMERGE_SCRIPT_ROOTS", None)


def test_icc_exec_target_profiles_are_isolated(tmp_path: Path):
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_SESSION_ID"] = "flywheel"
    try:
        daemon = EmergeDaemon(root=ROOT)
        daemon.call_tool(
            "icc_exec",
            {"code": "x = 5", "target_profile": "mycader-1.zwcad"},
        )
        isolated = daemon.call_tool(
            "icc_exec",
            {"code": "print(x)", "target_profile": "mytrader-1.xiadan"},
        )
        assert isolated["isError"] is True
        assert "NameError" in isolated["content"][0]["text"]
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_icc_exec_success_updates_candidate_registry(tmp_path: Path):
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_SESSION_ID"] = "flywheel"
    try:
        daemon = EmergeDaemon(root=ROOT)
        out = daemon.call_tool(
            "icc_exec",
            {
                "mode": "inline_code",
                "code": "print('ok')",
                "target_profile": "mycader-1.zwcad",
                "intent_signature": "zwcad.write.add-wall",
                "script_ref": "connectors/cade/actions/zwcad_add_wall.py",
                "verify_passed": False,
            },
        )
        assert out.get("isError") is not True

        registry = (
            tmp_path
            / "state"
            / "sessions"
            / "flywheel"
            / "candidates.json"
        )
        data = json.loads(registry.read_text(encoding="utf-8"))
        key = "zwcad.write.add-wall"
        assert key in data["candidates"]
        assert data["candidates"][key]["attempts"] == 1
        assert data["candidates"][key]["successes"] == 1
        assert data["candidates"][key]["verify_passes"] == 1
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_auto_promotes_candidate_to_canary_when_thresholds_met(tmp_path: Path):
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_SESSION_ID"] = "flywheel"
    try:
        daemon = EmergeDaemon(root=ROOT)
        # Run exactly PROMOTE_MIN_ATTEMPTS times to hit canary but not stable
        from scripts.policy_config import PROMOTE_MIN_ATTEMPTS
        for _ in range(PROMOTE_MIN_ATTEMPTS):
            out = daemon.call_tool(
                "icc_exec",
                {
                    "mode": "inline_code",
                    "code": "v = 1",
                    "target_profile": "mycader-1.zwcad",
                    "intent_signature": "zwcad.write.add-wall",
                    "script_ref": "connectors/cade/actions/zwcad_add_wall.py",
                    "verify_passed": True,
                },
            )
            assert out.get("isError") is not True

        reg = _registry_path(tmp_path / "state")
        data = json.loads(reg.read_text(encoding="utf-8"))
        key = "zwcad.write.add-wall"
        assert data["intents"][key]["stage"] == "canary"
        assert data["intents"][key]["rollout_pct"] == 20
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_auto_rolls_back_canary_on_two_consecutive_failures(tmp_path: Path):
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_SESSION_ID"] = "flywheel"
    try:
        from scripts.policy_config import PROMOTE_MIN_ATTEMPTS
        daemon = EmergeDaemon(root=ROOT)
        for _ in range(PROMOTE_MIN_ATTEMPTS):
            daemon.call_tool(
                "icc_exec",
                {
                    "mode": "inline_code",
                    "code": "x = 1",
                    "target_profile": "mycader-1.zwcad",
                    "intent_signature": "zwcad.write.add-wall",
                    "script_ref": "connectors/cade/actions/zwcad_add_wall.py",
                    "verify_passed": True,
                },
            )

        daemon.call_tool(
            "icc_exec",
            {
                "mode": "inline_code",
                "code": "raise RuntimeError('f1')",
                "target_profile": "mycader-1.zwcad",
                "intent_signature": "zwcad.write.add-wall",
                "script_ref": "connectors/cade/actions/zwcad_add_wall.py",
            },
        )
        daemon.call_tool(
            "icc_exec",
            {
                "mode": "inline_code",
                "code": "raise RuntimeError('f2')",
                "target_profile": "mycader-1.zwcad",
                "intent_signature": "zwcad.write.add-wall",
                "script_ref": "connectors/cade/actions/zwcad_add_wall.py",
            },
        )

        reg = _registry_path(tmp_path / "state")
        data = json.loads(reg.read_text(encoding="utf-8"))
        key = "zwcad.write.add-wall"
        assert data["intents"][key]["stage"] == "explore"
        assert data["intents"][key]["last_transition_reason"] == "two_consecutive_failures"
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_canary_sampling_progresses_to_stable(tmp_path: Path):
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_SESSION_ID"] = "flywheel"
    try:
        from scripts.policy_config import PROMOTE_MIN_ATTEMPTS, STABLE_MIN_ATTEMPTS
        daemon = EmergeDaemon(root=ROOT)
        for _ in range(PROMOTE_MIN_ATTEMPTS):
            daemon.call_tool(
                "icc_exec",
                {
                    "mode": "inline_code",
                    "code": "x = 1",
                    "target_profile": "mycader-1.zwcad",
                    "intent_signature": "zwcad.write.add-wall",
                    "script_ref": "connectors/cade/actions/zwcad_add_wall.py",
                    "verify_passed": True,
                },
            )
        # Run enough more to cross the stable threshold
        remaining = STABLE_MIN_ATTEMPTS - PROMOTE_MIN_ATTEMPTS + 1
        for _ in range(remaining):
            daemon.call_tool(
                "icc_exec",
                {
                    "mode": "inline_code",
                    "code": "x = 1",
                    "target_profile": "mycader-1.zwcad",
                    "intent_signature": "zwcad.write.add-wall",
                    "script_ref": "connectors/cade/actions/zwcad_add_wall.py",
                    "verify_passed": True,
                },
            )
        reg = _registry_path(tmp_path / "state")
        data = json.loads(reg.read_text(encoding="utf-8"))
        key = "zwcad.write.add-wall"
        assert data["intents"][key]["stage"] == "stable"
        assert data["intents"][key]["rollout_pct"] == 100
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_stable_rolls_back_on_window_failure_rate(tmp_path: Path):
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_SESSION_ID"] = "flywheel"
    try:
        from scripts.policy_config import PROMOTE_MIN_ATTEMPTS, STABLE_MIN_ATTEMPTS
        daemon = EmergeDaemon(root=ROOT)
        # Stable icc_exec normally bridges to a crystallized pipeline; this test
        # needs raw exec() outcomes (including raises) to drive window_failure_rate.
        daemon._try_flywheel_bridge = lambda _arguments: None  # type: ignore[method-assign]
        common = {
            "mode": "inline_code",
            "target_profile": "mycader-1.zwcad",
            "intent_signature": "zwcad.write.window-rate-test",
            "script_ref": "connectors/cade/actions/zwcad_window_rate_test.py",
        }

        for _ in range(PROMOTE_MIN_ATTEMPTS):
            daemon.call_tool("icc_exec", {**common, "code": "x = 1", "verify_passed": True})
        remaining = STABLE_MIN_ATTEMPTS - PROMOTE_MIN_ATTEMPTS + 1
        for _ in range(remaining):
            daemon.call_tool("icc_exec", {**common, "code": "x = 1", "verify_passed": True})

        for i in range(20):
            if i % 2 == 0:
                daemon.call_tool("icc_exec", {**common, "code": "x = 1", "verify_passed": True})
            else:
                daemon.call_tool("icc_exec", {**common, "code": "raise RuntimeError('window-fail')"})

        reg = _registry_path(tmp_path / "state")
        data = json.loads(reg.read_text(encoding="utf-8"))
        key = "zwcad.write.window-rate-test"
        assert data["intents"][key]["stage"] == "explore"
        assert data["intents"][key]["last_transition_reason"] == "window_failure_rate"
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_synthesis_ready_flag_set_on_canary_promotion(tmp_path):
    """synthesis_ready is set when an exec candidate reaches canary and WAL has code."""
    import json, os
    from pathlib import Path
    from scripts.emerge_daemon import EmergeDaemon
    from scripts.policy_config import PROMOTE_MIN_ATTEMPTS

    ROOT = Path(__file__).resolve().parents[1]
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_SESSION_ID"] = "synth-test"
    try:
        daemon = EmergeDaemon(root=ROOT)
        # Run enough successful icc_exec calls to cross the promote threshold
        for i in range(PROMOTE_MIN_ATTEMPTS):
            daemon.call_tool("icc_exec", {
                "code": f"__result = [{{'i': {i}}}]",
                "intent_signature": "test.read.synth",
                "no_replay": False,
            })
        registry_path = _registry_path(tmp_path / "state")
        assert registry_path.exists()
        data = json.loads(registry_path.read_text())
        entries = data.get("intents", {})
        synth_entry = next(
            (v for k, v in entries.items() if "test.read.synth" in k),
            None,
        )
        assert synth_entry is not None, "no registry entry found for test.read.synth"
        assert synth_entry.get("stage") == "canary", f"expected canary, got {synth_entry.get('stage')}"
        assert synth_entry.get("synthesis_ready") is True
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)


def test_exec_degraded_argument_is_not_trusted_for_policy_counters(tmp_path: Path):
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["EMERGE_SESSION_ID"] = "flywheel"
    try:
        daemon = EmergeDaemon(root=ROOT)
        key = "zwcad.write.add-wall"
        common = {
            "mode": "inline_code",
            "code": "x = 1",
            "target_profile": "mycader-1.zwcad",
            "intent_signature": "zwcad.write.add-wall",
            "script_ref": "connectors/cade/actions/zwcad_add_wall.py",
            "verification_state": "degraded",
        }

        daemon.call_tool("icc_exec", common)
        daemon.call_tool("icc_exec", common)

        registry = tmp_path / "state" / "sessions" / "flywheel" / "candidates.json"
        data = json.loads(registry.read_text(encoding="utf-8"))
        assert data["candidates"][key]["degraded_count"] == 0
        assert data["candidates"][key]["consecutive_failures"] == 0
    finally:
        os.environ.pop("EMERGE_STATE_ROOT", None)
        os.environ.pop("EMERGE_SESSION_ID", None)
