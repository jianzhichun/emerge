from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.policy_config import default_emerge_home  # noqa: E402


def main() -> None:
    payload_text = sys.stdin.read().strip()
    try:
        payload = json.loads(payload_text) if payload_text else {}
    except Exception:
        payload = {}

    emerge_home = default_emerge_home()
    for subdir in ("hook-state", "connectors", "repl"):
        (emerge_home / subdir).mkdir(parents=True, exist_ok=True)

    out = {"systemMessage": f"emerge plugin ready. Home: {emerge_home}"}
    print(json.dumps(out))


if __name__ == "__main__":
    main()
