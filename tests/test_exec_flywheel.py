import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.repl_daemon import ReplDaemon


def test_icc_exec_script_ref_mode_runs_file_with_args(tmp_path: Path):
    script = tmp_path / "double.py"
    script.write_text("print(__args['n'] * 2)\n", encoding="utf-8")

    os.environ["REPL_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["REPL_SESSION_ID"] = "flywheel"
    os.environ["REPL_SCRIPT_ROOTS"] = str(tmp_path)
    try:
        daemon = ReplDaemon(root=ROOT)
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
        os.environ.pop("REPL_STATE_ROOT", None)
        os.environ.pop("REPL_SESSION_ID", None)
        os.environ.pop("REPL_SCRIPT_ROOTS", None)


def test_icc_exec_script_ref_rejects_path_outside_allowlist(tmp_path: Path):
    outside = tmp_path / "outside.py"
    outside.write_text("print('x')\n", encoding="utf-8")
    os.environ["REPL_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["REPL_SESSION_ID"] = "flywheel"
    os.environ["REPL_SCRIPT_ROOTS"] = str(tmp_path / "allowed")
    try:
        daemon = ReplDaemon(root=ROOT)
        out = daemon.call_tool(
            "icc_exec",
            {"mode": "script_ref", "script_ref": str(outside)},
        )
        assert out["isError"] is True
        assert "outside allowed roots" in out["content"][0]["text"]
    finally:
        os.environ.pop("REPL_STATE_ROOT", None)
        os.environ.pop("REPL_SESSION_ID", None)
        os.environ.pop("REPL_SCRIPT_ROOTS", None)


def test_icc_exec_target_profiles_are_isolated(tmp_path: Path):
    os.environ["REPL_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["REPL_SESSION_ID"] = "flywheel"
    try:
        daemon = ReplDaemon(root=ROOT)
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
        os.environ.pop("REPL_STATE_ROOT", None)
        os.environ.pop("REPL_SESSION_ID", None)


def test_icc_exec_success_updates_candidate_registry(tmp_path: Path):
    os.environ["REPL_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["REPL_SESSION_ID"] = "flywheel"
    try:
        daemon = ReplDaemon(root=ROOT)
        out = daemon.call_tool(
            "icc_exec",
            {
                "mode": "inline_code",
                "code": "print('ok')",
                "target_profile": "mycader-1.zwcad",
                "intent_signature": "zwcad.add_wall",
                "script_ref": "connectors/cade/actions/zwcad_add_wall.py",
                "verify_passed": True,
            },
        )
        assert out.get("isError") is not True

        registry = (
            tmp_path
            / "state"
            / "flywheel"
            / "candidates.json"
        )
        data = json.loads(registry.read_text(encoding="utf-8"))
        key = "mycader-1.zwcad::zwcad.add_wall::connectors/cade/actions/zwcad_add_wall.py"
        assert key in data["candidates"]
        assert data["candidates"][key]["attempts"] == 1
        assert data["candidates"][key]["successes"] == 1
        assert data["candidates"][key]["verify_passes"] == 1
    finally:
        os.environ.pop("REPL_STATE_ROOT", None)
        os.environ.pop("REPL_SESSION_ID", None)


def test_auto_promotes_candidate_to_canary_when_thresholds_met(tmp_path: Path):
    os.environ["REPL_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["REPL_SESSION_ID"] = "flywheel"
    try:
        daemon = ReplDaemon(root=ROOT)
        for _ in range(20):
            out = daemon.call_tool(
                "icc_exec",
                {
                    "mode": "inline_code",
                    "code": "v = 1",
                    "target_profile": "mycader-1.zwcad",
                    "intent_signature": "zwcad.add_wall",
                    "script_ref": "connectors/cade/actions/zwcad_add_wall.py",
                    "verify_passed": True,
                },
            )
            assert out.get("isError") is not True

        reg = (
            tmp_path
            / "state"
            / "flywheel"
            / "pipelines-registry.json"
        )
        data = json.loads(reg.read_text(encoding="utf-8"))
        key = "mycader-1.zwcad::zwcad.add_wall::connectors/cade/actions/zwcad_add_wall.py"
        assert data["pipelines"][key]["status"] == "canary"
        assert data["pipelines"][key]["rollout_pct"] == 20
    finally:
        os.environ.pop("REPL_STATE_ROOT", None)
        os.environ.pop("REPL_SESSION_ID", None)


def test_auto_rolls_back_canary_on_two_consecutive_failures(tmp_path: Path):
    os.environ["REPL_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["REPL_SESSION_ID"] = "flywheel"
    try:
        daemon = ReplDaemon(root=ROOT)
        for _ in range(20):
            daemon.call_tool(
                "icc_exec",
                {
                    "mode": "inline_code",
                    "code": "x = 1",
                    "target_profile": "mycader-1.zwcad",
                    "intent_signature": "zwcad.add_wall",
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
                "intent_signature": "zwcad.add_wall",
                "script_ref": "connectors/cade/actions/zwcad_add_wall.py",
            },
        )
        daemon.call_tool(
            "icc_exec",
            {
                "mode": "inline_code",
                "code": "raise RuntimeError('f2')",
                "target_profile": "mycader-1.zwcad",
                "intent_signature": "zwcad.add_wall",
                "script_ref": "connectors/cade/actions/zwcad_add_wall.py",
            },
        )

        reg = (
            tmp_path
            / "state"
            / "flywheel"
            / "pipelines-registry.json"
        )
        data = json.loads(reg.read_text(encoding="utf-8"))
        key = "mycader-1.zwcad::zwcad.add_wall::connectors/cade/actions/zwcad_add_wall.py"
        assert data["pipelines"][key]["status"] == "explore"
        assert data["pipelines"][key]["last_transition_reason"] == "two_consecutive_failures"
    finally:
        os.environ.pop("REPL_STATE_ROOT", None)
        os.environ.pop("REPL_SESSION_ID", None)


def test_canary_sampling_progresses_to_stable(tmp_path: Path):
    os.environ["REPL_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["REPL_SESSION_ID"] = "flywheel"
    try:
        daemon = ReplDaemon(root=ROOT)
        for _ in range(20):
            daemon.call_tool(
                "icc_exec",
                {
                    "mode": "inline_code",
                    "code": "x = 1",
                    "target_profile": "mycader-1.zwcad",
                    "intent_signature": "zwcad.add_wall",
                    "script_ref": "connectors/cade/actions/zwcad_add_wall.py",
                    "verify_passed": True,
                },
            )
        for _ in range(140):
            daemon.call_tool(
                "icc_exec",
                {
                    "mode": "inline_code",
                    "code": "x = 1",
                    "target_profile": "mycader-1.zwcad",
                    "intent_signature": "zwcad.add_wall",
                    "script_ref": "connectors/cade/actions/zwcad_add_wall.py",
                    "verify_passed": True,
                },
            )
        reg = tmp_path / "state" / "flywheel" / "pipelines-registry.json"
        data = json.loads(reg.read_text(encoding="utf-8"))
        key = "mycader-1.zwcad::zwcad.add_wall::connectors/cade/actions/zwcad_add_wall.py"
        assert data["pipelines"][key]["status"] == "stable"
        assert data["pipelines"][key]["rollout_pct"] == 100
    finally:
        os.environ.pop("REPL_STATE_ROOT", None)
        os.environ.pop("REPL_SESSION_ID", None)


def test_stable_rolls_back_on_window_failure_rate(tmp_path: Path):
    os.environ["REPL_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["REPL_SESSION_ID"] = "flywheel"
    try:
        daemon = ReplDaemon(root=ROOT)
        common = {
            "mode": "inline_code",
            "target_profile": "mycader-1.zwcad",
            "intent_signature": "zwcad.add_wall",
            "script_ref": "connectors/cade/actions/zwcad_add_wall.py",
        }

        for _ in range(20):
            daemon.call_tool("icc_exec", {**common, "code": "x = 1", "verify_passed": True})
        for _ in range(140):
            daemon.call_tool("icc_exec", {**common, "code": "x = 1", "verify_passed": True})

        for i in range(20):
            if i % 2 == 0:
                daemon.call_tool("icc_exec", {**common, "code": "x = 1", "verify_passed": True})
            else:
                daemon.call_tool("icc_exec", {**common, "code": "raise RuntimeError('window-fail')"})

        reg = tmp_path / "state" / "flywheel" / "pipelines-registry.json"
        data = json.loads(reg.read_text(encoding="utf-8"))
        key = "mycader-1.zwcad::zwcad.add_wall::connectors/cade/actions/zwcad_add_wall.py"
        assert data["pipelines"][key]["status"] == "explore"
        assert data["pipelines"][key]["last_transition_reason"] == "window_failure_rate"
    finally:
        os.environ.pop("REPL_STATE_ROOT", None)
        os.environ.pop("REPL_SESSION_ID", None)


def test_degraded_exec_updates_failure_counters(tmp_path: Path):
    os.environ["REPL_STATE_ROOT"] = str(tmp_path / "state")
    os.environ["REPL_SESSION_ID"] = "flywheel"
    try:
        daemon = ReplDaemon(root=ROOT)
        key = "mycader-1.zwcad::zwcad.add_wall::connectors/cade/actions/zwcad_add_wall.py"
        common = {
            "mode": "inline_code",
            "code": "x = 1",
            "target_profile": "mycader-1.zwcad",
            "intent_signature": "zwcad.add_wall",
            "script_ref": "connectors/cade/actions/zwcad_add_wall.py",
            "verification_state": "degraded",
        }

        daemon.call_tool("icc_exec", common)
        daemon.call_tool("icc_exec", common)

        registry = tmp_path / "state" / "flywheel" / "candidates.json"
        data = json.loads(registry.read_text(encoding="utf-8"))
        assert data["candidates"][key]["degraded_count"] == 2
        assert data["candidates"][key]["consecutive_failures"] == 2
    finally:
        os.environ.pop("REPL_STATE_ROOT", None)
        os.environ.pop("REPL_SESSION_ID", None)
