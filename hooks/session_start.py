from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.policy_config import default_hook_state_root  # noqa: E402
from scripts.state_tracker import with_locked_tracker  # noqa: E402


def _hook_copy(name: str, fallback: str) -> str:
    path = ROOT / "docs" / "hooks" / name
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return fallback
    return text or fallback


def _compact_connector_index(max_chars: int = 200) -> str:
    """Return a compact one-line connector index for startup context."""
    connectors_root = Path.home() / ".emerge" / "connectors"
    if not connectors_root.is_dir():
        return ""

    items: list[str] = []
    for connector_dir in sorted(connectors_root.iterdir()):
        if not connector_dir.is_dir():
            continue
        name = connector_dir.name
        notes_path = connector_dir / "NOTES.md"
        if not notes_path.exists():
            items.append(name)
            continue
        try:
            raw = " ".join(notes_path.read_text(encoding="utf-8").split())
        except OSError:
            items.append(name)
            continue
        preview = raw[:60].strip()
        items.append(f"{name} ({preview})" if preview else name)

    if not items:
        return ""

    prefix = "Available connectors: "
    remaining = max_chars - len(prefix)
    if remaining <= 0:
        return ""

    compact: list[str] = []
    used = 0
    for item in items:
        add_len = len(item) + (2 if compact else 0)
        if used + add_len > remaining:
            break
        compact.append(item)
        used += add_len

    if not compact:
        return ""

    hidden = len(items) - len(compact)
    suffix = f", +{hidden} more" if hidden > 0 else ""
    return prefix + ", ".join(compact) + suffix


def main() -> None:
    payload_text = sys.stdin.read().strip()
    state_root = Path(default_hook_state_root())
    state_path = state_root / "state.json"
    def _mutate(tracker):
        # Always save on SessionStart: clear stale flywheel span from the previous session.
        tracker.state.pop("active_span_id", None)
        tracker.state.pop("active_span_intent", None)
        tracker.state.pop("turn_count", None)
        return tracker.format_additional_context()

    context_text = with_locked_tracker(state_path, _mutate)
    # Reset span nudge marker so the new session gets its own nudges.
    (state_root / "span-nudge-sent").unlink(missing_ok=True)
    # Stale the deep reflection cache so changes to filters/thresholds take
    # effect on turn 1 rather than waiting for the 15-minute TTL to expire.
    (state_root / "reflection-cache" / "global.json").unlink(missing_ok=True)
    _SPAN_PROTOCOL = _hook_copy(
        "span_protocol.md",
        "Span Protocol\n"
        "At the start of each user task that involves tool use, open a span: "
        'icc_span_open(intent_signature=\"connector.mode.name\") '
        "-> execute all steps -> icc_span_close(outcome=success|failure|aborted). "
        "Skip only for trivial one-off lookups (Read/Glob/Grep). "
        "Do NOT open sub-spans inside an active span - one span per top-level task. "
        "Repeated patterns auto-promote to zero-LLM pipelines.",
    )
    context_text = _SPAN_PROTOCOL + "\n\n" + context_text

    connector_index = _compact_connector_index(max_chars=200)
    if connector_index:
        context_text += "\n\n" + connector_index

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
