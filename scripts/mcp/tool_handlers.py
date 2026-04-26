"""ToolHandlers — per-tool MCP handler methods extracted from EmergeDaemon.

Each public method maps 1:1 to an MCP tool name. Dependencies are injected
so this module has zero import from emerge_daemon.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts.node_role import is_runner_role


class ToolHandlers:
    def __init__(
        self,
        *,
        bridge,
        flywheel,
        policy_engine,
        crystallize_fn,
        get_session,
        resolve_exec_code,
        get_runner_router,
        run_connector_pipeline,
        run_pipeline_remotely,
        state_root,
        write_operator_event,
        append_warning_text,
        get_http_server,
        sink_emit,
        tool_error,
        tool_ok_json,
    ) -> None:
        self._bridge = bridge
        # Default child dispatch — daemon overrides post-construction so that
        # monkey-patches on _try_flywheel_bridge propagate through icc_exec.
        self._try_bridge_fn = bridge.try_bridge
        self._flywheel = flywheel
        self._policy_engine = policy_engine
        self._crystallize_fn = crystallize_fn
        self._get_session = get_session
        self._resolve_exec_code = resolve_exec_code
        self._get_runner_router = get_runner_router
        self._run_connector_pipeline = run_connector_pipeline
        self._run_pipeline_remotely = run_pipeline_remotely
        self._get_state_root = state_root
        self._write_operator_event = write_operator_event
        self._append_warning_text = append_warning_text
        self._get_http_server = get_http_server
        self._sink_emit = sink_emit
        self._tool_error = tool_error
        self._tool_ok_json = tool_ok_json

    def handle_icc_exec(self, arguments: dict[str, Any]) -> dict[str, Any]:
        promoted = self._try_bridge_fn(arguments)
        if promoted is not None:
            response = {"isError": False, "content": [{"type": "text", "text": json.dumps(promoted)}]}
            try:
                _rr = self._get_runner_router()
                _client = _rr.find_client(arguments) if _rr else None
                self._flywheel.record_pipeline_event(
                    tool_name="icc_exec",
                    arguments=arguments,
                    result=promoted,
                    is_error=False,
                    execution_path="remote" if _client is not None else "local",
                )
            except Exception:
                pass
            _bsig = str(arguments.get("intent_signature", "")).strip()
            if _bsig and not arguments.get("no_replay"):
                self._write_operator_event(_bsig, is_error=False)
            return response
        try:
            mode = str(arguments.get("mode", "inline_code"))
            target_profile = str(arguments.get("target_profile", "default"))
            candidate_key = self._flywheel.resolve_exec_candidate_key(
                arguments=arguments,
                target_profile=target_profile,
            )
            sampled_in_policy = self._flywheel.should_sample(candidate_key)
            _rr = self._get_runner_router()
            _exec_client = _rr.find_client(arguments) if _rr else None
            execution_path = "remote" if _exec_client is not None else "local"
            if _exec_client is not None:
                remote_args = dict(arguments)
                if mode == "script_ref":
                    remote_args["code"] = self._resolve_exec_code(mode=mode, arguments=arguments)
                    remote_args["mode"] = "inline_code"
                result = _exec_client.call_tool("icc_exec", remote_args)
            else:
                code = self._resolve_exec_code(mode=mode, arguments=arguments)
                repl = self._get_session(target_profile)
                metadata = {
                    "mode": mode,
                    "target_profile": target_profile,
                    "intent_signature": arguments.get("intent_signature", ""),
                    "script_ref": arguments.get("script_ref", ""),
                    "no_replay": bool(arguments.get("no_replay", False)),
                }
                if isinstance(arguments.get("script_args"), dict):
                    metadata["script_args"] = dict(arguments.get("script_args") or {})
                for key in ("source", "synthesis_job_id", "source_intent_signature"):
                    value = arguments.get(key)
                    if value:
                        metadata[key] = str(value)
                result = repl.exec_code(
                    code,
                    metadata=metadata,
                    inject_vars={"__args": arguments.get("script_args", {})},
                    result_var=str(arguments.get("result_var", "")).strip() or None,
                )
            try:
                self._flywheel.record_exec_event(
                    arguments=arguments,
                    result=result,
                    target_profile=target_profile,
                    mode=mode,
                    execution_path=execution_path,
                    sampled_in_policy=sampled_in_policy,
                    candidate_key=candidate_key,
                )
            except Exception as exc:
                self._append_warning_text(result, f"policy bookkeeping failed: {exc}")
            _sig = str(arguments.get("intent_signature", ""))
            if _sig and not arguments.get("no_replay"):
                self._write_operator_event(_sig, is_error=bool(result.get("isError")))
            if "isError" not in result:
                result["isError"] = False
            _bf = self._bridge.last_failure
            if _bf:
                self._bridge.last_failure = None
                self._append_warning_text(
                    result,
                    f"bridge fallback: {_bf['pipeline_id']} ({_bf['mode']}) failed: "
                    f"{_bf['reason']}. Falling back to LLM inference.",
                )
            return result
        except Exception as exc:
            return self._tool_error(f"icc_exec failed: {exc}")

    def handle_icc_crystallize(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if is_runner_role():
            return self._tool_error(
                "icc_crystallize is orchestrator-only. Runner instances must send pattern_suggestion upstream."
            )
        try:
            intent_signature = str(arguments.get("intent_signature", "")).strip()
            connector = str(arguments.get("connector", "")).strip()
            pipeline_name = str(arguments.get("pipeline_name", "")).strip()
            mode = str(arguments.get("mode", "read")).strip()
            target_profile = str(arguments.get("target_profile", "default")).strip()
            _persistent_raw = arguments.get("persistent", False)
            if isinstance(_persistent_raw, str):
                persistent = _persistent_raw.strip().lower() in ("1", "true", "yes", "on")
            else:
                persistent = bool(_persistent_raw)
            if not all([intent_signature, connector, pipeline_name, mode]):
                return self._tool_error(
                    "icc_crystallize: intent_signature, connector, pipeline_name, and mode are required"
                )
            if mode not in ("read", "write"):
                return self._tool_error(
                    f"icc_crystallize: mode must be 'read' or 'write', got {mode!r}"
                )
            return self._crystallize_fn(
                intent_signature=intent_signature,
                connector=connector,
                pipeline_name=pipeline_name,
                mode=mode,
                target_profile=target_profile,
                persistent=persistent,
            )
        except Exception as exc:
            return self._tool_error(f"icc_crystallize failed: {exc}")

    def handle_icc_compose(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if is_runner_role():
            return self._tool_error(
                "icc_compose is orchestrator-only. Runner instances must send composition suggestions upstream."
            )
        from scripts.policy_config import PIPELINE_KEY_RE
        from scripts.intent_registry import IntentRegistry

        parent = str(arguments.get("intent_signature", "")).strip()
        children_raw = arguments.get("children")
        if not parent:
            return self._tool_error("icc_compose: intent_signature is required")
        if not isinstance(children_raw, list) or not children_raw:
            return self._tool_error(
                "icc_compose: children must be a non-empty list of intent_signatures"
            )
        children = [str(c).strip() for c in children_raw if str(c).strip()]
        if len(children) < 2:
            return self._tool_error(
                "icc_compose: composition requires at least 2 children — "
                "a single child is just the child itself"
            )

        if not PIPELINE_KEY_RE.match(parent):
            return self._tool_error(
                f"icc_compose: parent intent_signature {parent!r} must match connector.mode.name"
            )
        for c in children:
            if not PIPELINE_KEY_RE.match(c):
                return self._tool_error(
                    f"icc_compose: child {c!r} must match connector.mode.name"
                )

        reg = IntentRegistry.load(self._get_state_root())
        intents = reg["intents"]
        missing = [c for c in children if c not in intents]
        if missing:
            return self._tool_error(
                f"icc_compose: children must exist before composition — missing: {missing}"
            )

        def _reaches(node: str, target: str, seen: set[str]) -> bool:
            if node == target:
                return True
            if node in seen:
                return False
            seen.add(node)
            entry = intents.get(node)
            if not isinstance(entry, dict):
                return False
            for nxt in entry.get("composed_from") or []:
                if _reaches(str(nxt), target, seen):
                    return True
            return False

        for c in children:
            if c == parent or _reaches(c, parent, set()):
                return self._tool_error(
                    f"icc_compose: child {c!r} would create a cycle — "
                    f"{parent!r} already reachable from it"
                )

        description = str(arguments.get("description", "") or "").strip()
        entry = self._policy_engine.register_composite(
            parent, children=children, description=description,
        )
        min_stage = str(entry.get("stage", "explore"))

        payload = {
            "ok": True,
            "intent_signature": parent,
            "children": children,
            "stage": min_stage,
            "next_step": (
                f"Composite registered. Call icc_exec(intent_signature={parent!r}) — "
                "children will bridge-execute in order. If any child is non-stable, "
                "the composite falls back to LLM."
            ),
        }
        return {
            "isError": False,
            "structuredContent": payload,
            "content": [{"type": "text", "text": json.dumps(payload)}],
        }

    def handle_icc_reconcile(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if is_runner_role():
            return self._tool_error(
                "icc_reconcile is orchestrator-only. Runner instances must forward operator feedback upstream."
            )
        from scripts.policy_config import default_hook_state_root
        from scripts.state_tracker import with_locked_tracker

        delta_id = str(arguments.get("delta_id", "")).strip()
        outcome = str(arguments.get("outcome", "")).strip()
        intent_signature = str(arguments.get("intent_signature", "")).strip()
        if not delta_id:
            return self._tool_error("icc_reconcile: delta_id is required")
        if outcome not in ("confirm", "correct", "retract"):
            return self._tool_error(
                f"icc_reconcile: 'outcome' must be confirm/correct/retract, got {outcome!r}"
            )
        state_path = Path(default_hook_state_root()) / "state.json"
        def _mutate(tracker):
            tracker.reconcile_delta(delta_id, outcome)
            return tracker.to_dict()

        td = with_locked_tracker(state_path, _mutate)
        if intent_signature:
            from scripts.policy_config import PIPELINE_KEY_RE
            if PIPELINE_KEY_RE.match(intent_signature):
                if outcome == "confirm":
                    # Human explicitly confirmed the output was correct — strongest
                    # possible external anchor; count as operator_action evidence.
                    self._policy_engine.apply_evidence(
                        intent_signature, success=True, anchor_type="operator_action",
                    )
                elif outcome == "retract":
                    # Human retracted — the output was wrong.
                    self._policy_engine.apply_evidence(
                        intent_signature, success=False, anchor_type="operator_action",
                    )
            if outcome == "correct":
                self._flywheel.increment_human_fix(intent_signature)
        return self._tool_ok_json({
            "delta_id": delta_id,
            "outcome": outcome,
            "intent_signature": intent_signature or None,
            "verification_state": td.get("verification_state", "unverified"),
        })

    def handle_icc_hub(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from scripts.mcp.hub_handler import handle_icc_hub
        return handle_icc_hub(
            arguments,
            tool_error=self._tool_error,
            tool_ok_json=self._tool_ok_json,
        )

    def handle_runner_notify(self, arguments: dict[str, Any]) -> dict[str, Any]:
        runner_profile = str(arguments.get("runner_profile", "")).strip()
        ui_spec = arguments.get("ui_spec", {})
        if not runner_profile:
            return self._tool_error("runner_notify: runner_profile is required")
        if not isinstance(ui_spec, dict):
            return self._tool_error("runner_notify: ui_spec must be an object")
        http_srv = self._get_http_server()
        if http_srv is None:
            return self._tool_error("runner_notify requires HTTP daemon mode (--http flag)")
        result = http_srv.request_popup(runner_profile, ui_spec)
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}
