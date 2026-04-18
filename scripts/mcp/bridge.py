"""FlywheelBridge — bridge execution and failure classification.

Extracted from EmergeDaemon so the composite + classifier logic can be tested
and reasoned about independently of the 1200-line daemon module.

Dependencies are injected at construction time; no daemon imports here.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from scripts.intent_registry import IntentRegistry

_log = logging.getLogger(__name__)


class FlywheelBridge:
    def __init__(
        self,
        *,
        state_root: Callable[[], Any],
        get_runner_router: Callable[[], Any],
        run_remotely: Callable[..., dict],
        run_local_read: Callable[[dict], dict],
        run_local_write: Callable[[dict], dict],
        record_bridge_outcome: Callable[..., dict],
        sink_emit: Callable[[str, dict], None],
    ) -> None:
        self._get_state_root = state_root
        self._get_runner_router = get_runner_router
        self._run_remotely = run_remotely
        self._run_local_read = run_local_read
        self._run_local_write = run_local_write
        self._record_bridge_outcome = record_bridge_outcome
        self._sink_emit = sink_emit
        self.last_failure: dict[str, Any] | None = None
        # External dispatch hook — allows daemon (and tests) to override the
        # top-level bridge entry point so that monkey-patches on the daemon's
        # _try_flywheel_bridge propagate into composite child execution.
        # Defaults to self.try_bridge; set by EmergeDaemon after construction.
        self._dispatch: "Callable[[dict], dict | None]" = self.try_bridge

    @staticmethod
    def _classify_bridge_failure(
        result: Any, mode: str, has_non_empty_baseline: bool,
        row_keys_sample: "frozenset[str] | None" = None,
    ) -> "dict[str, str] | None":
        """Classify a bridge result as a failure or success (pure function).

        Returns ``None`` on success, else a dict with keys
        ``reason``, ``demotion_reason`` for downstream recording.
        """
        if not isinstance(result, dict):
            return None

        if result.get("verification_state") == "degraded":
            verify_info = result.get("verify_result") or {}
            why = ""
            if isinstance(verify_info, dict):
                why = str(verify_info.get("why", "") or "")
            return {
                "reason": f"verify_degraded: {why}",
                "demotion_reason": "bridge_broken",
            }

        if mode == "read":
            rows = result.get("rows")
            is_empty = rows is None or (
                isinstance(rows, (list, tuple, dict, str)) and len(rows) == 0
            )
            if is_empty and has_non_empty_baseline:
                return {
                    "reason": "rows empty after non-empty baseline",
                    "demotion_reason": "bridge_silent_empty",
                }

            if (
                row_keys_sample is not None
                and not is_empty
                and isinstance(rows, list)
                and rows
                and isinstance(rows[0], dict)
            ):
                current_keys = frozenset(rows[0].keys())
                if current_keys != row_keys_sample:
                    added = sorted(current_keys - row_keys_sample)
                    removed = sorted(row_keys_sample - current_keys)
                    parts = []
                    if removed:
                        parts.append(f"removed: {removed}")
                    if added:
                        parts.append(f"added: {added}")
                    return {
                        "reason": f"schema_drift: {', '.join(parts)}",
                        "demotion_reason": "bridge_schema_drift",
                    }

        if mode == "write":
            action = result.get("action_result")
            if isinstance(action, dict) and action.get("ok") is False:
                err = str(action.get("error", "") or "")
                return {
                    "reason": f"action_not_ok: {err}",
                    "demotion_reason": "bridge_broken",
                }

        return None

    @staticmethod
    def _classify_bridge_success_non_empty(result: Any, mode: str) -> bool | None:
        """Return ``True`` if this is a read-mode bridge that produced
        non-empty rows, ``None`` otherwise. Used to latch the
        ``has_ever_returned_non_empty`` baseline."""
        if mode != "read" or not isinstance(result, dict):
            return None
        rows = result.get("rows")
        if rows is not None and not (
            isinstance(rows, (list, tuple, dict, str)) and len(rows) == 0
        ):
            return True
        return None

    @staticmethod
    def _extract_row_keys_sample(result: Any, mode: str) -> "frozenset[str] | None":
        """Return frozenset of top-level keys from the first row dict, or None.

        Used to latch a key-set baseline on first non-empty bridge success so
        subsequent runs can detect schema renames (bridge_schema_drift).
        Only meaningful for read-mode results with list-of-dict rows.
        """
        if mode != "read" or not isinstance(result, dict):
            return None
        rows = result.get("rows")
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            return frozenset(rows[0].keys())
        return None

    def try_bridge(self, arguments: dict[str, Any]) -> dict[str, Any] | None:
        base_pipeline_id = str(arguments.get("base_pipeline_id", "")).strip()
        if not base_pipeline_id:
            base_pipeline_id = str(arguments.get("intent_signature", "")).strip()
        if not base_pipeline_id:
            return None

        key = base_pipeline_id
        bridge_entry = IntentRegistry.get(self._get_state_root(), key)
        if not isinstance(bridge_entry, dict):
            return None
        if str(bridge_entry.get("stage", "explore")) != "stable":
            return None

        composed_from = bridge_entry.get("composed_from") or []
        if isinstance(composed_from, list) and composed_from:
            return self._run_composite_bridge(
                base_pipeline_id, list(composed_from), arguments,
            )

        parts = base_pipeline_id.split(".", 2)
        if len(parts) != 3:
            return None
        connector, mode, name = parts
        pipeline_args = {**arguments, "connector": connector, "pipeline": name}
        try:
            _rr = self._get_runner_router()
            _client = _rr.find_client(arguments) if _rr else None
            if _client is not None:
                result = self._run_remotely(mode, pipeline_args, _client)
            elif mode == "write":
                result = self._run_local_write(pipeline_args)
            else:
                result = self._run_local_read(pipeline_args)
        except Exception as _bridge_exc:
            _log.warning(
                "flywheel bridge failed for %s (%s), falling back to LLM: %s",
                base_pipeline_id, mode, _bridge_exc,
            )
            self.last_failure = {
                "pipeline_id": base_pipeline_id,
                "mode": mode,
                "reason": str(_bridge_exc),
            }
            try:
                self._record_bridge_outcome(
                    base_pipeline_id,
                    success=False,
                    reason=str(_bridge_exc),
                    exception_class=type(_bridge_exc).__name__,
                )
            except Exception:
                pass
            return None
        current = IntentRegistry.get(self._get_state_root(), base_pipeline_id) or {}
        has_baseline = bool(current.get("has_ever_returned_non_empty"))
        stored_keys = current.get("row_keys_sample")
        row_keys_sample = frozenset(stored_keys) if isinstance(stored_keys, list) else None
        failure = self._classify_bridge_failure(result, mode, has_baseline, row_keys_sample)
        if failure is not None:
            _log.warning(
                "flywheel bridge %s for %s (%s), falling back to LLM",
                failure["demotion_reason"], base_pipeline_id, mode,
            )
            self.last_failure = {
                "pipeline_id": base_pipeline_id,
                "mode": mode,
                "reason": failure["reason"],
            }
            try:
                self._record_bridge_outcome(
                    base_pipeline_id,
                    success=False,
                    reason=failure["reason"],
                    demotion_reason=failure["demotion_reason"],
                )
            except Exception:
                pass
            return None
        result["bridge_promoted"] = True
        try:
            self._sink_emit("flywheel.bridge.promoted", {"pipeline_id": base_pipeline_id})
        except Exception:
            pass
        try:
            bridge_non_empty = self._classify_bridge_success_non_empty(result, mode)
            new_keys = self._extract_row_keys_sample(result, mode)
            try:
                self._record_bridge_outcome(
                    base_pipeline_id, success=True, non_empty=bridge_non_empty,
                    row_keys_sample=new_keys,
                )
            except TypeError:
                self._record_bridge_outcome(
                    base_pipeline_id, success=True, non_empty=bridge_non_empty,
                )
        except Exception:
            pass
        return result

    def _run_composite_bridge(
        self,
        composite_id: str,
        children: list[str],
        arguments: dict[str, Any],
        _child_bridge_fn: "Callable[[dict], dict | None] | None" = None,
    ) -> dict[str, Any] | None:
        """Run each child intent's bridge sequentially, returning aggregated result.

        Each child receives the same ``arguments`` plus the previous child's
        result under ``__prev_result`` — callers can wire child pipelines to
        consume the upstream output. If any child bridge fails (returns None
        or raises), the composite is marked broken and the caller falls back
        to the LLM path.

        ``_child_bridge_fn`` is an optional override for child dispatch — used by
        ``EmergeDaemon`` to route children through its own ``_try_flywheel_bridge``
        so that test monkey-patches on the daemon propagate into composite execution.
        """
        child_bridge = _child_bridge_fn if _child_bridge_fn is not None else self._dispatch
        aggregated: dict[str, Any] = {
            "bridge_promoted": True,
            "composite": True,
            "composite_id": composite_id,
            "children": [],
        }
        prev_result: Any = None
        for child_id in children:
            child_args = {**arguments, "intent_signature": child_id}
            if prev_result is not None:
                child_args["__prev_result"] = prev_result
            child_args.pop("base_pipeline_id", None)
            try:
                child_result = child_bridge(child_args)
            except Exception as _exc:
                child_result = None
                self.last_failure = {
                    "pipeline_id": composite_id,
                    "mode": "composite",
                    "reason": f"child {child_id} raised: {_exc}",
                }
                try:
                    self._record_bridge_outcome(
                        composite_id,
                        success=False,
                        reason=f"child {child_id} raised: {_exc}",
                        exception_class=type(_exc).__name__,
                    )
                except Exception:
                    pass
                return None
            if child_result is None:
                self.last_failure = {
                    "pipeline_id": composite_id,
                    "mode": "composite",
                    "reason": f"child {child_id} bridge returned None",
                }
                try:
                    self._record_bridge_outcome(
                        composite_id,
                        success=False,
                        reason=f"child {child_id} bridge unavailable",
                        exception_class="CompositeChildMissing",
                    )
                except Exception:
                    pass
                return None
            aggregated["children"].append({"intent": child_id, "result": child_result})
            prev_result = child_result
        try:
            self._sink_emit("flywheel.bridge.composite", {
                "pipeline_id": composite_id,
                "children": children,
            })
        except Exception:
            pass
        try:
            self._record_bridge_outcome(composite_id, success=True)
        except Exception:
            pass
        return aggregated
