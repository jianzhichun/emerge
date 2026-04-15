"""MCP resource reading for EmergeDaemon.

McpResourceHandler owns list_resources / read_resource / get_connector_intents /
build_intents_section / get_prompt — all pure-read operations on daemon state.
No side effects; safe to call from any thread.

Separated from emerge_daemon.py to keep the main file focused on tool dispatch
and lifecycle management.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from scripts.policy_config import PIPELINE_KEY_RE as _PIPELINE_KEY_RE


class McpResourceHandler:
    """Read-only view of daemon state for MCP resource endpoints."""

    def __init__(
        self,
        *,
        state_root: "Callable[[], Path]",  # callable so tests can update daemon._state_root after init
        pipeline: "Callable[[], Any]",   # callable returning PipelineEngine
        goal_control: Any,      # GoalControlPlane
        span_tracker: Any,      # SpanTracker
        hook_state_path: Callable[[], Path],
    ) -> None:
        self._get_state_root = state_root
        self._get_pipeline = pipeline
        self._goal_control = goal_control
        self._span_tracker = span_tracker
        self._hook_state_path = hook_state_path

    # ------------------------------------------------------------------
    # Resource listing
    # ------------------------------------------------------------------

    def list_resources(self) -> list[dict[str, Any]]:
        from scripts.policy_config import load_json_object
        from scripts.pipeline_engine import PipelineEngine

        static: list[dict[str, Any]] = [
            {
                "uri": "policy://current",
                "name": "Pipeline policy registry",
                "mimeType": "application/json",
                "description": "Current session pipeline lifecycle tracking (explore→canary→stable)",
            },
            {
                "uri": "runner://status",
                "name": "Runner health summary",
                "mimeType": "application/json",
                "description": "Remote runner connectivity and health for all configured endpoints",
            },
            {
                "uri": "state://deltas",
                "name": "State tracker deltas",
                "mimeType": "application/json",
                "description": "Session goal, recorded deltas, and open risks",
            },
            {
                "uri": "state://goal",
                "name": "Goal control snapshot",
                "mimeType": "application/json",
                "description": "Current active goal and decision metadata",
            },
            {
                "uri": "state://goal-ledger",
                "name": "Goal control ledger",
                "mimeType": "application/json",
                "description": "Recent goal events and decision outcomes",
            },
        ]

        connector_names: set[str] = set()
        for connector_root in self._get_pipeline()._connector_roots:
            if not connector_root.exists():
                continue
            for meta in connector_root.glob("*/pipelines/*/*.yaml"):
                parts = meta.relative_to(connector_root).parts
                if len(parts) == 4:
                    connector, _, mode, name_yaml = parts
                    name = name_yaml[:-5]
                    uri = f"pipeline://{connector}/{mode}/{name}"
                    static.append({
                        "uri": uri,
                        "name": f"{connector} {mode} pipeline: {name}",
                        "mimeType": "application/json",
                        "description": f"Pipeline metadata for {connector}/{mode}/{name}",
                    })
            for notes in connector_root.glob("*/NOTES.md"):
                cname = notes.parent.name
                connector_names.add(cname)
                uri = f"connector://{cname}/notes"
                static.append({
                    "uri": uri,
                    "name": f"{cname} connector notes",
                    "mimeType": "text/markdown",
                    "description": (
                        f"Operational notes for the {cname} vertical: COM patterns, "
                        "API quirks, known issues. Includes tracked intent_signature list."
                    ),
                })

        registry_path = self._get_state_root() / "pipelines-registry.json"
        registry = load_json_object(registry_path, root_key="pipelines")
        for key in registry["pipelines"]:
            if _PIPELINE_KEY_RE.match(key):
                connector_names.add(key.split(".", 1)[0])

        already_noted = {r["uri"] for r in static}
        for cname in sorted(connector_names):
            static.append({
                "uri": f"connector://{cname}/intents",
                "name": f"{cname} tracked intents",
                "mimeType": "application/json",
                "description": (
                    f"JSON index of all flywheel-tracked intent_signature values for {cname}, "
                    "with status and description"
                ),
            })
            notes_uri = f"connector://{cname}/notes"
            if notes_uri not in already_noted:
                static.append({
                    "uri": notes_uri,
                    "name": f"{cname} connector notes",
                    "mimeType": "text/markdown",
                    "description": f"Tracked intents for {cname} connector (no NOTES.md yet).",
                })

        span_candidates = self._span_tracker._load_candidates().get("spans", {})
        span_connectors: set[str] = set()
        for sig in span_candidates:
            if _PIPELINE_KEY_RE.match(sig):
                span_connectors.add(sig.split(".", 1)[0])
        for cname in sorted(span_connectors):
            spans_uri = f"connector://{cname}/spans"
            if spans_uri not in already_noted:
                static.append({
                    "uri": spans_uri,
                    "name": f"{cname} span intents",
                    "mimeType": "application/json",
                    "description": (
                        f"JSON index of all flywheel-tracked span intents for {cname}, "
                        "with policy status and skeleton generation state."
                    ),
                })
        return static

    # ------------------------------------------------------------------
    # Resource reading
    # ------------------------------------------------------------------

    def read_resource(self, uri: str) -> dict[str, Any]:
        from scripts.policy_config import load_json_object
        from scripts.pipeline_engine import PipelineEngine
        from scripts.runner_client import RunnerRouter

        if uri == "policy://current":
            path = self._get_state_root() / "pipelines-registry.json"
            data = load_json_object(path, root_key="pipelines")
            return {"uri": uri, "mimeType": "application/json", "text": json.dumps(data)}

        if uri == "runner://status":
            router = RunnerRouter.from_env()
            summary = router.health_summary() if router else {"configured": False, "any_reachable": False}
            return {"uri": uri, "mimeType": "application/json", "text": json.dumps(summary)}

        if uri == "state://deltas":
            from scripts.state_tracker import load_tracker
            tracker = load_tracker(self._hook_state_path())
            data = tracker.to_dict()
            snapshot = self._goal_control.read_snapshot()
            data["goal"] = snapshot.get("text", "")
            data["goal_source"] = snapshot.get("source", "unset")
            data["goal_version"] = snapshot.get("version", 0)
            return {"uri": uri, "mimeType": "application/json", "text": json.dumps(data)}

        if uri == "state://goal":
            snapshot = self._goal_control.read_snapshot()
            return {"uri": uri, "mimeType": "application/json", "text": json.dumps(snapshot)}

        if uri == "state://goal-ledger":
            rows = self._goal_control.read_ledger(limit=500)
            return {"uri": uri, "mimeType": "application/json", "text": json.dumps({"events": rows})}

        if uri.startswith("pipeline://"):
            rest = uri[len("pipeline://"):]
            parts = rest.split("/", 2)
            if len(parts) == 3:
                connector, mode, name = parts
                if any(".." in p or p.startswith("/") for p in (connector, mode, name)):
                    raise KeyError(f"Resource not found: {uri}")
                for connector_root in self._get_pipeline()._connector_roots:
                    meta = connector_root / connector / "pipelines" / mode / f"{name}.yaml"
                    try:
                        meta.resolve().relative_to(connector_root.resolve())
                    except ValueError:
                        continue
                    if meta.exists():
                        data = PipelineEngine._load_metadata(meta)
                        return {"uri": uri, "mimeType": "application/json", "text": json.dumps(data)}

        if uri.startswith("connector://"):
            rest = uri[len("connector://"):]
            parts = rest.split("/", 1)
            if len(parts) == 2:
                connector, resource = parts
                if not re.match(r"^[a-z0-9][a-z0-9_-]*$", connector):
                    raise KeyError(f"Resource not found: {uri}")
                if resource == "notes":
                    notes_text = ""
                    for connector_root in self._get_pipeline()._connector_roots:
                        notes = connector_root / connector / "NOTES.md"
                        try:
                            notes.resolve().relative_to(connector_root.resolve())
                        except ValueError:
                            continue
                        if notes.exists():
                            notes_text = notes.read_text(encoding="utf-8")
                            break
                    intents_section = self.build_intents_section(connector)
                    if intents_section:
                        notes_text = (notes_text.rstrip() + "\n\n" + intents_section).lstrip()
                    if notes_text:
                        return {"uri": uri, "mimeType": "text/markdown", "text": notes_text}
                    raise KeyError(f"Resource not found: {uri}")
                if resource == "intents":
                    data = self.get_connector_intents(connector)
                    return {"uri": uri, "mimeType": "application/json", "text": json.dumps(data)}
                if resource == "spans":
                    candidates = self._span_tracker._load_candidates().get("spans", {})
                    relevant = {k: v for k, v in candidates.items() if k.startswith(f"{connector}.")}
                    return {"uri": uri, "mimeType": "application/json", "text": json.dumps(relevant, ensure_ascii=False)}

        raise KeyError(f"Resource not found: {uri}")

    # ------------------------------------------------------------------
    # Connector intent index
    # ------------------------------------------------------------------

    def get_connector_intents(self, connector: str) -> dict[str, Any]:
        """Return all tracked intent entries for a connector from pipelines-registry.json."""
        from scripts.policy_config import load_json_object
        registry_path = self._get_state_root() / "pipelines-registry.json"
        registry = load_json_object(registry_path, root_key="pipelines")
        prefix = f"{connector}."
        return {
            key: {
                "status": pipeline.get("status", "explore"),
                "success_rate": pipeline.get("success_rate", 0.0),
                "verify_rate": pipeline.get("verify_rate", 0.0),
                "attempts": pipeline.get("attempts_at_transition", 0),
                "description": pipeline.get("description", ""),
                "source": pipeline.get("source", "exec"),
            }
            for key, pipeline in registry["pipelines"].items()
            if key.startswith(prefix)
        }

    def build_intents_section(self, connector: str) -> str:
        """Build a markdown table of tracked intents for injection into connector://notes."""
        intents = self.get_connector_intents(connector)
        if not intents:
            return ""
        status_icon = {"stable": "✓", "canary": "⟳", "explore": "…"}
        rows = []
        for key in sorted(intents):
            info = intents[key]
            icon = status_icon.get(info["status"], "?")
            success_pct = f"{info['success_rate'] * 100:.0f}%"
            desc = info["description"] or ""
            path = "`span-bridge`" if info["source"] == "pipeline" else "`icc_exec`"
            rows.append(f"| `{key}` | {info['status']} {icon} | {success_pct} | {path} | {desc} |")
        header = (
            "---\n"
            "## Tracked Intents (Emerge flywheel)\n"
            "- Intents with `span-bridge` path are crystallized pipelines — "
            "`icc_span_open` executes them at zero LLM cost.\n"
            "- Intents with `icc_exec` path are still in explore/canary — "
            "use `icc_exec` with the exact `intent_signature`.\n"
            "- Do NOT invent new intent names — pick from this list whenever the intent matches.\n\n"
            "| Intent | Status | Success | Path | Description |\n"
            "|--------|--------|---------|------|-------------|"
        )
        return header + "\n" + "\n".join(rows)

    # ------------------------------------------------------------------
    # Prompts
    # ------------------------------------------------------------------

    _PROMPTS = [
        {
            "name": "icc_explore",
            "description": "Explore a new vertical using icc_exec with policy tracking",
            "arguments": [
                {"name": "vertical", "description": "Name of the vertical (e.g. zwcad)", "required": True},
                {"name": "goal", "description": "What to explore", "required": False},
            ],
        },
    ]

    def get_prompt(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "icc_explore":
            vertical = str(arguments.get("vertical", "<vertical>"))
            goal = str(arguments.get("goal", "explore the vertical"))
            content = (
                f"Use icc_exec to explore the {vertical} vertical. Goal: {goal}.\n"
                f"Include intent_signature='<intent>' and script_ref='~/.emerge/connectors/{vertical}/pipelines/read/state.py' "
                "in each icc_exec call so the policy flywheel can track progress.\n"
                "When the exec is stable and consistent, use icc_span_open with intent_signature='<intent>' "
                "to trigger the bridge path."
            )
            return {"name": name, "messages": [{"role": "user", "content": content}]}
        raise KeyError(f"Prompt not found: {name}")
