from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from scripts.policy_config import atomic_write_json


LEVEL_CORE_CRITICAL = "core_critical"
LEVEL_CORE_SECONDARY = "core_secondary"
LEVEL_PERIPHERAL = "peripheral"
MAX_DELTAS = 500  # hard cap per session; pre_compact resets on compaction


class StateTracker:
    def __init__(self, state: dict[str, Any] | None = None) -> None:
        if state is None:
            self.state = {
                "open_risks": [],
                "deltas": [],
                "verification_state": "verified",
                "consistency_window_ms": 0,
            }
        else:
            self.state = _normalize_state(state)

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
        args_summary: str | None = None,
    ) -> str:
        message = str(message).strip() or "(no message)"
        if ts_ms is None:
            ts_ms = int(time.time() * 1000)
        delta_id = f"d-{int(time.time() * 1000)}-{len(self.state['deltas'])}"
        entry: dict[str, Any] = {
            "id": delta_id,
            "message": message,
            "level": level,
            "verification_state": verification_state,
            "provisional": provisional,
            "intent_signature": intent_signature,
            "tool_name": tool_name,
            "ts_ms": ts_ms,
        }
        if args_summary:
            entry["args_summary"] = str(args_summary)[:200]
        self.state["deltas"].append(entry)
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
        # Dedup key includes intent_signature: same message from different intents
        # creates separate risk entries (they represent different failure sites).
        # No-intent risks (intent_signature=None or "") dedup on text alone.
        dedup_key = f"{text}\x00{intent_signature or ''}"
        for existing in self.state["open_risks"]:
            if isinstance(existing, dict):
                existing_key = f"{existing.get('text', '')}\x00{existing.get('intent_signature', '') or ''}"
                if existing_key == dedup_key:
                    return
            elif isinstance(existing, str) and existing == text and not (intent_signature or ""):
                return
        risk_id = "r-" + hashlib.sha256(dedup_key.encode()).hexdigest()[:12]
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
        open_risks = [
            r for r in risks
            if (isinstance(r, dict) and r.get("status") == "open") or isinstance(r, str)
        ]
        # Sort by recency (created_at_ms desc) so most recent risks survive trimming
        open_risks.sort(
            key=lambda r: int(r.get("created_at_ms", 0)) if isinstance(r, dict) else 0,
            reverse=True,
        )

        def _risk_line(r) -> str:
            return f"- {r['text']}" if isinstance(r, dict) else f"- {r}"

        risk_lines = [_risk_line(r) for r in open_risks]
        risks_text = "\n".join(risk_lines) if risk_lines else "- None."

        if budget_chars and len(risks_text) > budget_chars // 3:
            # Give risks at most 1/3 of the budget; keep most-recent, truncate rest
            allowed = budget_chars // 3
            kept, total = [], 0
            for line in risk_lines:
                if total + len(line) + 1 > allowed:
                    remaining = len(risk_lines) - len(kept)
                    kept.append(f"- … {remaining} more risks (read state://deltas for full list)")
                    break
                kept.append(line)
                total += len(line) + 1
            risks_text = "\n".join(kept) if kept else "- None."

        return {
            "Delta": delta_text,
            "Open Risks": risks_text,
        }

    def format_recovery_token(
        self,
        budget_chars: int | None = None,
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

        token: dict[str, Any] = {
            "schema_version": "flywheel.v1",
            "verification_state": self.state.get("verification_state", "verified"),
            "consistency_window_ms": int(self.state.get("consistency_window_ms", 0) or 0),
            "open_risks": [
                (r["text"] if isinstance(r, dict) else str(r))
                for r in self.state.get("open_risks", [])
                if (isinstance(r, dict) and r.get("status") == "open") or isinstance(r, str)
            ],
            "deltas": token_deltas,
            "active_span_id": self.state.get("active_span_id") or None,
            "active_span_intent": self.state.get("active_span_intent") or None,
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
    ) -> str:
        context = self.format_context(budget_chars=budget_chars)
        token = self.format_recovery_token(budget_chars=budget_chars)
        token_json = json.dumps(token, ensure_ascii=True, separators=(",", ":"))

        delta_text = context["Delta"]
        risks_text = context["Open Risks"]
        open_risks = [
            r for r in self.state.get("open_risks", [])
            if (isinstance(r, dict) and r.get("status") == "open") or isinstance(r, str)
        ]
        is_idle = not self.state.get("deltas") and not open_risks
        if is_idle:
            return f"FLYWHEEL_TOKEN\n{token_json}"

        return (
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
            "open_risks": [],
            "deltas": [],
            "verification_state": "verified",
            "consistency_window_ms": 0,
        }
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
            if item.get("args_summary"):
                normalized["args_summary"] = str(item["args_summary"])[:200]
            deltas.append(normalized)

    notes_injected_raw = raw.get("notes_injected", [])
    notes_injected: list[str] = []
    if isinstance(notes_injected_raw, list):
        seen: set[str] = set()
        for item in notes_injected_raw:
            name = str(item).strip().lower()
            if not name or name in seen:
                continue
            seen.add(name)
            notes_injected.append(name)

    out: dict[str, Any] = {
        "open_risks": open_risks,
        "deltas": deltas,
        "verification_state": verification_state,
        "consistency_window_ms": consistency_window_ms,
        "notes_injected": notes_injected,
    }
    # Preserve flywheel span/session fields (written by SpanTracker / hooks as raw JSON keys).
    for _k in ("active_span_id", "active_span_intent", "turn_count"):
        if _k in raw:
            out[_k] = raw[_k]
    return out


def load_tracker(path: Path) -> StateTracker:
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return StateTracker()
        return StateTracker(_normalize_state(raw))
    return StateTracker()


def save_tracker(path: Path, tracker: StateTracker) -> None:
    atomic_write_json(path, _normalize_state(tracker.to_dict()))
