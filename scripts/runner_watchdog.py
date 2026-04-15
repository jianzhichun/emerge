"""
Emerge Runner Watchdog
======================
Launches remote_runner.py as a subprocess and keeps it alive.

Restart triggers:
  1. Process crashes / exits unexpectedly → auto-restart after RESTART_DELAY_S
  2. Signal file appears at SIGNAL_FILE path → graceful restart (used by AI deploy)

Usage (from plugin root, in interactive/RDP session):
    pythonw scripts/runner_watchdog.py --host 0.0.0.0 --port 8787
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SIGNAL_FILE = ROOT / ".watchdog-restart"
RESTART_DELAY_S = 3
POLL_INTERVAL_S = 2

_CONFIG_PATH = Path.home() / ".emerge" / "runner-config.json"


def _load_team_lead_url() -> str:
    try:
        data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        return str(data.get("team_lead_url", "") or "").strip().rstrip("/")
    except (OSError, ValueError, json.JSONDecodeError):
        return ""


def _start_runner(host: str, port: int, python: str) -> subprocess.Popen:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    tl = _load_team_lead_url()
    if tl:
        env["EMERGE_TEAM_LEAD_URL"] = tl
    return subprocess.Popen(
        [python, str(ROOT / "scripts" / "remote_runner.py"), "--host", host, "--port", str(port)],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def run(host: str, port: int, python: str) -> None:
    SIGNAL_FILE.unlink(missing_ok=True)
    proc = _start_runner(host, port, python)
    log(f"runner started (pid={proc.pid})")

    while True:
        time.sleep(POLL_INTERVAL_S)

        restart = False

        # Check signal file (AI-triggered deploy restart)
        if SIGNAL_FILE.exists():
            log("restart signal detected")
            SIGNAL_FILE.unlink(missing_ok=True)
            restart = True

        # Check if runner exited
        elif proc.poll() is not None:
            log(f"runner exited (code={proc.returncode}), restarting in {RESTART_DELAY_S}s")
            time.sleep(RESTART_DELAY_S)
            restart = True

        if restart:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            proc = _start_runner(host, port, python)
            log(f"runner restarted (pid={proc.pid})")


def log(msg: str) -> None:
    log_path = ROOT / ".watchdog.log"
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n"
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Emerge runner watchdog")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()
    run(args.host, args.port, args.python)
