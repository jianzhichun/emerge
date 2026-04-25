"""Memory Hub sync agent — CLI entry point.

Real code lives in:
  scripts/sync/asset_ops.py  — connector asset export/import
  scripts/sync/git_ops.py    — git worktree operations
  scripts/sync/sync_flow.py  — push_flow / pull_flow / event loop
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.sync.asset_ops import connectors_root as _connectors_root  # noqa: E402
from scripts.sync.git_ops import git_setup_worktree  # noqa: E402
from scripts.sync.sync_flow import pull_flow, run_event_loop, sync_connector  # noqa: E402
from scripts.hub_config import (  # noqa: E402
    is_configured,
    load_hub_config,
    save_hub_config,
    hub_worktree_path,
)

logger = logging.getLogger(__name__)


# ── CLI commands ─────────────────────────────────────────────────────────────

def cmd_setup() -> None:
    """Interactive setup wizard."""
    print("emerge_sync setup")
    remote = input("Remote URL (e.g. git@quasar:team/hub.git): ").strip()
    branch = input("Branch name [emerge-hub]: ").strip() or "emerge-hub"
    author = input("Author (e.g. alice <alice@team.com>): ").strip()

    conns_root = _connectors_root()
    available: list[str] = []
    if conns_root.exists():
        available = [d.name for d in conns_root.iterdir() if d.is_dir()]
    if not available:
        print("No local connectors found. Add connectors first.")
        return
    print(f"Available connectors: {', '.join(available)}")
    selected_input = input("Select connectors (comma-separated): ").strip()
    selected = [s.strip() for s in selected_input.split(",") if s.strip() in available]

    cfg = {
        "remote": remote,
        "branch": branch,
        "poll_interval_seconds": 300,
        "selected_verticals": selected,
        "author": author,
    }
    save_hub_config(cfg)

    worktree = hub_worktree_path()
    print(f"Setting up hub worktree at {worktree}...")
    result = git_setup_worktree(worktree, remote, branch, author)
    print(f"Worktree ready: {result['action']}")
    print("Running initial pull...")
    for connector in selected:
        pull_flow(connector)
    print("Setup complete.")


def cmd_sync(connector: str | None = None) -> None:
    cfg = load_hub_config()
    verticals = [connector] if connector else cfg.get("selected_verticals", [])
    for c in verticals:
        result = sync_connector(c)
        push_result = result.get("push", {})
        if push_result.get("conflict"):
            print(f"sync {c}: conflict — resolve via icc_hub(action='status')")
            continue
        if not result.get("ok"):
            err = push_result.get("error") or result.get("pull", {}).get("error") or "unknown"
            print(f"sync {c}: error — {err}")
            continue
        if result.get("mode") == "read-only":
            print(f"sync {c}: ok (pull-only)")
            continue
        print(f"sync {c}: ok (push)")
        pull_result = result.get("pull", {})
        if pull_result.get("action") == "imported":
            print(f"sync {c}: ok (pull — imported updates)")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = sys.argv[1:]
    if not args or args[0] == "run":
        if not is_configured():
            print("Not configured. Run: python scripts/emerge_sync.py setup")
            sys.exit(1)
        run_event_loop()
    elif args[0] == "setup":
        cmd_setup()
    elif args[0] == "sync":
        connector_arg = args[1] if len(args) > 1 else None
        cmd_sync(connector_arg)
    else:
        print(f"Unknown command: {args[0]}")
        print("Usage: emerge_sync.py [run|setup|sync [connector]]")
