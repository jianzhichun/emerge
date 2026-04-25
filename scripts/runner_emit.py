from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def _load_runner_config(config_path: Path | None = None) -> dict[str, Any]:
    path = config_path or Path.home() / ".emerge" / "runner-config.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _default_message_id(event: dict[str, Any]) -> str:
    material = json.dumps(event, sort_keys=True, ensure_ascii=True, default=str)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return f"runner-{digest}"


def build_event(event: dict[str, Any], *, runner_profile: str = "", machine_id: str = "") -> dict[str, Any]:
    payload = dict(event)
    payload.setdefault("schema_version", 1)
    payload.setdefault("observed_at_ms", int(time.time() * 1000))
    if runner_profile:
        payload.setdefault("runner_profile", runner_profile)
    if machine_id:
        payload.setdefault("machine_id", machine_id)
    payload.setdefault("message_id", _default_message_id(payload))
    return payload


def emit_event(
    event: dict[str, Any],
    *,
    team_lead_url: str | None = None,
    runner_profile: str | None = None,
    machine_id: str | None = None,
    timeout_s: float = 1.0,
    config_path: Path | None = None,
) -> bool:
    cfg = _load_runner_config(config_path)
    url = (team_lead_url or os.environ.get("EMERGE_TEAM_LEAD_URL") or str(cfg.get("team_lead_url", ""))).strip().rstrip("/")
    profile = (runner_profile or os.environ.get("EMERGE_RUNNER_PROFILE") or str(cfg.get("runner_profile", ""))).strip()
    machine = (machine_id or os.environ.get("EMERGE_MACHINE_ID") or "").strip()
    if not url or not profile:
        return False
    payload = build_event(event, runner_profile=profile, machine_id=machine)
    body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    req = urllib.request.Request(
        f"{url}/runner/event",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s):
            return True
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def main() -> int:
    import sys

    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        print(f"invalid JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        print("event must be a JSON object", file=sys.stderr)
        return 2
    ok = emit_event(payload)
    print(json.dumps({"ok": ok}))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
