"""Span tool handlers for EmergeDaemon.

SpanHandlers owns icc_span_open / icc_span_close / icc_span_approve.
All daemon state is accessed via injected callables so the class can be
tested independently.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Callable


class CompositeBridgeUnavailable(Exception):
    """Composite `_try_flywheel_bridge` returned None; bridge failure already recorded."""


class SpanHandlers:
    """Implements the three span MCP tool handlers."""

    def __init__(
        self,
        *,
        span_tracker: Any,          # SpanTracker instance
        open_spans: dict,           # span_id → SpanRecord; shared mutable ref
        intent_gate: set,           # shared mutable set of confirmed-new intents
        save_intent_gate: Callable[[], None],
        generate_skeleton: Callable[..., "Path | None"],   # (intent_signature, span) → Path|None
        sink: Callable[[], Any],    # () → metrics sink with .emit()
        run_pipeline: Callable,     # (mode, args) → (result_dict, exec_path_str)
        record_pipeline_event: Callable,  # FlywheelRecorder.record_pipeline_event
        record_bridge_outcome: Callable | None = None,  # PolicyEngine.record_bridge_outcome
        tool_error: Callable[[str], dict],
        tool_ok_json: Callable[[Any], dict],
    ) -> None:
        self._span_tracker = span_tracker
        self._open_spans = open_spans
        self._intent_gate = intent_gate
        self._save_intent_gate = save_intent_gate
        self._generate_skeleton = generate_skeleton
        self._get_sink = sink
        self._run_pipeline = run_pipeline
        self._record_pipeline_event = record_pipeline_event
        self._record_bridge_outcome = record_bridge_outcome
        self._tool_error = tool_error
        self._tool_ok_json = tool_ok_json

    # ------------------------------------------------------------------
    # icc_span_open
    # ------------------------------------------------------------------

    def handle_span_open(self, arguments: dict[str, Any]) -> dict[str, Any]:
        intent_signature = str(arguments.get("intent_signature", "")).strip()
        if not intent_signature:
            return self._tool_error("icc_span_open: 'intent_signature' is required")

        policy_status = self._span_tracker.get_policy_status(intent_signature)

        # Bridge check: stable policy → zero-LLM execution
        if policy_status == "stable":
            parts = intent_signature.split(".", 2)
            if len(parts) == 3:
                connector, mode, pipeline_name = parts
                pipeline_args = {**arguments, "connector": connector, "pipeline": pipeline_name}
                try:
                    bridge_result, exec_path = self._run_pipeline(mode, pipeline_args)
                    bridge_result["bridge_promoted"] = True
                    try:
                        self._record_pipeline_event(
                            tool_name="icc_span_open",
                            arguments=pipeline_args,
                            result=bridge_result,
                            is_error=False,
                            execution_path=exec_path,
                        )
                    except Exception:
                        pass
                    try:
                        self._get_sink().emit("span.bridge.promoted", {"intent_signature": intent_signature})
                    except Exception:
                        pass
                    if self._record_bridge_outcome is not None:
                        try:
                            self._record_bridge_outcome(intent_signature, success=True)
                        except Exception:
                            pass
                    return self._tool_ok_json({
                        "bridge": True,
                        "bridge_type": "result",
                        "intent_signature": intent_signature,
                        "result": bridge_result,
                    })
                except CompositeBridgeUnavailable:
                    # PolicyEngine already recorded bridge failure for the composite.
                    pass
                except Exception as _bridge_exc:
                    if self._record_bridge_outcome is not None:
                        try:
                            self._record_bridge_outcome(
                                intent_signature,
                                success=False,
                                reason=str(_bridge_exc),
                                exception_class=type(_bridge_exc).__name__,
                            )
                        except Exception:
                            pass
                    # PipelineMissingError or any failure → fall through to explore

        # Intent gate: new intent for existing connector → confirm_needed
        _candidates = self._span_tracker._load_candidates()["intents"]
        if intent_signature not in _candidates:
            _connector = intent_signature.split(".", 1)[0]
            _same_connector = {
                k: v for k, v in _candidates.items()
                if k.startswith(f"{_connector}.")
            }
            if _same_connector and intent_signature not in self._intent_gate:
                self._intent_gate.add(intent_signature)
                self._save_intent_gate()
                _items = [
                    f"{_k} ({_v.get('successes', 0)}/{_v.get('attempts', 0)})"
                    for _k, _v in sorted(
                        _same_connector.items(),
                        key=lambda x: -x[1].get("attempts", 0),
                    )[:5]
                ]
                return self._tool_ok_json({
                    "status": "confirm_needed",
                    "intent_signature": intent_signature,
                    "existing_intents": _items,
                    "message": (
                        f"Safety gate: '{intent_signature}' is a new intent for connector '{_connector}'. "
                        f"Existing intents: {', '.join(_items)}. "
                        "If this is intentional, re-call icc_span_open with the exact same "
                        "intent_signature to confirm — the span will open normally. "
                        "Or switch to an existing intent above."
                    ),
                })

        # Open a new span
        try:
            span = self._span_tracker.open_span(
                intent_signature=intent_signature,
                description=str(arguments.get("description", "")).strip(),
                args=arguments.get("args") or {},
                source=str(arguments.get("source", "manual")).strip(),
                skill_name=str(arguments.get("skill_name", "") or "").strip() or None,
            )
        except RuntimeError as exc:
            return self._tool_error(str(exc))

        self._open_spans[span.span_id] = span
        return self._tool_ok_json({
            "span_id": span.span_id,
            "intent_signature": intent_signature,
            "status": "opened",
            "policy_status": policy_status,
        })

    # ------------------------------------------------------------------
    # icc_span_close
    # ------------------------------------------------------------------

    def handle_span_close(self, arguments: dict[str, Any]) -> dict[str, Any]:
        outcome = str(arguments.get("outcome", "")).strip()
        if outcome not in ("success", "failure", "aborted"):
            return self._tool_error(
                f"icc_span_close: 'outcome' must be success/failure/aborted, got {outcome!r}"
            )
        span_id = str(arguments.get("span_id", "")).strip()
        result_summary = arguments.get("result_summary") or {}

        from scripts.span_tracker import SpanRecord
        span = self._open_spans.pop(span_id, None)
        if span is None:
            import uuid as _uuid
            span = SpanRecord(
                span_id=span_id or str(_uuid.uuid4()),
                intent_signature=str(arguments.get("intent_signature", "")).strip(),
                description="",
                source="manual",
                opened_at_ms=0,
            )

        closed = self._span_tracker.close_span(span, outcome=outcome, result_summary=result_summary)
        policy_status = self._span_tracker.get_policy_status(closed.intent_signature)
        synthesis_ready = self._span_tracker.is_synthesis_ready(closed.intent_signature)
        skeleton_path: str | None = None

        if synthesis_ready and not self._span_tracker.skeleton_already_generated(closed.intent_signature):
            latest = self._span_tracker.latest_successful_span(closed.intent_signature)
            if latest:
                generated = self._generate_skeleton(
                    intent_signature=closed.intent_signature,
                    span=latest,
                )
                if generated:
                    skeleton_path = str(generated)
                    self._span_tracker.mark_skeleton_generated(closed.intent_signature)
                    try:
                        self._get_sink().emit("span.skeleton_generated", {
                            "intent_signature": closed.intent_signature,
                            "path": skeleton_path,
                        })
                    except Exception:
                        pass

        response: dict[str, Any] = {
            "span_id": closed.span_id,
            "intent_signature": closed.intent_signature,
            "outcome": outcome,
            "policy_status": policy_status,
            "synthesis_ready": synthesis_ready,
            "is_read_only": closed.is_read_only,
        }
        if skeleton_path:
            response["skeleton_path"] = skeleton_path
            # Auto-activate at stable unless opted out. The flywheel's whole
            # point is zero-LLM repeat execution; leaving pipelines in _pending
            # awaiting manual icc_span_approve means every future session pays
            # LLM cost on an intent we already learned.
            require_manual = os.environ.get("EMERGE_REQUIRE_APPROVE", "0") == "1"
            activated_path: str | None = None
            if not require_manual:
                activated_path = self._auto_activate_pipeline(closed.intent_signature)
            if activated_path:
                response["auto_activated"] = True
                response["pipeline_path"] = activated_path
                response["next_step"] = (
                    f"Pipeline activated at {activated_path}. "
                    "Bridge is live — next call hits zero-LLM path."
                )
            else:
                response["next_step"] = (
                    f"Review and complete {skeleton_path}, "
                    "then call icc_span_approve to activate the bridge."
                )
        return self._tool_ok_json(response)

    def _auto_activate_pipeline(self, intent_signature: str) -> str | None:
        """Promote a stable skeleton from _pending/ to active without human gate.

        Returns the activated .py path on success, None if auto-activation is
        not applicable (wrong stage, missing skeleton, or write failure).
        Failures are swallowed — caller falls back to the manual-approve path.
        """
        try:
            if self._span_tracker.get_policy_status(intent_signature) != "stable":
                return None
            result = self.handle_span_approve({"intent_signature": intent_signature})
        except Exception:
            return None
        if not isinstance(result, dict) or result.get("isError"):
            return None
        try:
            import json as _json
            body = _json.loads(result["content"][0]["text"])
        except Exception:
            return None
        return str(body.get("pipeline_path") or "") or None

    # ------------------------------------------------------------------
    # icc_span_approve
    # ------------------------------------------------------------------

    def handle_span_approve(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from scripts.policy_config import resolve_connector_root
        from scripts.crystallizer import IndentedSafeDumper

        intent_signature = str(arguments.get("intent_signature", "")).strip()
        if not intent_signature:
            return self._tool_error("icc_span_approve: 'intent_signature' is required")

        policy_status = self._span_tracker.get_policy_status(intent_signature)
        if policy_status != "stable":
            return self._tool_error(
                f"icc_span_approve: intent '{intent_signature}' is not stable "
                f"(status={policy_status}). Only stable spans can be approved."
            )

        parts = intent_signature.split(".", 2)
        if len(parts) != 3:
            return self._tool_error(
                f"icc_span_approve: cannot parse connector/mode/name from '{intent_signature}'"
            )
        connector, mode, pipeline_name = parts
        target_root = resolve_connector_root()
        pending_py = target_root / connector / "pipelines" / mode / "_pending" / f"{pipeline_name}.py"

        if not pending_py.exists():
            return self._tool_error(
                f"icc_span_approve: skeleton not found at {pending_py}. "
                "Run icc_span_close to generate the skeleton first, then implement it before "
                "approving. Check _pending/ directory."
            )

        real_dir = target_root / connector / "pipelines" / mode
        real_dir.mkdir(parents=True, exist_ok=True)
        real_py = real_dir / f"{pipeline_name}.py"
        real_yaml = real_dir / f"{pipeline_name}.yaml"

        fd, tmp_py = tempfile.mkstemp(prefix=".approve-", dir=str(real_dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(pending_py.read_text(encoding="utf-8"))
            os.replace(tmp_py, real_py)
        except Exception as exc:
            if os.path.exists(tmp_py):
                os.unlink(tmp_py)
            return self._tool_error(f"icc_span_approve: failed to move skeleton: {exc}")

        pending_py.unlink(missing_ok=True)

        mode_step_key = "read_steps" if mode == "read" else "write_steps"
        yaml_data: dict[str, Any] = {
            "intent_signature": intent_signature,
            "rollback_or_stop_policy": "stop",
            mode_step_key: ["run_read" if mode == "read" else "run_write"],
            "verify_steps": ["verify_read" if mode == "read" else "verify_write"],
            "span_approved": True,
        }
        try:
            yaml_src = IndentedSafeDumper.dump_yaml(yaml_data)
            fd2, tmp_yaml = tempfile.mkstemp(prefix=".approve-yaml-", dir=str(real_dir))
            try:
                with os.fdopen(fd2, "w", encoding="utf-8") as f:
                    f.write(yaml_src)
                os.replace(tmp_yaml, real_yaml)
            except Exception:
                if os.path.exists(tmp_yaml):
                    os.unlink(tmp_yaml)
                raise
        except Exception as exc:
            return self._tool_error(f"icc_span_approve: failed to generate YAML: {exc}")

        try:
            self._get_sink().emit("span.approved", {"intent_signature": intent_signature})
        except Exception:
            pass

        return self._tool_ok_json({
            "approved": True,
            "intent_signature": intent_signature,
            "pipeline_path": str(real_py),
            "yaml_path": str(real_yaml),
            "bridge_active": True,
            "message": (
                f"Pipeline activated at {real_py}. "
                "Future icc_span_open calls will bridge directly to this pipeline."
            ),
        })
