"""InstructionsLoaded hook — inject connector context when CLAUDE.md is lazily loaded.

CC fires InstructionsLoaded each time a CLAUDE.md or .claude/rules/*.md file
is loaded — including mid-session lazy loads when entering a new directory.
This is more precise than SessionStart (which only fires once per session).

We inject:
- Span state reminder (if a span is open)
- Connector NOTES.md content (if the loaded file is a connector rules file)
- A compact reflection summary from the span tracker (if the cache is warm)

Output contract: top-level systemMessage (InstructionsLoaded is not in the
hookSpecificOutput allowed list).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> None:
    payload_text = sys.stdin.read().strip()
    try:
        payload = json.loads(payload_text) if payload_text else {}
    except Exception:
        payload = {}

    from scripts.policy_config import REFLECTION_CACHE_TTL_MS, default_hook_state_root, pin_plugin_data_path_if_present
    pin_plugin_data_path_if_present()
    state_root = Path(default_hook_state_root())

    parts: list[str] = []

    # 1. Active span reminder
    try:
        from scripts.state_tracker import load_tracker
        tracker = load_tracker(state_root / "state.json")
        span_id = tracker.state.get("active_span_id")
        span_intent = tracker.state.get("active_span_intent") or ""
        if span_id:
            parts.append(
                f"[Span active: {span_intent or span_id}] "
                "Do NOT call icc_span_open — call icc_span_close when done."
            )
    except Exception:
        pass

    # 2. Compact reflection (warm cache only — don't compute on every file load)
    try:
        import os
        from scripts.span_tracker import SpanTracker
        from scripts.policy_config import default_exec_root
        exec_root = Path(os.environ.get("EMERGE_STATE_ROOT", str(default_exec_root())))
        st = SpanTracker(state_root=exec_root, hook_state_root=state_root)
        reflection = st.format_reflection_with_cache(cache_ttl_ms=REFLECTION_CACHE_TTL_MS)
        if reflection:
            parts.append(reflection)
    except Exception:
        pass

    # 3. Connector NOTES.md — fires when CC lazily loads a .claude/rules/connector-*.md file.
    #    session_start.py writes those files with a 400-char stub; the stub's only purpose
    #    is to trigger this lazy load. Here we inject the full NOTES.md (up to 1200 chars)
    #    as the actual operational payload. The asymmetry (400 stub vs 1200 here) is
    #    intentional: the stub is a navigation hint, not a context payload.
    #    InstructionsLoaded fires at most once per rules file per session, so token cost
    #    is bounded to one injection per connector encountered during the session.
    try:
        file_path = str(payload.get("file_path", ""))
        if "/rules/connector-" in file_path and file_path.endswith(".md"):
            name = Path(file_path).stem.removeprefix("connector-")
            notes_path = Path.home() / ".emerge" / "connectors" / name / "NOTES.md"
            if notes_path.exists():
                notes_text = notes_path.read_text(encoding="utf-8").strip()
                if notes_text:
                    parts.append(f"[Connector:{name} NOTES]\n{notes_text[:1200]}")
    except Exception:
        pass

    if parts:
        print(json.dumps({"systemMessage": "\n\n".join(parts)}))
    else:
        print(json.dumps({}))


if __name__ == "__main__":
    main()
