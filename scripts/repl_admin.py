from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.policy_config import (
    PROMOTE_MAX_HUMAN_FIX_RATE,
    PROMOTE_MIN_ATTEMPTS,
    PROMOTE_MIN_SUCCESS_RATE,
    PROMOTE_MIN_VERIFY_RATE,
    ROLLBACK_CONSECUTIVE_FAILURES,
    STABLE_MIN_ATTEMPTS,
    STABLE_MIN_SUCCESS_RATE,
    STABLE_MIN_VERIFY_RATE,
    derive_profile_token,
    derive_session_id,
    default_hook_state_root,
    default_repl_root,
)


def _resolve_state_root() -> Path:
    return Path(os.environ.get("REPL_STATE_ROOT", str(default_repl_root()))).expanduser().resolve()


def _resolve_session_id() -> str:
    return derive_session_id(os.environ.get("REPL_SESSION_ID"), ROOT)


def _session_paths() -> tuple[Path, Path, Path]:
    state_root = _resolve_state_root()
    session_id = _resolve_session_id()
    target_profile = str(os.environ.get("REPL_TARGET_PROFILE", "default")).strip() or "default"
    if target_profile != "default":
        profile_key = derive_profile_token(target_profile)
        session_id = f"{session_id}__{profile_key}"
    session_dir = state_root / session_id
    return session_dir, session_dir / "wal.jsonl", session_dir / "checkpoint.json"


def _load_hook_state_summary() -> dict[str, str]:
    state_path = Path(
        os.environ.get("CLAUDE_PLUGIN_DATA", str(default_hook_state_root()))
    ) / "state.json"
    if not state_path.exists():
        return {"goal": "", "goal_source": "unset"}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return {"goal": "", "goal_source": "unset"}
    if not isinstance(data, dict):
        return {"goal": "", "goal_source": "unset"}
    goal = str(data.get("goal", "") or "")
    goal_source = str(data.get("goal_source", "unset") or "unset")
    return {"goal": goal, "goal_source": goal_source}


def cmd_status() -> dict:
    session_dir, wal_path, checkpoint_path = _session_paths()
    wal_entries = 0
    if wal_path.exists():
        with wal_path.open("r", encoding="utf-8") as f:
            wal_entries = sum(1 for line in f if line.strip())
    return {
        "session_id": _resolve_session_id(),
        "state_root": str(_resolve_state_root()),
        "session_dir": str(session_dir),
        "wal_exists": wal_path.exists(),
        "wal_entries": wal_entries,
        "checkpoint_exists": checkpoint_path.exists(),
    }


def cmd_clear() -> dict:
    session_dir, _, _ = _session_paths()
    existed = session_dir.exists()
    if existed:
        shutil.rmtree(session_dir)
    return {
        "session_id": _resolve_session_id(),
        "session_dir": str(session_dir),
        "cleared": True,
        "existed": existed,
    }


def cmd_policy_status() -> dict:
    session_dir, _, _ = _session_paths()
    registry_path = session_dir / "pipelines-registry.json"
    pipelines = []
    registry_corrupt = False
    if registry_path.exists():
        try:
            data = json.loads(registry_path.read_text(encoding="utf-8"))
        except Exception:
            data = {"pipelines": {}}
            registry_corrupt = True
        raw = data.get("pipelines", {})
        if isinstance(raw, dict):
            for key, value in raw.items():
                if not isinstance(value, dict):
                    continue
                item = {"key": key, **value}
                pipelines.append(item)
    pipelines.sort(key=lambda x: (str(x.get("status", "")), str(x.get("key", ""))))
    hook_summary = _load_hook_state_summary()
    return {
        "session_id": _resolve_session_id(),
        "state_root": str(_resolve_state_root()),
        "registry_exists": registry_path.exists(),
        "registry_corrupt": registry_corrupt,
        "goal": hook_summary["goal"],
        "goal_source": hook_summary["goal_source"],
        "pipeline_count": len(pipelines),
        "thresholds": {
            "promote_min_attempts": PROMOTE_MIN_ATTEMPTS,
            "promote_min_success_rate": PROMOTE_MIN_SUCCESS_RATE,
            "promote_min_verify_rate": PROMOTE_MIN_VERIFY_RATE,
            "promote_max_human_fix_rate": PROMOTE_MAX_HUMAN_FIX_RATE,
            "stable_min_attempts": STABLE_MIN_ATTEMPTS,
            "stable_min_success_rate": STABLE_MIN_SUCCESS_RATE,
            "stable_min_verify_rate": STABLE_MIN_VERIFY_RATE,
            "rollback_consecutive_failures": ROLLBACK_CONSECUTIVE_FAILURES,
        },
        "pipelines": pipelines,
    }


def render_policy_status_pretty(data: dict) -> str:
    lines: list[str] = []
    lines.append(f"Session: {data.get('session_id', '')}")
    lines.append(f"State root: {data.get('state_root', '')}")
    lines.append(f"Goal: {data.get('goal', '')}")
    lines.append(f"Goal source: {data.get('goal_source', 'unset')}")
    lines.append("")
    lines.append("Thresholds:")
    thresholds = data.get("thresholds", {})
    for key in sorted(thresholds.keys()):
        lines.append(f"- {key}: {thresholds[key]}")
    lines.append("")
    lines.append("Pipelines:")
    pipelines = data.get("pipelines", [])
    if not pipelines:
        lines.append("- (none)")
    else:
        for item in pipelines:
            lines.append(f"- key: {item.get('key', '')}")
            lines.append(f"  status: {item.get('status', '')}")
            lines.append(f"  rollout_pct: {item.get('rollout_pct', 0)}")
            lines.append(f"  success_rate: {item.get('success_rate', 0)}")
            lines.append(f"  verify_rate: {item.get('verify_rate', 0)}")
            lines.append(f"  human_fix_rate: {item.get('human_fix_rate', 0)}")
            lines.append(f"  consecutive_failures: {item.get('consecutive_failures', 0)}")
            lines.append(f"  policy_enforced_count: {item.get('policy_enforced_count', 0)}")
            lines.append(f"  stop_triggered_count: {item.get('stop_triggered_count', 0)}")
            lines.append(f"  rollback_executed_count: {item.get('rollback_executed_count', 0)}")
            lines.append(f"  last_policy_action: {item.get('last_policy_action', 'none')}")
            lines.append(f"  transition_reason: {item.get('last_transition_reason', '')}")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Local REPL state admin utility")
    parser.add_argument("command", choices=["status", "clear", "policy-status"])
    parser.add_argument("--pretty", action="store_true", help="Render human-readable output")
    args = parser.parse_args()

    if args.command == "status":
        out = cmd_status()
    elif args.command == "policy-status":
        out = cmd_policy_status()
    else:
        out = cmd_clear()

    if args.pretty and args.command == "policy-status":
        print(render_policy_status_pretty(out), end="")
    else:
        print(json.dumps(out))


if __name__ == "__main__":
    main()
