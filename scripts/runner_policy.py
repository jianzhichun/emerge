from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from scripts import runner_emit


def _stable_id(prefix: str, payload: dict[str, Any]) -> str:
    material = json.dumps(payload, sort_keys=True, ensure_ascii=True, default=str)
    return f"{prefix}-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:16]}"


class EvidenceForwardingPolicy:
    """Runner-role PolicyEngine shim.

    It preserves the call surface used by span/exec/bridge recorders, but never
    mutates local lifecycle state. Every meaningful observation is forwarded to
    the orchestrator for the real PolicyEngine to apply.
    """

    def apply_evidence(
        self,
        intent_signature: str,
        *,
        success: bool,
        anchor_type: str = "self_report",
        evidence_unit_id: str | None = None,
        verify_observed: bool = False,
        verify_passed: bool = False,
        human_fix: bool = False,
        is_degraded: bool = False,
        description: str = "",
        is_read_only: bool | None = None,
        target_profile: str = "default",
        execution_path: str | None = None,
        policy_action: str | None = None,
        policy_enforced: bool = False,
        stop_triggered: bool = False,
        rollback_executed: bool = False,
        ts_ms: int | None = None,
    ) -> dict[str, Any]:
        ts = int(ts_ms if ts_ms is not None else time.time() * 1000)
        unit_id = evidence_unit_id or _stable_id(
            "evidence",
            {
                "intent_signature": intent_signature,
                "success": success,
                "verify_observed": verify_observed,
                "verify_passed": verify_passed,
                "ts_ms": ts,
            },
        )
        event = {
            "type": "evidence_report",
            "message_id": unit_id,
            "intent_signature": intent_signature,
            "success": bool(success),
            "anchor_type": anchor_type,
            "evidence_unit_id": unit_id,
            "verify_observed": bool(verify_observed),
            "verify_passed": bool(verify_passed),
            "human_fix": bool(human_fix),
            "is_degraded": bool(is_degraded),
            "description": description,
            "is_read_only": is_read_only,
            "target_profile": target_profile,
            "execution_path": execution_path,
            "policy_action": policy_action,
            "policy_enforced": bool(policy_enforced),
            "stop_triggered": bool(stop_triggered),
            "rollback_executed": bool(rollback_executed),
            "ts_ms": ts,
        }
        runner_emit.emit_event(event)
        return {"forwarded": True, "intent_signature": intent_signature, "evidence_unit_id": unit_id}

    def record_bridge_outcome(
        self,
        intent_signature: str,
        *,
        success: bool,
        reason: str | None = None,
        exception_class: str | None = None,
        demotion_reason: str = "bridge_broken",
        non_empty: bool | None = None,
        ts_ms: int | None = None,
        row_keys_sample=None,
    ) -> dict[str, Any]:
        event = {
            "type": "bridge_outcome_report",
            "message_id": _stable_id(
                "bridge",
                {
                    "intent_signature": intent_signature,
                    "success": success,
                    "reason": reason,
                    "ts_ms": ts_ms,
                },
            ),
            "intent_signature": intent_signature,
            "success": bool(success),
            "reason": reason or "",
            "exception_class": exception_class or "",
            "demotion_reason": demotion_reason,
            "non_empty": non_empty,
            "row_keys_sample": sorted(row_keys_sample) if row_keys_sample is not None else None,
            "ts_ms": int(ts_ms if ts_ms is not None else time.time() * 1000),
        }
        runner_emit.emit_event(event)
        return {"forwarded": True, "intent_signature": intent_signature}

    def register_composite(self, intent_signature: str, **_kwargs: Any) -> dict[str, Any]:
        runner_emit.emit_event(
            {
                "type": "forbidden_policy_write",
                "message_id": _stable_id("forbidden", {"intent_signature": intent_signature, "op": "register_composite"}),
                "operation": "register_composite",
                "intent_signature": intent_signature,
            }
        )
        return {}

    def increment_human_fix(self, intent_signature: str) -> dict[str, Any]:
        return self.apply_evidence(intent_signature, success=False, human_fix=True)
