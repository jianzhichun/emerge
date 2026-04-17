from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.policy_config import REFLECTION_CACHE_TTL_MS, default_state_root, default_hook_state_root  # noqa: E402
from scripts.span_tracker import SpanTracker  # noqa: E402
from scripts.state_tracker import StateTracker, load_tracker, save_tracker  # noqa: E402

_BUDGET_CHARS = 800


def main() -> None:
    sys.stdin.read()  # consume stdin (unused by PreCompact)

    state_root = Path(default_hook_state_root())
    state_path = state_root / "state.json"
    tracker = load_tracker(state_path)

    token = tracker.format_recovery_token(budget_chars=_BUDGET_CHARS)
    token_json = json.dumps(token, ensure_ascii=True, separators=(",", ":"))
    _SPAN_PROTOCOL = (
        "Span Protocol\n"
        "At the start of each user task that involves tool use, open a span: "
        'icc_span_open(intent_signature="connector.mode.name") '
        "→ execute all steps → icc_span_close(outcome=success|failure|aborted). "
        "Skip only for trivial one-off lookups (Read/Glob/Grep). "
        "Do NOT open sub-spans inside an active span — one span per top-level task. "
        "Repeated patterns auto-promote to zero-LLM pipelines."
    )
    span_line = ""
    if tracker.state.get("active_span_id"):
        sid = tracker.state["active_span_id"]
        sint = tracker.state.get("active_span_intent", "")
        span_line = f"\nActive span: {sid} ({sint}) -- call icc_span_close when done."
    exec_root = Path(os.environ.get("EMERGE_STATE_ROOT", str(default_state_root())))
    reflection = SpanTracker(
        state_root=exec_root,
        hook_state_root=state_root,
    ).format_reflection_with_cache(cache_ttl_ms=REFLECTION_CACHE_TTL_MS)
    reflection_block = f"{reflection}\n\n" if reflection else ""

    context_text = (
        _SPAN_PROTOCOL + span_line + "\n\n" + reflection_block
        + f"Open Risks\n"
        + ("\n".join(f"- {r}" for r in token.get("open_risks", [])) or "- None.")
        + f"\n\nFLYWHEEL_TOKEN\n{token_json}"
    )

    # Reset tracker so the next session starts fresh.
    fresh = StateTracker()
    save_tracker(state_path, fresh)

    out = {"systemMessage": context_text}
    print(json.dumps(out))


if __name__ == "__main__":
    main()
