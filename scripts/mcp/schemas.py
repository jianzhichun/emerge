"""MCP tool schema definitions for EmergeDaemon.

Returns the list of tool descriptors that EmergeDaemon advertises via
tools/list.  Keeping schemas here makes emerge_daemon.py easier to read
and lets the schemas be consulted independently (e.g. for tests or docs).
"""
from __future__ import annotations

from typing import Any


def get_tool_schemas() -> list[dict[str, Any]]:
    """Return MCP 2025-11-25 tool descriptors for all icc_* and runner_notify tools."""
    return [
        {
            "name": "icc_span_open",
            "title": "Open Intent Span",
            "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
            "description": (
                "Open an intent span to track a multi-step MCP tool call sequence "
                "in the flywheel. Use before any sequence of Lark/context7/skill tool calls "
                "that represents a reusable intent. When the intent pipeline is stable, "
                "returns the pipeline result directly (bridge) with zero LLM overhead. "
                "Blocked if another span is already open."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "intent_signature": {
                        "type": "string",
                        "description": "<connector>.(read|write).<name> — e.g. 'lark.read.get-doc'",
                    },
                    "description": {"type": "string"},
                    "args": {"type": "object", "description": "Input args for this span"},
                    "source": {"type": "string", "enum": ["skill", "manual"], "default": "manual"},
                    "skill_name": {"type": "string"},
                },
                "required": ["intent_signature"],
            },
            "outputSchema": {
                "type": "object",
                "properties": {
                    "span_id": {
                        "type": "string",
                        "description": "Unique identifier for this span — pass to icc_span_close",
                    },
                    "intent_signature": {"type": "string"},
                    "status": {
                        "type": "string",
                        "description": "opened | bridge (when pipeline bridged directly)",
                    },
                    "policy_status": {
                        "type": "string",
                        "description": "explore | canary | stable",
                    },
                    "bridge": {
                        "type": "boolean",
                        "description": "True when span was bridged — no span_id returned, result is in 'result'",
                    },
                    "result": {
                        "description": "Pipeline result when bridge=true",
                    },
                },
            },
        },
        {
            "name": "icc_span_close",
            "title": "Close Intent Span",
            "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
            "description": (
                "Close the current intent span and commit it to the flywheel WAL. "
                "When the intent reaches stable, auto-generates a Python skeleton "
                "in _pending/ for review. Call icc_span_approve after completing the skeleton."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "span_id": {"type": "string", "description": "span_id from icc_span_open"},
                    "outcome": {"type": "string", "enum": ["success", "failure", "aborted"]},
                    "result_summary": {"type": "object"},
                    "intent_signature": {
                        "type": "string",
                        "description": "Required when span_id is unknown (daemon restart recovery)",
                    },
                },
                "required": ["outcome"],
            },
            "outputSchema": {
                "type": "object",
                "properties": {
                    "span_id": {"type": "string"},
                    "intent_signature": {"type": "string"},
                    "outcome": {
                        "type": "string",
                        "description": "success | failure | aborted",
                    },
                    "policy_status": {
                        "type": "string",
                        "description": "explore | canary | stable",
                    },
                    "synthesis_ready": {
                        "type": "boolean",
                        "description": "True when the pipeline skeleton is ready for icc_span_approve",
                    },
                    "is_read_only": {"type": "boolean"},
                    "skeleton_path": {
                        "type": "string",
                        "description": "Path to generated _pending/<name>.py — present when synthesis_ready=true",
                    },
                    "next_step": {
                        "type": "string",
                        "description": "Human-readable action hint when skeleton is ready",
                    },
                },
            },
        },
        {
            "name": "icc_span_approve",
            "title": "Approve Pipeline Skeleton",
            "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
            "description": (
                "Approve a completed pipeline skeleton and activate the span bridge. "
                "Moves _pending/<name>.py to the real pipeline directory and generates "
                "the required .yaml metadata. Only works when the intent is stable. "
                "After approval, icc_span_open will bridge directly to this pipeline."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "intent_signature": {
                        "type": "string",
                        "description": "Stable span intent to approve",
                    },
                },
                "required": ["intent_signature"],
            },
            "outputSchema": {
                "type": "object",
                "properties": {
                    "intent_signature": {"type": "string"},
                    "pipeline_path": {
                        "type": "string",
                        "description": "Path to activated .py pipeline",
                    },
                    "yaml_path": {
                        "type": "string",
                        "description": "Path to generated .yaml metadata",
                    },
                    "activated": {
                        "type": "boolean",
                        "description": "True when bridge is now active",
                    },
                },
            },
        },
        {
            "name": "icc_exec",
            "title": "Execute Intent",
            "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
            "description": "Execute Python in a persistent session with flywheel tracking. intent_signature is required (enforced). Read tasks set __result=[{...}]; write tasks set __action={'ok':True,...}; side effects use no_replay=True.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python code to execute. Required when mode='inline_code' (the default)."},
                    "mode": {"type": "string", "enum": ["inline_code", "script_ref"], "default": "inline_code", "description": "Execution mode. 'inline_code' requires 'code'; 'script_ref' requires 'script_ref'."},
                    "target_profile": {"type": "string", "description": "Execution profile / remote runner key", "default": "default"},
                    "intent_signature": {"type": "string", "description": "Stable dot-notation identifier for this exec pattern (e.g. zwcad.read.state). Required for flywheel tracking. Use connector://notes to see existing intents before choosing."},
                    "description": {"type": "string", "description": "Human-readable description of what this intent does. Stored in registry and surfaced in connector://notes. Only needed the first time a new intent is introduced."},
                    "no_replay": {"type": "boolean", "description": "If true, exclude this call from WAL replay and crystallization. Use for side-effectful calls only.", "default": False},
                    "script_ref": {"type": "string", "description": "Path to script file. Required when mode='script_ref'."},
                    "script_args": {"type": "object", "description": "Arguments injected as __args in script scope"},
                    "result_var": {"type": "string", "description": "Optional variable name to extract from exec globals as structured JSON in response (e.g. '__result')."},
                    "base_pipeline_id": {"type": "string", "description": "Pipeline id for flywheel bridge routing (e.g. mock.read.layers)"},
                },
                "required": [],
            },
            "outputSchema": {
                "type": "object",
                "properties": {
                    "bridge_promoted": {
                        "type": "boolean",
                        "description": "True when flywheel bridge short-circuited to pipeline result",
                    },
                    "synthesis_ready": {
                        "type": "boolean",
                        "description": "True when enough execs recorded to crystallize a pipeline",
                    },
                    "policy_status": {
                        "type": "string",
                        "description": "Current flywheel policy status: explore | canary | stable",
                    },
                    "result": {
                        "description": "Exec result payload (stdout, result_var extraction, or pipeline data)",
                    },
                    "error": {
                        "type": "string",
                        "description": "Error message if isError=true",
                    },
                },
            },
        },
        # icc_read and icc_write are intentionally omitted from the schema.
        # They remain callable internally but are deprecated for CC use.
        # Use icc_span_open(intent_signature='<connector>.(read|write).<name>')
        # instead — the span bridge executes the pipeline automatically when stable.
        {
            "name": "icc_reconcile",
            "title": "Reconcile Delta",
            "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
            "description": "Reconcile a state tracker delta — confirm, correct, or retract a recorded observation. Pass intent_signature with outcome=correct to register a human fix against the policy flywheel.",
            "_internal": True,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "delta_id": {"type": "string", "description": "ID of the delta to reconcile"},
                    "outcome": {"type": "string", "enum": ["confirm", "correct", "retract"], "description": "Reconciliation outcome"},
                    "intent_signature": {"type": "string", "description": "Intent signature of the exec/pipeline being corrected (required when outcome=correct to update human_fix_rate)"},
                },
                "required": ["delta_id", "outcome"],
            },
        },
        {
            "name": "icc_crystallize",
            "title": "Crystallize Pipeline",
            "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
            "description": "Crystallize exec history into a pipeline file. Reads the WAL for the most recent successful icc_exec matching intent_signature and generates .py + .yaml in the connector root. Call when synthesis_ready is true in policy://current.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "intent_signature": {"type": "string", "description": "Intent signature used in icc_exec calls (e.g. zwcad.read.state)"},
                    "connector": {"type": "string", "description": "Connector name for the output pipeline (e.g. zwcad)"},
                    "pipeline_name": {"type": "string", "description": "Pipeline file name without extension (e.g. state)"},
                    "mode": {"type": "string", "enum": ["read", "write"], "description": "Pipeline mode"},
                    "target_profile": {"type": "string", "description": "Which exec profile's WAL to read", "default": "default"},
                    "persistent": {
                        "type": "boolean",
                        "description": "Whether to scaffold optional start/stop persistent hooks in the generated pipeline",
                        "default": False,
                    },
                },
                "required": ["intent_signature", "connector", "pipeline_name", "mode"],
            },
        },
        {
            "name": "icc_hub",
            "title": "Memory Hub",
            "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
            "description": (
                "Manage Memory Hub — bidirectional connector asset sync via a self-hosted git repo. "
                "Actions: configure (first-time setup — saves config and initialises git worktree), "
                "list (show config), add/remove (manage verticals), "
                "sync (manual push+pull), status (show pending conflicts), "
                "resolve (resolve a conflict with ours|theirs|skip)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["configure", "list", "add", "remove", "sync", "status", "resolve", "setup"],
                        "description": "Hub action to perform",
                    },
                    "remote": {
                        "type": "string",
                        "description": "Git remote URL (required for configure, e.g. user@host:repos/hub.git)",
                    },
                    "branch": {
                        "type": "string",
                        "description": "Orphan branch name (configure only, default: emerge-hub)",
                    },
                    "author": {
                        "type": "string",
                        "description": "Git commit author (required for configure, e.g. 'Alice <alice@team.com>')",
                    },
                    "selected_verticals": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Connector names to sync (configure only)",
                    },
                    "poll_interval_seconds": {
                        "type": "integer",
                        "description": "Background pull interval in seconds (configure only, default: 300)",
                    },
                    "connector": {
                        "type": "string",
                        "description": "Connector name (required for add/remove, optional for sync)",
                    },
                    "conflict_id": {
                        "type": "string",
                        "description": "Conflict ID from status output (required for resolve)",
                    },
                    "resolution": {
                        "type": "string",
                        "enum": ["ours", "theirs", "skip"],
                        "description": "Resolution choice (required for resolve)",
                    },
                },
                "required": ["action"],
            },
        },
        {
            "name": "runner_notify",
            "title": "Notify operator via runner popup",
            "annotations": {"readOnlyHint": False},
            "description": (
                "Show a popup on the runner machine and wait for operator response. "
                "Returns {ok, value, timed_out}. Requires HTTP daemon mode."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "runner_profile": {
                        "type": "string",
                        "description": "Runner profile name (e.g. mycader-1)",
                    },
                    "ui_spec": {
                        "type": "object",
                        "description": (
                            "Popup spec. type: choice|input|confirm|info|toast. "
                            "toast is fire-and-forget (no popup-result posted). "
                            "Other types block until operator responds. "
                            "Fields: title, body, options (choice), timeout_s."
                        ),
                    },
                },
                "required": ["runner_profile", "ui_spec"],
            },
        },
    ]
