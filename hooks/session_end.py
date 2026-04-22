from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.span_service import SpanService  # noqa: E402


def main() -> None:
    sys.stdin.read()  # consume stdin (unused by SessionEnd)

    cleanup_performed: list[str] = []

    # Clear stale active_span_id — if a span was open when session ended,
    # it is unresolvable; SessionStart will also clear it, but belt+suspenders.
    if SpanService().clear_active():
        cleanup_performed.append("cleared_active_span")

    # SessionEnd does not accept `hookSpecificOutput` —
    # use top-level `systemMessage` (or empty object when nothing to report).
    if cleanup_performed:
        out = {"systemMessage": f"session_end: cleanup_performed={','.join(cleanup_performed)}"}
    else:
        out = {}
    print(json.dumps(out))


if __name__ == "__main__":
    main()
