from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.goal_control_plane import init_goal_control_plane  # noqa: E402
from scripts.policy_config import REFLECTION_CACHE_TTL_MS, default_exec_root, default_hook_state_root, pin_plugin_data_path_if_present  # noqa: E402
from scripts.span_tracker import SpanTracker  # noqa: E402
from scripts.state_tracker import load_tracker  # noqa: E402

_BUDGET_CHARS = 800


def main() -> None:
    sys.stdin.read()  # consume stdin (unused by PostCompact)

    pin_plugin_data_path_if_present()
    state_root = Path(default_hook_state_root())
    state_path = state_root / "state.json"

    # PreCompact already reset the tracker. Load the fresh (empty) state.
    tracker = load_tracker(state_path)
    goal_cp = init_goal_control_plane(state_root, tracker)
    snap = goal_cp.read_snapshot()

    token = tracker.format_recovery_token(
        budget_chars=_BUDGET_CHARS,
        goal_override=str(snap.get("text", "")),
        goal_source_override=str(snap.get("source", "unset")),
    )
    token_json = json.dumps(token, ensure_ascii=True, separators=(",", ":"))

    exec_root = Path(os.environ.get("EMERGE_STATE_ROOT", str(default_exec_root())))
    reflection = SpanTracker(
        state_root=exec_root,
        hook_state_root=state_root,
    ).format_reflection_with_cache(cache_ttl_ms=REFLECTION_CACHE_TTL_MS)
    reflection_block = f"{reflection}\n\n" if reflection else ""

    _SPAN_PROTOCOL = (
        "Span Protocol\n"
        "At the start of each user task that involves tool use, open a span: "
        'icc_span_open(intent_signature="connector.mode.name") '
        "→ execute all steps → icc_span_close(outcome=success|failure|aborted). "
        "Skip only for trivial one-off lookups (Read/Glob/Grep). "
        "Do NOT open sub-spans inside an active span — one span per top-level task. "
        "Repeated patterns auto-promote to zero-LLM pipelines."
    )

    context_text = (
        "[PostCompact] Context compacted. State reset to clean baseline.\n\n"
        + _SPAN_PROTOCOL + "\n\n"
        + reflection_block
        + f"Goal\n{str(snap.get('text', '')) or 'Not set.'}\n\n"
        "Open Risks\n- None.\n\n"
        f"FLYWHEEL_TOKEN\n{token_json}"
    )

    # PostCompact uses top-level systemMessage (not hookSpecificOutput)
    out = {"systemMessage": context_text}
    print(json.dumps(out))


if __name__ == "__main__":
    main()
