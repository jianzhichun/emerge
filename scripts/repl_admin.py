"""Local REPL state admin utility — CLI entry point.

All business logic lives in the sub-packages:
  scripts/admin/shared.py    — path resolvers, _local_plugin_version
  scripts/admin/api.py       — SSE, cockpit HTML, settings, status
  scripts/admin/control_plane.py — all cmd_control_plane_* functions
  scripts/admin/pipeline.py  — pipeline/connector operations
  scripts/admin/cockpit.py   — CockpitHTTPServer and HTTP handlers
  scripts/admin/runner.py    — runner deploy, self-install URLs, config
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.admin.shared import (  # noqa: E402
    _local_plugin_version,
    _resolve_state_root,
    _resolve_repl_root,
    _resolve_connector_root,
)
from scripts.admin.api import (  # noqa: E402
    cmd_status,
    cmd_clear,
    render_policy_status_pretty,
)
from scripts.admin.control_plane import (  # noqa: E402
    _resolve_session_id,
    _session_paths,
    cmd_control_plane_state,
    cmd_control_plane_intents,
    cmd_control_plane_session,
    cmd_control_plane_hook_state,
    cmd_control_plane_exec_events,
    cmd_control_plane_tool_events,
    cmd_control_plane_pipeline_events,
    cmd_control_plane_spans,
    cmd_control_plane_span_candidates,
    cmd_control_plane_reflection_cache,
    cmd_control_plane_monitors,
    cmd_control_plane_delta_reconcile,
    cmd_control_plane_risk_update,
    cmd_control_plane_risk_add,
    cmd_control_plane_policy_freeze,
    cmd_control_plane_policy_unfreeze,
    cmd_control_plane_session_export,
    cmd_control_plane_session_reset,
)
from scripts.admin.pipeline import (  # noqa: E402
    _load_registry,
    _save_registry,
    _normalize_intent_signature,
    cmd_policy_status,
    cmd_intent_delete,
    cmd_intent_set,
    cmd_connector_export,
    cmd_connector_import,
    cmd_normalize_intents,
)
from scripts.admin.cockpit import (  # noqa: E402
    _make_cockpit_handler,
    _StandaloneDaemonStub,
    CockpitHTTPServer,
    cmd_serve,
    cmd_serve_stop,
)
from scripts.admin.runner import (  # noqa: E402
    cmd_runner_status,
    cmd_runner_deploy,
    cmd_runner_install_url,
    cmd_runner_config_status,
    cmd_runner_config_set,
    cmd_runner_config_unset,
    render_runner_status_pretty,
    _load_runner_config,
    _save_runner_config,
)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Local REPL state admin utility")
    parser.add_argument(
        "command",
        choices=[
            "status",
            "clear",
            "policy-status",
            "runner-status",
            "runner-config-status",
            "runner-config-set",
            "runner-config-unset",
            "runner-install-url",
            "runner-deploy",
            "intent-delete",
            "intent-set",
            "connector-export",
            "connector-import",
            "normalize-intents",
            "serve",
            "serve-stop",
        ],
    )
    parser.add_argument("--pretty", action="store_true", help="Render human-readable output")
    parser.add_argument("--runner-key", default="", help="Runner key (usually target_profile)")
    parser.add_argument("--runner-url", default="", help="Runner URL")
    parser.add_argument("--as-default", action="store_true", help="Set default runner URL")
    parser.add_argument("--clear-default", action="store_true", help="Clear default runner URL")
    parser.add_argument("--target-profile", default="", help="Target profile key (runner map / deploy routing)")
    parser.add_argument("--runner-port", type=int, default=8787, help="Remote runner bind port")
    parser.add_argument(
        "--daemon-port",
        type=int,
        default=8789,
        help="Daemon HTTP port for runner-install-url (team-lead URL generation)",
    )
    parser.add_argument("--intent-key", default="", help="Intent key for intent-delete/intent-set (e.g. mock.read.layers)")
    parser.add_argument("--set", dest="set_fields", action="append", metavar="FIELD=VALUE",
                        help="Field to patch for intent-set (repeatable, e.g. --set stage=explore --set rollout_pct=0)")
    parser.add_argument("--connector", default="", help="Connector name for connector-export")
    parser.add_argument("--out", default="", help="Output zip path for connector-export")
    parser.add_argument("--pkg", default="", help="Package zip path for connector-import")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing connector/registry on import")
    parser.add_argument("--open", action="store_true", help="Open browser after starting cockpit server")
    parser.add_argument("--port", type=int, default=0, help="Port for cockpit serve (0 = auto-assign free port)")
    args = parser.parse_args()

    if args.command == "status":
        out = cmd_status()
    elif args.command == "policy-status":
        out = cmd_policy_status()
    elif args.command == "runner-status":
        out = cmd_runner_status()
    elif args.command == "runner-config-status":
        out = cmd_runner_config_status()
    elif args.command == "runner-config-set":
        out = cmd_runner_config_set(
            runner_key=str(args.runner_key),
            runner_url=str(args.runner_url),
            as_default=bool(args.as_default),
        )
    elif args.command == "runner-config-unset":
        out = cmd_runner_config_unset(
            runner_key=str(args.runner_key),
            clear_default=bool(args.clear_default),
        )
    elif args.command == "runner-install-url":
        out = cmd_runner_install_url(
            runner_port=int(args.runner_port),
            daemon_port=int(args.daemon_port),
        )
        if args.pretty and out.get("ok"):
            print(f"Team lead (generated): {out.get('team_lead_url', '')}")
            print(f"Linux/macOS:\n  {out.get('bash', '')}\n")
            print(f"Windows PowerShell:\n  {out.get('powershell', '')}")
            return
    elif args.command == "runner-deploy":
        out = cmd_runner_deploy(
            runner_url=str(args.runner_url),
            target_profile=str(args.target_profile) or "default",
        )
    elif args.command == "intent-delete":
        out = cmd_intent_delete(key=str(args.intent_key))
    elif args.command == "intent-set":
        fields: dict = {}
        for pair in (args.set_fields or []):
            k, _, v = pair.partition("=")
            k = k.strip()
            try:
                fields[k] = int(v)
            except ValueError:
                try:
                    fields[k] = float(v)
                except ValueError:
                    fields[k] = v
        out = cmd_intent_set(key=str(args.intent_key), fields=fields)
    elif args.command == "connector-export":
        out = cmd_connector_export(
            connector=str(args.connector),
            out=str(args.out) if args.out else f"{args.connector}-emerge-pkg.zip",
        )
    elif args.command == "connector-import":
        out = cmd_connector_import(
            pkg=str(args.pkg),
            overwrite=bool(args.overwrite),
        )
    elif args.command == "normalize-intents":
        out = cmd_normalize_intents(
            connector=str(args.connector),
        )
    elif args.command == "serve":
        port = getattr(args, "port", 0) or 0
        open_b = getattr(args, "open", False)
        result = cmd_serve(port=port, open_browser=open_b)
        status = "reused existing" if result.get("reused") else "started"
        print(f"Cockpit running at {result['url']} ({status})")
        if not result.get("reused"):
            print("Press Ctrl-C to stop.")
            import time as _time
            try:
                while True:
                    _time.sleep(1)
            except KeyboardInterrupt:
                cmd_serve_stop()
        sys.exit(0)
    elif args.command == "serve-stop":
        out = cmd_serve_stop()
        print(json.dumps(out))
        sys.exit(0)
    else:
        out = cmd_clear()

    if args.pretty and args.command == "policy-status":
        print(render_policy_status_pretty(out), end="")
    elif args.pretty and args.command == "runner-status":
        print(render_runner_status_pretty(out), end="")
    else:
        print(json.dumps(out))


if __name__ == "__main__":
    main()
