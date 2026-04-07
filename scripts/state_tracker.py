from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


LEVEL_CORE_CRITICAL = "core_critical"
LEVEL_CORE_SECONDARY = "core_secondary"
LEVEL_PERIPHERAL = "peripheral"
MAX_GOAL_CHARS = 120
MAX_DELTAS = 500  # hard cap per session; pre_compact resets on compaction


class StateTracker:
    def __init__(self, state: dict[str, Any] | None = None) -> None:
        if state is None:
            self.state = {
                "goal": "",
                "goal_source": "unset",
                "open_risks": [],
                "deltas": [],
                "verification_state": "verified",
                "consistency_window_ms": 0,
            }
        else:
            self.state = _normalize_state(state)

    def set_goal(self, goal: str, source: str = "unknown") -> None:
        sanitized = str(goal).strip()
        if len(sanitized) > MAX_GOAL_CHARS:
            sanitized = sanitized[:MAX_GOAL_CHARS]
        self.state["goal"] = sanitized
        self.state["goal_source"] = source

    def set_consistency_window(self, window_ms: int) -> None:
        try:
            self.state["consistency_window_ms"] = max(0, int(window_ms))
        except Exception:
            self.state["consistency_window_ms"] = 0

    def add_delta(
        self,
        message: str,
        level: str = LEVEL_CORE_CRITICAL,
        verification_state: str = "verified",
        provisional: bool = False,
        intent_signature: str | None = None,
        tool_name: str | None = None,
        ts_ms: int | None = None,
    ) -> str:
        message = str(message).strip() or "(no message)"
        if ts_ms is None:
            ts_ms = int(time.time() * 1000)
        delta_id = f"d-{int(time.time() * 1000)}-{len(self.state['deltas'])}"
        self.state["deltas"].append(
            {
                "id": delta_id,
                "message": message,
                "level": level,
                "verification_state": verification_state,
                "provisional": provisional,
                "intent_signature": intent_signature,
                "tool_name": tool_name,
                "ts_ms": ts_ms,
            }
        )
        if verification_state == "degraded":
            self.state["verification_state"] = "degraded"
        # Hard cap: keep the most recent MAX_DELTAS entries to prevent unbounded growth.
        # pre_compact.py resets the tracker on compaction, so this only matters for
        # very long sessions with many icc_* calls.
        if len(self.state["deltas"]) > MAX_DELTAS:
            self.state["deltas"] = self.state["deltas"][-MAX_DELTAS:]
        return delta_id

    def add_risk(
        self,
        risk: str,
        intent_signature: str | None = None,
        source_delta_id: str | None = None,
    ) -> None:
        text = str(risk).strip()
        if not text:
            return
        for existing in self.state["open_risks"]:
            if isinstance(existing, dict) and existing.get("text") == text:
                return
            if isinstance(existing, str) and existing == text:
                return
        risk_id = "r-" + hashlib.sha256(text.encode()).hexdigest()[:12]
        self.state["open_risks"].append(
            {
                "risk_id": risk_id,
                "text": text,
                "status": "open",
                "created_at_ms": int(time.time() * 1000),
                "snoozed_until_ms": None,
                "handled_reason": None,
                "source_delta_id": source_delta_id,
                "intent_signature": intent_signature,
            }
        )

    def update_risk(
        self,
        risk_id: str,
        action: str,
        reason: str | None = None,
        snooze_duration_ms: int | None = None,
    ) -> None:
        if action not in ("handle", "snooze", "reopen"):
            raise ValueError(f"update_risk: action must be handle/snooze/reopen, got {action!r}")
        for r in self.state["open_risks"]:
            if not isinstance(r, dict):
                continue
            if r.get("risk_id") == risk_id:
                if action == "handle":
                    r["status"] = "handled"
                    r["handled_reason"] = reason
                elif action == "snooze":
                    r["status"] = "snoozed"
                    r["snoozed_until_ms"] = int(time.time() * 1000) + (snooze_duration_ms or 3600000)
                elif action == "reopen":
                    r["status"] = "open"
                    r["snoozed_until_ms"] = None
                    r["handled_reason"] = None
                break

    def mark_degraded(self, reason: str) -> None:
        self.state["verification_state"] = "degraded"
        self.add_risk(reason)

    def reconcile_delta(self, delta_id: str, outcome: str) -> None:
        if outcome not in {"confirm", "correct", "retract"}:
            raise ValueError(f"reconcile_delta: outcome must be confirm/correct/retract, got {outcome!r}")
        for delta in self.state["deltas"]:
            if delta["id"] == delta_id:
                delta["provisional"] = False
                delta["reconcile_outcome"] = outcome
                if outcome == "retract":
                    delta["verification_state"] = "degraded"
                    self.state["verification_state"] = "degraded"
                break

    def can_auto_chain_high_risk_write(self) -> bool:
        return self.state.get("verification_state") != "degraded"

    def format_context(
        self,
        budget_chars: int | None = None,
        goal_override: str | None = None,
        goal_source_override: str | None = None,
    ) -> dict[str, str]:
        critical, secondary, peripheral = self._partition_deltas()

        delta_lines = [f"- {d['message']}" for d in critical]
        if secondary:
            delta_lines.extend([f"- {d['message']}" for d in secondary])
        if peripheral:
            delta_lines.extend([f"- {d['message']}" for d in peripheral])

        delta_text = "\n".join(delta_lines) if delta_lines else "- No changes."

        if budget_chars and len(delta_text) > budget_chars:
            # Trim order: drop peripheral -> aggregate secondary -> keep critical verbatim.
            delta_lines = [f"- {d['message']}" for d in critical]
            if secondary:
                delta_lines.append(f"- Secondary changes: {len(secondary)} (aggregated)")
            delta_text = "\n".join(delta_lines)
            if len(delta_text) > budget_chars:
                delta_text = "\n".join([f"- {d['message']}" for d in critical]) or "- No changes."

        risks = self.state["open_risks"]
        risk_texts = []
        for r in risks:
            if isinstance(r, dict):
                if r.get("status") == "open":
                    risk_texts.append(f"- {r['text']}")
            elif isinstance(r, str):
                risk_texts.append(f"- {r}")
        risks_text = "\n".join(risk_texts) if risk_texts else "- None."

        goal_text = (
            str(goal_override).strip()
            if goal_override is not None
            else str(self.state.get("goal", "") or "").strip()
        )
        goal_source = (
            str(goal_source_override).strip()
            if goal_source_override is not None
            else str(self.state.get("goal_source", "unset") or "unset")
        )

        return {
            "Goal": goal_text or "Not set.",
            "Goal Source": goal_source or "unset",
            "Delta": delta_text,
            "Open Risks": risks_text,
        }

    def format_recovery_token(
        self,
        budget_chars: int | None = None,
        goal_override: str | None = None,
        goal_source_override: str | None = None,
    ) -> dict[str, Any]:
        critical, secondary, peripheral = self._partition_deltas()
        selected: list[dict[str, Any]] = [*critical, *secondary, *peripheral]
        aggregated_secondary = 0
        aggregated_peripheral = 0

        if budget_chars:
            encoded = json.dumps(selected, ensure_ascii=True, separators=(",", ":"))
            if len(encoded) > budget_chars:
                selected = [*critical]
                aggregated_secondary = len(secondary)
                aggregated_peripheral = len(peripheral)

        token_deltas: list[dict[str, Any]] = []
        for item in selected:
            row = {
                "id": str(item.get("id", "")),
                "level": str(item.get("level", LEVEL_CORE_CRITICAL)),
                "message": str(item.get("message", "")),
                "verification_state": str(item.get("verification_state", "verified")),
                "provisional": bool(item.get("provisional", False)),
            }
            if "reconcile_outcome" in item:
                row["reconcile_outcome"] = str(item.get("reconcile_outcome", ""))
            token_deltas.append(row)
        if aggregated_secondary:
            token_deltas.append(
                {
                    "id": "agg-secondary",
                    "level": LEVEL_CORE_SECONDARY,
                    "message": f"aggregated:{aggregated_secondary}",
                    "verification_state": "verified",
                    "provisional": False,
                    "aggregated": True,
                }
            )
        if aggregated_peripheral:
            token_deltas.append(
                {
                    "id": "agg-peripheral",
                    "level": LEVEL_PERIPHERAL,
                    "message": f"aggregated:{aggregated_peripheral}",
                    "verification_state": "verified",
                    "provisional": False,
                    "aggregated": True,
                }
            )

        goal_text = (
            str(goal_override).strip()
            if goal_override is not None
            else str(self.state.get("goal", "") or "").strip()
        )
        goal_source = (
            str(goal_source_override).strip()
            if goal_source_override is not None
            else str(self.state.get("goal_source", "unset") or "unset")
        )

        token: dict[str, Any] = {
            "schema_version": "flywheel.v1",
            "goal": goal_text,
            "goal_source": goal_source,
            "verification_state": self.state.get("verification_state", "verified"),
            "consistency_window_ms": int(self.state.get("consistency_window_ms", 0) or 0),
            "open_risks": [
                (r["text"] if isinstance(r, dict) else str(r))
                for r in self.state.get("open_risks", [])
                if (isinstance(r, dict) and r.get("status") == "open") or isinstance(r, str)
            ],
            "deltas": token_deltas,
        }
        if budget_chars:
            encoded = json.dumps(token, ensure_ascii=True, separators=(",", ":"))
            if len(encoded) > budget_chars:
                # Hard cap: truncate critical deltas to fit budget
                kept: list[dict[str, Any]] = []
                overhead = len(encoded) - sum(
                    len(json.dumps(d, ensure_ascii=True, separators=(",", ":")))
                    for d in token_deltas
                )
                budget_left = budget_chars - overhead
                for d in token_deltas:
                    s = json.dumps(d, ensure_ascii=True, separators=(",", ":"))
                    if budget_left - len(s) - 2 >= 0:
                        kept.append(d)
                        budget_left -= len(s) + 2  # +2 for separator
                    else:
                        break
                token["deltas"] = kept
        return token

    def format_additional_context(
        self,
        budget_chars: int | None = None,
        goal_override: str | None = None,
        goal_source_override: str | None = None,
    ) -> str:
        context = self.format_context(
            budget_chars=budget_chars,
            goal_override=goal_override,
            goal_source_override=goal_source_override,
        )
        token = self.format_recovery_token(
            budget_chars=budget_chars,
            goal_override=goal_override,
            goal_source_override=goal_source_override,
        )
        token_json = json.dumps(token, ensure_ascii=True, separators=(",", ":"))

        # When the session is idle (no goal, no deltas, no risks), skip the
        # human-readable section — it would only add token noise with empty values.
        goal_text = context["Goal"]
        delta_text = context["Delta"]
        risks_text = context["Open Risks"]
        is_idle = (
            goal_text in ("Not set.", "")
            and delta_text in ("- No changes.", "")
            and risks_text in ("- None.", "")
        )
        if is_idle:
            return f"FLYWHEEL_TOKEN\n{token_json}"

        return (
            f"Goal\n{goal_text}\n\n"
            f"Delta\n{delta_text}\n\n"
            f"Open Risks\n{risks_text}\n\n"
            f"FLYWHEEL_TOKEN\n{token_json}"
        )

    def _partition_deltas(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        deltas = list(self.state["deltas"])
        critical = [d for d in deltas if d["level"] == LEVEL_CORE_CRITICAL]
        secondary = [d for d in deltas if d["level"] == LEVEL_CORE_SECONDARY]
        peripheral = [d for d in deltas if d["level"] == LEVEL_PERIPHERAL]
        return critical, secondary, peripheral

    def to_dict(self) -> dict[str, Any]:
        return self.state


def _normalize_state(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {
            "goal": "",
            "goal_source": "unset",
            "open_risks": [],
            "deltas": [],
            "verification_state": "verified",
            "consistency_window_ms": 0,
        }
    goal = str(raw.get("goal", ""))
    goal_source = str(raw.get("goal_source", "unset"))
    if len(goal) > MAX_GOAL_CHARS:
        goal = goal[:MAX_GOAL_CHARS]
    verification_state = (
        "degraded" if str(raw.get("verification_state", "verified")) == "degraded" else "verified"
    )
    try:
        consistency_window_ms = max(0, int(raw.get("consistency_window_ms", 0)))
    except Exception:
        consistency_window_ms = 0

    open_risks_raw = raw.get("open_risks", [])
    open_risks: list[dict[str, Any]] = []
    if isinstance(open_risks_raw, list):
        for item in open_risks_raw:
            if isinstance(item, str):
                open_risks.append(
                    {
                        "risk_id": "r-" + hashlib.sha256(item.encode()).hexdigest()[:12],
                        "text": item,
                        "status": "open",
                        "created_at_ms": 0,
                        "snoozed_until_ms": None,
                        "handled_reason": None,
                        "source_delta_id": None,
                        "intent_signature": None,
                    }
                )
            elif isinstance(item, dict):
                open_risks.append(
                    {
                        "risk_id": str(item.get("risk_id", "")),
                        "text": str(item.get("text", "")),
                        "status": str(item.get("status", "open")),
                        "created_at_ms": int(item.get("created_at_ms", 0) or 0),
                        "snoozed_until_ms": item.get("snoozed_until_ms"),
                        "handled_reason": item.get("handled_reason"),
                        "source_delta_id": item.get("source_delta_id"),
                        "intent_signature": item.get("intent_signature"),
                    }
                )

    deltas_raw = raw.get("deltas", [])
    deltas: list[dict[str, Any]] = []
    if isinstance(deltas_raw, list):
        for item in deltas_raw:
            if not isinstance(item, dict):
                continue
            delta_id = str(item.get("id", ""))
            message = str(item.get("message", ""))
            level = str(item.get("level", LEVEL_CORE_CRITICAL))
            if level not in {LEVEL_CORE_CRITICAL, LEVEL_CORE_SECONDARY, LEVEL_PERIPHERAL}:
                level = LEVEL_CORE_CRITICAL
            delta_state = (
                "degraded"
                if str(item.get("verification_state", "verified")) == "degraded"
                else "verified"
            )
            normalized = {
                "id": delta_id or f"d-{int(time.time() * 1000)}-{len(deltas)}",
                "message": message,
                "level": level,
                "verification_state": delta_state,
                "provisional": bool(item.get("provisional", False)),
            }
            if "reconcile_outcome" in item:
                normalized["reconcile_outcome"] = str(item["reconcile_outcome"])
            normalized["intent_signature"] = item.get("intent_signature") or None
            normalized["tool_name"] = item.get("tool_name") or None
            try:
                normalized["ts_ms"] = int(item.get("ts_ms", 0))
            except Exception:
                normalized["ts_ms"] = 0
            deltas.append(normalized)

    return {
        "goal": goal,
        "goal_source": goal_source,
        "open_risks": open_risks,
        "deltas": deltas,
        "verification_state": verification_state,
        "consistency_window_ms": consistency_window_ms,
    }


def load_tracker(path: Path) -> StateTracker:
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return StateTracker()
        return StateTracker(_normalize_state(raw))
    return StateTracker()


def save_tracker(path: Path, tracker: StateTracker) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix="state-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            json.dump(_normalize_state(tracker.to_dict()), tmp, ensure_ascii=True, indent=2)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
