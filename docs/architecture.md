# Emerge Architecture Layers

Emerge keeps deterministic mechanisms in Python and moves behavioral guidance into markdown assets that Claude Code agents can read, critique, and revise without changing runtime contracts.

## Mechanism Layer

Python owns stable, testable mechanisms:

- `scripts/emerge_daemon.py` and `scripts/daemon_http.py`: MCP/HTTP control plane, runner SSE hub, and tool dispatch.
- `scripts/policy_engine.py` and `scripts/intent_registry.py`: the only lifecycle mutation path for intent `stage` and registry writes.
- `scripts/pipeline_engine.py` and `scripts/exec_session.py`: zero-inference pipeline execution and WAL/session persistence.
- `scripts/synthesis_agent.py` and `scripts/synthesis_coordinator.py`: job packaging, validation, smoke checks, and artifact materialization.
- `scripts/admin/*`, `scripts/operator_popup.py`, and hooks: HTTP, CLI, state, popup, and hook transport primitives.

Mechanism code should reject malformed input, preserve path containment, and keep API shape stable.

## Behavior Layer

Markdown assets carry operational judgment:

- `skills/emerge-forward-synthesis` and `skills/emerge-reverse-synthesis`: lead-agent distillation contracts.
- `skills/cockpit-rendering`: cockpit copy, empty states, and presentation rules.
- `skills/runner-elicitation-policy`: when runner popups should interrupt and how results are classified.
- `skills/admin-runner-operations`: runner install/deploy/status workflow.
- `commands/admin-batch-update-runners.md` and `commands/diagnose-stuck-flywheel.md`: repeatable admin workflows.
- `docs/hooks/*.md`: hook copy injected by mechanism hooks.

Behavior assets must not become hidden policy writers. They describe how agents should operate; Python still validates every submitted result.

## Perception Layer

Watcher agents observe runner-side context and emit suggestions:

- `agents/operator-watcher.md`: generic watcher contract.
- `agents/connector-watcher-template.md`: template for watchers generated from connector-local `watcher_profile.yaml`.
- `agents/forward-distiller.md`: lead-agent role for forward synthesis jobs.

Watcher agents are markdown-only behavior. Product assets must not hard-code vertical names; connector-specific differences belong in connector configuration. Watchers can read configured sources and emit redacted suggestions, but must not write pipelines, policy state, or Memory Hub data.

## Runner Boundary

Runners execute local primitives and forward events. They do not own promotion:

- Popups render locally through `scripts/operator_popup.py`; policy for when to interrupt lives in `skills/runner-elicitation-policy`.
- Runner deploy/install/status code remains in Python; rollout workflow lives in `skills/admin-runner-operations` and commands.
- Synthesis results return to the orchestrator via `icc_synthesis_submit`; runners never directly materialize product pipelines.
