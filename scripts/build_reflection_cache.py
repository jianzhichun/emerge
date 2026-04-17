from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.policy_config import default_state_root, default_hook_state_root
from scripts.span_tracker import SpanTracker


def _build_deep_summary(tracker: SpanTracker, max_items: int = 8) -> str:
    candidates = tracker._load_candidates().get("intents", {})
    if not candidates:
        return ""

    stable: list[tuple[str, dict]] = []
    canary: list[tuple[str, dict]] = []
    rollback: list[tuple[str, dict]] = []
    for sig, entry in candidates.items():
        status = tracker.get_policy_status(sig)
        if status == "stable":
            stable.append((sig, entry))
        elif status == "canary":
            canary.append((sig, entry))
        elif status == "rollback":
            rollback.append((sig, entry))

    stable.sort(key=lambda x: -int(x[1].get("attempts", 0)))
    canary.sort(key=lambda x: -int(x[1].get("attempts", 0)))
    rollback.sort(key=lambda x: -int(x[1].get("consecutive_failures", 0)))

    recent_fail: dict[str, int] = {}
    wal = tracker._wal_path()
    if wal.exists():
        try:
            rows = wal.read_text(encoding="utf-8").splitlines()[-200:]
        except OSError:
            rows = []
        for line in rows:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            sig = str(rec.get("intent_signature", "")).strip()
            if not sig:
                continue
            if rec.get("outcome") != "success":
                recent_fail[sig] = recent_fail.get(sig, 0) + 1

    lines: list[str] = ["Muscle memory (deep)"]
    if stable:
        lines.append(
            "High-confidence intents: "
            + ", ".join(sig for sig, _ in stable[:max_items])
        )
    if canary:
        lines.append(
            "Near-stable intents: "
            + ", ".join(sig for sig, _ in canary[: min(5, max_items)])
        )
    if rollback:
        lines.append(
            "Watchlist (rollback): "
            + ", ".join(sig for sig, _ in rollback[: min(3, max_items)])
        )
    if recent_fail:
        hot = sorted(recent_fail.items(), key=lambda x: -x[1])[:5]
        lines.append(
            "Recent failure hot spots: "
            + ", ".join(f"{sig} x{count}" for sig, count in hot)
        )
    if not stable and not canary and not rollback and not recent_fail:
        return ""
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build deep reflection cache for hook-side injection."
    )
    parser.add_argument(
        "--max-items",
        type=int,
        default=8,
        help="Maximum intents listed per section.",
    )
    args = parser.parse_args()

    exec_root = Path(os.environ.get("EMERGE_STATE_ROOT", str(default_state_root())))
    hook_root = Path(default_hook_state_root())
    tracker = SpanTracker(state_root=exec_root, hook_state_root=hook_root)

    summary = _build_deep_summary(tracker, max_items=max(1, args.max_items))
    if not summary:
        print("No reflection data found; cache not updated.")
        return 0
    tracker.write_reflection_cache(
        summary_text=summary,
        meta={"builder": "build_reflection_cache.py", "max_items": int(args.max_items)},
    )
    print("Reflection cache written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
