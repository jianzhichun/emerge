"""runner_sync.py — SessionStart hook: auto-deploy all configured runners when plugin version changes.

Reads ~/.emerge/runner-map.json, deduplicates by URL, checks each runner's
plugin.json version against the local version, and runs runner-deploy for
any that are out of date.

Designed to be fast and silent when everything is up to date.
Exit code is always 0 (failures are logged, not fatal — CC must start cleanly).
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _local_version() -> str:
    manifest = ROOT / ".claude-plugin" / "plugin.json"
    try:
        return str(json.loads(manifest.read_text(encoding="utf-8")).get("version", "")).strip()
    except Exception:
        return ""


def _http_get(runner_url: str, path: str) -> dict:
    with _NO_PROXY.open(runner_url.rstrip("/") + path, timeout=8) as resp:
        return json.loads(resp.read())


def _runner_version(runner_url: str) -> str:
    """Read plugin.json version from remote runner via /status root + icc_exec."""
    status = _http_get(runner_url, "/status")
    remote_root = status.get("root", "").strip()
    if not remote_root:
        return ""
    # Use forward-slash join so this works on both Linux and Windows runners
    code = (
        f"import json, pathlib\n"
        f"v = json.loads(pathlib.Path({repr(remote_root)}).joinpath('.claude-plugin','plugin.json')"
        f".read_text(encoding='utf-8')).get('version','')\n"
        f"print(v)"
    )
    payload = json.dumps({
        "tool_name": "icc_exec",
        "arguments": {"code": code, "no_replay": True},
    }).encode()
    req = urllib.request.Request(
        runner_url.rstrip("/") + "/run",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _NO_PROXY.open(req, timeout=8) as resp:
        data = json.loads(resp.read())
    if not data.get("ok"):
        raise RuntimeError(data.get("error", "icc_exec failed"))
    # Runner wraps stdout as "stdout: <value>" in text content blocks
    for block in (data.get("result", {}).get("content", []) or []):
        if isinstance(block, dict) and block.get("type") == "text":
            text = str(block["text"]).strip()
            if text.lower().startswith("stdout:"):
                text = text[7:].strip()
            line = text.splitlines()[0].strip() if text else ""
            if line:
                return line
    return ""


def _deploy(runner_url: str, profile: str) -> None:
    """Run runner-deploy for the given profile."""
    from scripts.admin.runner import cmd_runner_deploy
    cmd_runner_deploy(runner_url=runner_url, target_profile=profile)


def main() -> None:
    local_ver = _local_version()
    if not local_ver:
        return  # no version info, skip

    from scripts.runner_client import RunnerRouter
    cfg_path = RunnerRouter.persisted_config_path()
    if not cfg_path.exists():
        return  # no runners configured

    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    runner_map: dict[str, str] = cfg.get("map", {})
    if not runner_map:
        return

    # Deduplicate: one deploy per unique URL (multiple profiles can share a URL)
    # Keep first profile name per URL for deploy call
    seen: dict[str, str] = {}  # url -> first_profile
    for profile, url in runner_map.items():
        if url and url not in seen:
            seen[url] = profile

    synced: list[str] = []
    errors: list[str] = []

    for url, profile in seen.items():
        try:
            remote_ver = _runner_version(url)
        except Exception:
            # Runner unreachable — skip silently (not a sync error)
            continue

        if remote_ver == local_ver:
            continue  # up to date

        try:
            _deploy(url, profile)
            synced.append(f"{profile} ({url}): {remote_ver or '?'} → {local_ver}")
        except Exception as exc:
            errors.append(f"{profile} ({url}): {exc}")

    if synced:
        print(f"[emerge] runner sync: updated {len(synced)} runner(s)")
        for line in synced:
            print(f"  ✓ {line}")
    if errors:
        print(f"[emerge] runner sync: {len(errors)} error(s) (non-fatal)")
        for line in errors:
            print(f"  ✗ {line}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Never fail CC startup
        print(f"[emerge] runner_sync error (non-fatal): {exc}", file=sys.stderr)
    sys.exit(0)
