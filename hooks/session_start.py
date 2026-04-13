from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.goal_control_plane import EVENT_HOOK_PAYLOAD, init_goal_control_plane  # noqa: E402
from scripts.policy_config import default_hook_state_root, pin_plugin_data_path_if_present  # noqa: E402
from scripts.state_tracker import load_tracker, save_tracker  # noqa: E402


def main() -> None:
    payload_text = sys.stdin.read().strip()
    try:
        payload = json.loads(payload_text) if payload_text else {}
    except Exception:
        payload = {}
    pin_plugin_data_path_if_present()
    state_root = Path(default_hook_state_root())
    state_path = state_root / "state.json"
    tracker = load_tracker(state_path)
    goal_cp = init_goal_control_plane(state_root, tracker)
    if "goal" in payload:
        goal_cp.ingest(
            event_type=EVENT_HOOK_PAYLOAD,
            source="hook_payload",
            actor="SessionStart",
            text=str(payload["goal"]),
            rationale="SessionStart hook payload goal",
            confidence=0.5,
        )
    # Always save on SessionStart: clear stale flywheel span from the previous session.
    tracker.state.pop("active_span_id", None)
    tracker.state.pop("active_span_intent", None)
    tracker.state.pop("turn_count", None)
    save_tracker(state_path, tracker)
    snap = goal_cp.read_snapshot()
    context_text = tracker.format_additional_context(
        goal_override=str(snap.get("text", "")),
        goal_source_override=str(snap.get("source", "unset")),
    )

    _SPAN_PROTOCOL = (
        "Span Protocol\n"
        "At the start of each user task that involves tool use, open a span: "
        'icc_span_open(intent_signature="connector.mode.name") '
        "→ execute all steps → icc_span_close(outcome=success|failure|aborted). "
        "Skip only for trivial one-off lookups (Read/Glob/Grep). "
        "Do NOT open sub-spans inside an active span — one span per top-level task. "
        "Repeated patterns auto-promote to zero-LLM pipelines."
    )
    context_text = _SPAN_PROTOCOL + "\n\n" + context_text

    conflicts_path = Path.home() / ".emerge" / "pending-conflicts.json"
    try:
        conflicts_data = json.loads(conflicts_path.read_text(encoding="utf-8"))
        pending = [c for c in conflicts_data.get("conflicts", []) if c.get("status") == "pending"]
        if pending:
            by_connector: dict[str, int] = {}
            for c in pending:
                connector = c.get("connector", "unknown")
                by_connector[connector] = by_connector.get(connector, 0) + 1
            connector_summary = ", ".join(
                f"{name} ({count} file{'s' if count != 1 else ''})"
                for name, count in sorted(by_connector.items())
            )
            context_text += (
                f"\n\n⚠️ Memory Hub has {len(pending)} unresolved sync conflict(s)."
                " Run /emerge:hub to resolve them.\n"
                f"Connectors affected: {connector_summary}"
            )
    except (OSError, json.JSONDecodeError, AttributeError):
        pass

    out = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context_text,
        }
    }
    print(json.dumps(out))


if __name__ == "__main__":
    main()
