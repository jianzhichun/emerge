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


def _write_connector_rules(cwd: str) -> None:
    """Generate .claude/rules/connector-<name>.md for each connector with NOTES.md.

    CC lazy-loads these files and fires InstructionsLoaded on each load, which
    injects the live NOTES.md content and reflection at exactly the right moment
    (not just once at SessionStart).
    """
    connectors_root = Path.home() / ".emerge" / "connectors"
    if not connectors_root.is_dir():
        return
    try:
        rules_dir = Path(cwd) / ".claude" / "rules"
        rules_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    for connector_dir in sorted(connectors_root.iterdir()):
        if not connector_dir.is_dir():
            continue
        notes_path = connector_dir / "NOTES.md"
        if not notes_path.exists():
            continue
        name = connector_dir.name
        try:
            excerpt = notes_path.read_text(encoding="utf-8").strip()[:400]
            content = (
                f"<!-- emerge:connector:{name} — auto-generated at SessionStart -->\n"
                f"# Connector: {name}\n\n"
                f"{excerpt}\n"
            )
            (rules_dir / f"connector-{name}.md").write_text(content, encoding="utf-8")
        except OSError:
            continue


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

    # Generate .claude/rules/connector-*.md for lazy connector context injection
    _write_connector_rules(payload.get("cwd") or str(Path.cwd()))

    # 确保 HTTP daemon 正在运行（幂等，已运行则无操作）
    import subprocess as _sub
    _plugin_root = Path(__file__).resolve().parents[1]
    try:
        _sub.Popen(
            [sys.executable,
             str(_plugin_root / "scripts" / "emerge_daemon.py"),
             "--ensure-running"],
            start_new_session=True,
            stdout=_sub.DEVNULL,
            stderr=_sub.DEVNULL,
        )
    except Exception:
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
