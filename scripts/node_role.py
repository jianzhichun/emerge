from __future__ import annotations

import os
from enum import Enum


class NodeRole(str, Enum):
    ORCHESTRATOR = "orchestrator"
    RUNNER = "runner"


def current_node_role() -> NodeRole:
    raw = os.environ.get("EMERGE_NODE_ROLE", "").strip().lower()
    if not raw and os.environ.get("EMERGE_RUNNER_MODE", "").strip().lower() in {"1", "true", "yes", "on"}:
        raw = NodeRole.RUNNER.value
    if raw == NodeRole.RUNNER.value:
        return NodeRole.RUNNER
    return NodeRole.ORCHESTRATOR


def is_runner_role() -> bool:
    return current_node_role() == NodeRole.RUNNER
