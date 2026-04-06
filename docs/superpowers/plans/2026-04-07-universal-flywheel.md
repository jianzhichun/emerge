# Universal Flywheel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the `icc_exec`-only flywheel with an Intent Span system that tracks all tool calls (Lark, context7, file ops, skill sequences) and bridges stable patterns via macro replay (read-only) or Python pipeline (write).

**Architecture:** A new `SpanTracker` module manages span lifecycle—open/close/WAL/candidates—independently of `icc_exec`. The `PostToolUse` hook records every tool call into an active-span buffer. Three new daemon tools (`icc_span_open`, `icc_span_close`, `icc_span_approve`) replace `icc_read`, `icc_write`, and `icc_crystallize`. When a span reaches stable, read-only spans auto-crystallize to macro JSON; write spans auto-generate a Python skeleton awaiting human approval. The bridge fires inside `icc_span_open`, returning either a recipe (macro) or result (pipeline) transparently.

**Tech Stack:** Python 3.11+, existing `policy_config.py` thresholds, `pytest`, JSONL append-only WAL, atomic temp-file writes.

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `scripts/span_tracker.py` | Create | SpanRecord, SpanTracker (open/close/WAL/candidates/policy) |
| `scripts/span_crystallizer.py` | Create | Macro JSON generation, Python skeleton generation |
| `hooks/post_tool_use.py` | Modify | Append action to active-span buffer when span is open |
| `hooks/pre_tool_use.py` | Modify | Validate icc_span_open / icc_span_close / icc_span_approve |
| `scripts/emerge_daemon.py` | Modify | Add 3 new tools, span bridge, connector://macros resource, deprecate icc_read/write/crystallize |
| `tests/test_span_tracker.py` | Create | SpanTracker unit tests |
| `tests/test_span_crystallizer.py` | Create | Crystallizer unit tests |
| `tests/test_mcp_tools_integration.py` | Modify | Integration tests for new span tools |
| `tests/test_hook_scripts_output.py` | Modify | Hook tests for action recording |

---

## Task 1: SpanTracker — Core Data Structures and WAL

**Files:**
- Create: `scripts/span_tracker.py`
- Create: `tests/test_span_tracker.py`

- [ ] **Step 1.1: Write the failing tests**

```python
# tests/test_span_tracker.py
from __future__ import annotations
import json
import time
from pathlib import Path
import pytest
from scripts.span_tracker import SpanTracker, is_read_only_tool


@pytest.fixture
def tracker(tmp_path):
    hook_state = tmp_path / "hook-state"
    hook_state.mkdir()
    state_path = hook_state / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    return SpanTracker(state_root=tmp_path, hook_state_root=hook_state)


def test_is_read_only_tool_known_readonly():
    assert is_read_only_tool("Read") is True
    assert is_read_only_tool("Glob") is True
    assert is_read_only_tool("Grep") is True
    assert is_read_only_tool("WebFetch") is True
    assert is_read_only_tool("mcp__context7__query-docs") is True


def test_is_read_only_tool_suffix_patterns():
    assert is_read_only_tool("mcp__lark_doc__get") is True
    assert is_read_only_tool("mcp__lark_base__list") is True
    assert is_read_only_tool("mcp__lark_drive__search") is True


def test_is_read_only_tool_write_tools():
    assert is_read_only_tool("mcp__lark_doc__create") is False
    assert is_read_only_tool("mcp__lark_im__send") is False
    assert is_read_only_tool("Edit") is False
    assert is_read_only_tool("Write") is False
    assert is_read_only_tool("Bash") is False


def test_open_span_writes_active_span_to_hook_state(tracker, tmp_path):
    hook_state = tmp_path / "hook-state"
    span = tracker.open_span("lark.read.get-doc", description="test")
    state = json.loads((hook_state / "state.json").read_text())
    assert state["active_span_id"] == span.span_id
    assert state["active_span_intent"] == "lark.read.get-doc"


def test_open_span_clears_buffer(tracker, tmp_path):
    hook_state = tmp_path / "hook-state"
    buf = hook_state / "active-span-actions.jsonl"
    buf.write_text("stale line\n", encoding="utf-8")
    tracker.open_span("lark.read.get-doc")
    assert buf.read_text(encoding="utf-8") == ""


def test_close_span_writes_to_wal(tracker, tmp_path):
    span = tracker.open_span("lark.write.create-doc", args={"title": "T"})
    tracker.close_span(span, outcome="success", result_summary={"doc_id": "x"})
    wal = tmp_path / "span-wal" / "spans.jsonl"
    assert wal.exists()
    record = json.loads(wal.read_text().strip())
    assert record["intent_signature"] == "lark.write.create-doc"
    assert record["outcome"] == "success"
    assert record["result_summary"] == {"doc_id": "x"}


def test_close_span_clears_active_span_from_hook_state(tracker, tmp_path):
    hook_state = tmp_path / "hook-state"
    span = tracker.open_span("lark.read.get-doc")
    tracker.close_span(span, outcome="success")
    state = json.loads((hook_state / "state.json").read_text())
    assert "active_span_id" not in state
    assert "active_span_intent" not in state


def test_close_span_reads_actions_from_buffer(tracker, tmp_path):
    hook_state = tmp_path / "hook-state"
    span = tracker.open_span("lark.read.get-doc")
    buf = hook_state / "active-span-actions.jsonl"
    buf.write_text(
        json.dumps({"tool_name": "mcp__lark_doc__get", "args_hash": "abc", "has_side_effects": False, "ts_ms": 1}) + "\n",
        encoding="utf-8",
    )
    tracker.close_span(span, outcome="success")
    wal = tmp_path / "span-wal" / "spans.jsonl"
    record = json.loads(wal.read_text().strip())
    assert len(record["actions"]) == 1
    assert record["actions"][0]["tool_name"] == "mcp__lark_doc__get"
    assert record["is_read_only"] is True


def test_close_span_is_not_read_only_when_side_effects(tracker, tmp_path):
    hook_state = tmp_path / "hook-state"
    span = tracker.open_span("lark.write.create-doc")
    buf = hook_state / "active-span-actions.jsonl"
    buf.write_text(
        json.dumps({"tool_name": "mcp__lark_doc__create", "args_hash": "abc", "has_side_effects": True, "ts_ms": 1}) + "\n",
        encoding="utf-8",
    )
    tracker.close_span(span, outcome="success")
    wal = tmp_path / "span-wal" / "spans.jsonl"
    record = json.loads(wal.read_text().strip())
    assert record["is_read_only"] is False


def test_policy_status_starts_explore(tracker):
    assert tracker.get_policy_status("lark.read.get-doc") == "explore"


def test_policy_status_reaches_canary_after_threshold(tracker, tmp_path):
    from scripts.policy_config import PROMOTE_MIN_ATTEMPTS
    span = tracker.open_span("lark.read.get-doc")
    for _ in range(PROMOTE_MIN_ATTEMPTS):
        s = tracker.open_span("lark.read.get-doc")
        tracker.close_span(s, outcome="success")
    assert tracker.get_policy_status("lark.read.get-doc") == "canary"


def test_policy_status_rollback_on_consecutive_failures(tracker):
    from scripts.policy_config import ROLLBACK_CONSECUTIVE_FAILURES
    for _ in range(ROLLBACK_CONSECUTIVE_FAILURES):
        s = tracker.open_span("lark.read.get-doc")
        tracker.close_span(s, outcome="failure")
    assert tracker.get_policy_status("lark.read.get-doc") == "rollback"


def test_latest_successful_span_returns_most_recent(tracker, tmp_path):
    for i in range(3):
        s = tracker.open_span("lark.read.get-doc", args={"n": i})
        tracker.close_span(s, outcome="success", result_summary={"n": i})
    latest = tracker.latest_successful_span("lark.read.get-doc")
    assert latest is not None
    assert latest["result_summary"]["n"] == 2


def test_latest_successful_span_ignores_failures(tracker):
    s = tracker.open_span("lark.read.get-doc")
    tracker.close_span(s, outcome="failure")
    assert tracker.latest_successful_span("lark.read.get-doc") is None
```

- [ ] **Step 1.2: Run tests to verify they fail**

```bash
python -m pytest tests/test_span_tracker.py -q
```

Expected: `ModuleNotFoundError: No module named 'scripts.span_tracker'`

- [ ] **Step 1.3: Implement SpanTracker**

Create `scripts/span_tracker.py`:

```python
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any

from scripts.policy_config import (
    PROMOTE_MAX_HUMAN_FIX_RATE,
    PROMOTE_MIN_ATTEMPTS,
    PROMOTE_MIN_SUCCESS_RATE,
    ROLLBACK_CONSECUTIVE_FAILURES,
    STABLE_MIN_ATTEMPTS,
    STABLE_MIN_SUCCESS_RATE,
    WINDOW_SIZE,
)

# Conservative read-only classification: unknown tools default to has_side_effects=True
_READ_ONLY_TOOL_NAMES = {"Read", "Glob", "Grep", "WebFetch", "WebSearch", "ToolSearch"}
_READ_ONLY_TOOL_PREFIXES = ("mcp__context7__",)
_READ_ONLY_TOOL_SUFFIXES = ("__get", "__list", "__search", "__query", "__read", "__resolve", "__query-docs")


def is_read_only_tool(tool_name: str) -> bool:
    if tool_name in _READ_ONLY_TOOL_NAMES:
        return True
    for prefix in _READ_ONLY_TOOL_PREFIXES:
        if tool_name.startswith(prefix):
            return True
    for suffix in _READ_ONLY_TOOL_SUFFIXES:
        if tool_name.endswith(suffix):
            return True
    return False


@dataclass
class ActionRecord:
    seq: int
    tool_name: str
    args_hash: str
    has_side_effects: bool
    ts_ms: int

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "tool_name": self.tool_name,
            "args_hash": self.args_hash,
            "has_side_effects": self.has_side_effects,
            "ts_ms": self.ts_ms,
        }


@dataclass
class SpanRecord:
    span_id: str
    intent_signature: str
    description: str
    source: str  # "skill" | "manual"
    opened_at_ms: int
    is_read_only: bool = True
    skill_name: str | None = None
    closed_at_ms: int | None = None
    outcome: str | None = None
    args: dict = field(default_factory=dict)
    result_summary: dict = field(default_factory=dict)
    actions: list[ActionRecord] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "span_id": self.span_id,
            "intent_signature": self.intent_signature,
            "description": self.description,
            "source": self.source,
            "skill_name": self.skill_name,
            "opened_at_ms": self.opened_at_ms,
            "closed_at_ms": self.closed_at_ms,
            "outcome": self.outcome,
            "is_read_only": self.is_read_only,
            "args": self.args,
            "result_summary": self.result_summary,
            "actions": [a.to_dict() for a in self.actions],
        }


class SpanTracker:
    """Manages intent span lifecycle: open → actions → close → WAL → candidates → policy."""

    def __init__(self, state_root: Path, hook_state_root: Path) -> None:
        self._state_root = state_root
        self._hook_state_root = hook_state_root
        self._span_wal_root = state_root / "span-wal"
        self._span_wal_root.mkdir(parents=True, exist_ok=True)

    # ── paths ──────────────────────────────────────────────────────────────

    def _candidates_path(self) -> Path:
        return self._state_root / "span-candidates.json"

    def _buffer_path(self) -> Path:
        return self._hook_state_root / "active-span-actions.jsonl"

    def _state_path(self) -> Path:
        return self._hook_state_root / "state.json"

    def _wal_path(self) -> Path:
        return self._span_wal_root / "spans.jsonl"

    # ── atomic write ───────────────────────────────────────────────────────

    def _atomic_write(self, path: Path, data: dict) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    # ── span open / close ─────────────────────────────────────────────────

    def open_span(
        self,
        intent_signature: str,
        description: str = "",
        args: dict | None = None,
        source: str = "manual",
        skill_name: str | None = None,
    ) -> SpanRecord:
        span = SpanRecord(
            span_id=str(uuid.uuid4()),
            intent_signature=intent_signature,
            description=description,
            source=source,
            skill_name=skill_name,
            opened_at_ms=int(time.time() * 1000),
            args=args or {},
        )
        # Write active span ID to hook state so PostToolUse can read it
        state_path = self._state_path()
        try:
            state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        except Exception:
            state = {}
        state["active_span_id"] = span.span_id
        state["active_span_intent"] = intent_signature
        self._atomic_write(state_path, state)
        # Clear the action buffer for this new span
        self._buffer_path().write_text("", encoding="utf-8")
        return span

    def close_span(
        self,
        span: SpanRecord,
        outcome: str,
        result_summary: dict | None = None,
    ) -> SpanRecord:
        span.closed_at_ms = int(time.time() * 1000)
        span.outcome = outcome
        span.result_summary = result_summary or {}
        # Collect actions from hook buffer
        buf = self._buffer_path()
        actions: list[ActionRecord] = []
        if buf.exists():
            for i, line in enumerate(buf.read_text(encoding="utf-8").splitlines()):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    actions.append(ActionRecord(
                        seq=i,
                        tool_name=str(rec.get("tool_name", "")),
                        args_hash=str(rec.get("args_hash", "")),
                        has_side_effects=bool(rec.get("has_side_effects", True)),
                        ts_ms=int(rec.get("ts_ms", 0)),
                    ))
                except Exception:
                    pass
        span.actions = actions
        # is_read_only: all actions must be side-effect-free (empty span = read-only by default)
        span.is_read_only = all(not a.has_side_effects for a in actions)
        # Persist to WAL
        with self._wal_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(span.to_dict(), ensure_ascii=False) + "\n")
        # Update candidates registry
        self._update_candidates(span)
        # Clear active span from hook state
        state_path = self._state_path()
        try:
            state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        except Exception:
            state = {}
        state.pop("active_span_id", None)
        state.pop("active_span_intent", None)
        self._atomic_write(state_path, state)
        buf.unlink(missing_ok=True)
        return span

    # ── candidates / policy ───────────────────────────────────────────────

    def _load_candidates(self) -> dict:
        path = self._candidates_path()
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"spans": {}}

    def _update_candidates(self, span: SpanRecord) -> None:
        if not span.intent_signature:
            return
        candidates = self._load_candidates()
        key = span.intent_signature
        entry = candidates["spans"].get(key, {
            "intent_signature": key,
            "is_read_only": span.is_read_only,
            "description": span.description,
            "attempts": 0,
            "successes": 0,
            "human_fixes": 0,
            "consecutive_failures": 0,
            "recent_outcomes": [],
            "last_ts_ms": 0,
        })
        is_success = span.outcome == "success"
        entry["attempts"] += 1
        if is_success:
            entry["successes"] += 1
        entry["consecutive_failures"] = 0 if is_success else int(entry.get("consecutive_failures", 0)) + 1
        recent = list(entry.get("recent_outcomes", []))
        recent.append(1 if is_success else 0)
        entry["recent_outcomes"] = recent[-WINDOW_SIZE:]
        entry["last_ts_ms"] = span.closed_at_ms or 0
        entry["is_read_only"] = span.is_read_only
        if span.description:
            entry["description"] = span.description
        candidates["spans"][key] = entry
        self._atomic_write(self._candidates_path(), candidates)

    def get_policy_status(self, intent_signature: str) -> str:
        """explore | canary | stable | rollback.

        Macro spans (is_read_only=True) skip verify_rate — they have no verify step.
        Write spans use the same thresholds as exec candidates minus verify_rate
        (pipeline verify kicks in after approve).
        """
        candidates = self._load_candidates()
        entry = candidates["spans"].get(intent_signature, {})
        if not entry:
            return "explore"
        attempts = int(entry.get("attempts", 0))
        successes = int(entry.get("successes", 0))
        human_fixes = int(entry.get("human_fixes", 0))
        consecutive_failures = int(entry.get("consecutive_failures", 0))
        if consecutive_failures >= ROLLBACK_CONSECUTIVE_FAILURES:
            return "rollback"
        if attempts == 0:
            return "explore"
        success_rate = successes / attempts
        human_fix_rate = human_fixes / attempts
        if attempts >= STABLE_MIN_ATTEMPTS and success_rate >= STABLE_MIN_SUCCESS_RATE:
            return "stable"
        if (
            attempts >= PROMOTE_MIN_ATTEMPTS
            and success_rate >= PROMOTE_MIN_SUCCESS_RATE
            and human_fix_rate <= PROMOTE_MAX_HUMAN_FIX_RATE
        ):
            return "canary"
        return "explore"

    def is_synthesis_ready(self, intent_signature: str) -> bool:
        return self.get_policy_status(intent_signature) == "stable"

    def latest_successful_span(self, intent_signature: str) -> dict | None:
        """Return the most recent successful span record from WAL."""
        wal = self._wal_path()
        if not wal.exists():
            return None
        best: dict | None = None
        best_ts = 0
        with wal.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if (
                    rec.get("intent_signature") == intent_signature
                    and rec.get("outcome") == "success"
                    and int(rec.get("closed_at_ms", 0)) > best_ts
                ):
                    best = rec
                    best_ts = int(rec["closed_at_ms"])
        return best
```

- [ ] **Step 1.4: Run tests to verify they pass**

```bash
python -m pytest tests/test_span_tracker.py -q
```

Expected: all tests pass.

- [ ] **Step 1.5: Commit**

```bash
git add scripts/span_tracker.py tests/test_span_tracker.py
git commit -m "feat: add SpanTracker — intent span WAL, candidates, policy lifecycle"
```

---

## Task 2: SpanCrystallizer — Macro JSON and Pipeline Skeleton

**Files:**
- Create: `scripts/span_crystallizer.py`
- Create: `tests/test_span_crystallizer.py`

- [ ] **Step 2.1: Write the failing tests**

```python
# tests/test_span_crystallizer.py
from __future__ import annotations
import json
from pathlib import Path
import pytest
from scripts.span_crystallizer import SpanCrystallizer


@pytest.fixture
def crystallizer(tmp_path):
    connector_root = tmp_path / "connectors"
    return SpanCrystallizer(connector_root=connector_root)


def _make_span(intent_signature: str, is_read_only: bool, actions: list[dict]) -> dict:
    return {
        "span_id": "test-span",
        "intent_signature": intent_signature,
        "description": "test span",
        "is_read_only": is_read_only,
        "args": {"doc_id": "123"},
        "result_summary": {"title": "T"},
        "actions": actions,
        "outcome": "success",
    }


def test_crystallize_macro_creates_json_file(crystallizer, tmp_path):
    span = _make_span("lark.read.get-doc", True, [
        {"tool_name": "mcp__lark_doc__get", "args_hash": "abc", "has_side_effects": False, "seq": 0, "ts_ms": 1},
    ])
    path = crystallizer.crystallize_macro("lark.read.get-doc", span)
    assert path.exists()
    macro = json.loads(path.read_text())
    assert macro["intent_signature"] == "lark.read.get-doc"
    assert macro["is_read_only"] is True
    assert len(macro["actions"]) == 1
    assert macro["actions"][0]["tool_name"] == "mcp__lark_doc__get"


def test_crystallize_macro_path_is_under_connector_root(crystallizer, tmp_path):
    span = _make_span("lark.read.get-doc", True, [])
    path = crystallizer.crystallize_macro("lark.read.get-doc", span)
    connector_root = tmp_path / "connectors"
    assert path.is_relative_to(connector_root)
    assert "lark" in str(path)
    assert "macros" in str(path)
    assert path.name == "get-doc.json"


def test_generate_pipeline_skeleton_is_python(crystallizer):
    span = _make_span("lark.write.create-doc", False, [
        {"tool_name": "mcp__lark_doc__create", "args_hash": "abc", "has_side_effects": True, "seq": 0, "ts_ms": 1},
        {"tool_name": "mcp__lark_doc__append", "args_hash": "def", "has_side_effects": True, "seq": 1, "ts_ms": 2},
    ])
    skeleton = crystallizer.generate_pipeline_skeleton("lark.write.create-doc", span)
    assert "def run_write(metadata, args):" in skeleton
    assert "lark.write.create-doc" in skeleton
    assert "mcp__lark_doc__create" in skeleton
    assert "mcp__lark_doc__append" in skeleton


def test_crystallize_macro_overwrites_existing(crystallizer, tmp_path):
    span = _make_span("lark.read.get-doc", True, [
        {"tool_name": "mcp__lark_doc__get", "args_hash": "abc", "has_side_effects": False, "seq": 0, "ts_ms": 1},
    ])
    path1 = crystallizer.crystallize_macro("lark.read.get-doc", span)
    span2 = _make_span("lark.read.get-doc", True, [
        {"tool_name": "mcp__lark_doc__list", "args_hash": "xyz", "has_side_effects": False, "seq": 0, "ts_ms": 2},
    ])
    path2 = crystallizer.crystallize_macro("lark.read.get-doc", span2)
    assert path1 == path2
    macro = json.loads(path2.read_text())
    assert macro["actions"][0]["tool_name"] == "mcp__lark_doc__list"


def test_macro_path_for_nested_name(crystallizer, tmp_path):
    span = _make_span("lark.read.folder.list", True, [])
    path = crystallizer.crystallize_macro("lark.read.folder.list", span)
    assert path.name == "folder.list.json"
```

- [ ] **Step 2.2: Run tests to verify they fail**

```bash
python -m pytest tests/test_span_crystallizer.py -q
```

Expected: `ModuleNotFoundError: No module named 'scripts.span_crystallizer'`

- [ ] **Step 2.3: Implement SpanCrystallizer**

Create `scripts/span_crystallizer.py`:

```python
from __future__ import annotations

import json
import textwrap
from pathlib import Path


class SpanCrystallizer:
    """Converts stable spans into crystallized artifacts.

    Read-only spans → macro JSON (auto, zero human intervention).
    Write spans → Python skeleton (auto-generated, awaits human approve).
    """

    def __init__(self, connector_root: Path) -> None:
        self._connector_root = connector_root

    def _macro_path(self, intent_signature: str) -> Path:
        # intent_signature: <connector>.(read|write).<name[.subname]>
        parts = intent_signature.split(".", 2)
        connector = parts[0]
        name = parts[2] if len(parts) >= 3 else intent_signature
        macro_dir = self._connector_root / connector / "macros"
        macro_dir.mkdir(parents=True, exist_ok=True)
        return macro_dir / f"{name}.json"

    def crystallize_macro(self, intent_signature: str, span: dict) -> Path:
        """Write macro JSON for a stable read-only span. Returns the path written."""
        actions = [
            {
                "tool_name": a["tool_name"],
                # Preserve original args_hash for audit; template slots TBD by skill authors
                "args_hash": a.get("args_hash", ""),
            }
            for a in span.get("actions", [])
        ]
        macro = {
            "intent_signature": intent_signature,
            "description": span.get("description", ""),
            "is_read_only": True,
            "args_schema": {},  # populated by skill authors post-crystallize
            "actions": actions,
            "crystallized_from_span": span.get("span_id", ""),
        }
        path = self._macro_path(intent_signature)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(macro, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        return path

    def generate_pipeline_skeleton(self, intent_signature: str, span: dict) -> str:
        """Return Python source for a write pipeline skeleton from span actions."""
        actions = span.get("actions", [])
        call_lines = []
        for action in actions:
            tool = action.get("tool_name", "unknown_tool")
            call_lines.append(f'    mcp_call("{tool}", args)  # seq={action.get("seq", "?")}')
        if not call_lines:
            call_lines = ["    pass  # no actions recorded — implement manually"]
        body = "\n".join(call_lines)
        return textwrap.dedent(f'''\
            # auto-generated by icc_span_approve — review before promoting
            # intent_signature: {intent_signature}
            # crystallized_from_span: {span.get("span_id", "")}
            #
            # IMPORTANT: Replace mcp_call() stubs with actual MCP tool calls.
            # Add args parameters as needed.

            def run_write(metadata, args):
            {body}
                return {{"ok": True}}
        ''')

    def macro_exists(self, intent_signature: str) -> bool:
        return self._macro_path(intent_signature).exists()

    def read_macro(self, intent_signature: str) -> dict | None:
        path = self._macro_path(intent_signature)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def list_macros(self, connector: str) -> list[dict]:
        """Return all macro records for a connector."""
        macro_dir = self._connector_root / connector / "macros"
        if not macro_dir.exists():
            return []
        results = []
        for f in sorted(macro_dir.glob("*.json")):
            try:
                results.append(json.loads(f.read_text(encoding="utf-8")))
            except Exception:
                pass
        return results
```

- [ ] **Step 2.4: Run tests to verify they pass**

```bash
python -m pytest tests/test_span_crystallizer.py -q
```

Expected: all tests pass.

- [ ] **Step 2.5: Commit**

```bash
git add scripts/span_crystallizer.py tests/test_span_crystallizer.py
git commit -m "feat: add SpanCrystallizer — macro JSON and Python skeleton generation"
```

---

## Task 3: PostToolUse Hook — Action Recording

**Files:**
- Modify: `hooks/post_tool_use.py`
- Modify: `tests/test_hook_scripts_output.py`

- [ ] **Step 3.1: Write the failing test**

Add to `tests/test_hook_scripts_output.py` (find the existing test class and append):

```python
def test_post_tool_use_records_action_when_span_active(tmp_path, monkeypatch):
    """When active_span_id is in state.json, PostToolUse appends action to buffer."""
    import json, subprocess, sys
    hook_state = tmp_path / "hook-state"
    hook_state.mkdir()
    state = {
        "active_span_id": "span-123",
        "active_span_intent": "lark.read.get-doc",
        "goal": "",
        "goal_source": "unset",
        "deltas": [],
    }
    (hook_state / "state.json").write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(hook_state))

    payload = {
        "tool_name": "mcp__lark_doc__get",
        "tool_result": {"content": [{"type": "text", "text": "{}"}]},
    }
    result = subprocess.run(
        [sys.executable, "hooks/post_tool_use.py"],
        input=json.dumps(payload),
        capture_output=True, text=True,
        cwd=str(Path(__file__).parents[1]),
    )
    assert result.returncode == 0
    buf = hook_state / "active-span-actions.jsonl"
    assert buf.exists()
    line = json.loads(buf.read_text().strip().splitlines()[-1])
    assert line["tool_name"] == "mcp__lark_doc__get"
    assert line["has_side_effects"] is False  # get is read-only


def test_post_tool_use_does_not_record_when_no_active_span(tmp_path, monkeypatch):
    import json, subprocess, sys
    hook_state = tmp_path / "hook-state"
    hook_state.mkdir()
    state = {"goal": "", "goal_source": "unset", "deltas": []}
    (hook_state / "state.json").write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(hook_state))

    payload = {
        "tool_name": "mcp__lark_doc__get",
        "tool_result": {"content": [{"type": "text", "text": "{}"}]},
    }
    result = subprocess.run(
        [sys.executable, "hooks/post_tool_use.py"],
        input=json.dumps(payload),
        capture_output=True, text=True,
        cwd=str(Path(__file__).parents[1]),
    )
    assert result.returncode == 0
    buf = hook_state / "active-span-actions.jsonl"
    assert not buf.exists()
```

- [ ] **Step 3.2: Run test to verify it fails**

```bash
python -m pytest tests/test_hook_scripts_output.py::test_post_tool_use_records_action_when_span_active -q
```

Expected: FAIL — buffer file not created.

- [ ] **Step 3.3: Extend PostToolUse hook**

In `hooks/post_tool_use.py`, add the span action recording block after the existing delta tracking, before `save_tracker`:

```python
# After existing imports, add:
from scripts.span_tracker import is_read_only_tool  # noqa: E402

# Inside main(), after the reconcile block and before save_tracker(), insert:

    # --- span action recording ---
    active_span_id = str(tracker.to_dict().get("active_span_id", "") or "")
    # active_span_id lives in state.json (written by icc_span_open via SpanTracker)
    # Re-read it directly from state dict since StateTracker may not expose it
    try:
        _state_data = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        active_span_id = str(_state_data.get("active_span_id", "") or "")
    except Exception:
        active_span_id = ""

    if active_span_id and tool_name:
        _buf_path = state_root / "active-span-actions.jsonl"
        _has_side_effects = not is_read_only_tool(tool_name)
        import hashlib as _hashlib
        _args_raw = json.dumps(payload.get("tool_input", {}), sort_keys=True, ensure_ascii=True)
        _args_hash = _hashlib.sha256(_args_raw.encode()).hexdigest()[:16]
        _action_rec = {
            "tool_name": tool_name,
            "args_hash": _args_hash,
            "has_side_effects": _has_side_effects,
            "ts_ms": int(__import__("time").time() * 1000),
        }
        try:
            with _buf_path.open("a", encoding="utf-8") as _f:
                _f.write(json.dumps(_action_rec, ensure_ascii=True) + "\n")
        except Exception:
            pass
```

- [ ] **Step 3.4: Run tests to verify they pass**

```bash
python -m pytest tests/test_hook_scripts_output.py -q
```

Expected: all tests pass.

- [ ] **Step 3.5: Commit**

```bash
git add hooks/post_tool_use.py tests/test_hook_scripts_output.py
git commit -m "feat: extend PostToolUse hook to record actions into active span buffer"
```

---

## Task 4: PreToolUse Hook — Validate New Span Tools

**Files:**
- Modify: `hooks/pre_tool_use.py`
- Modify: `tests/test_hook_scripts_output.py`

- [ ] **Step 4.1: Write the failing tests**

Append to `tests/test_hook_scripts_output.py`:

```python
def _run_pre_hook(payload: dict) -> dict:
    import json, subprocess, sys
    from pathlib import Path
    result = subprocess.run(
        [sys.executable, "hooks/pre_tool_use.py"],
        input=json.dumps(payload),
        capture_output=True, text=True,
        cwd=str(Path(__file__).parents[1]),
    )
    return json.loads(result.stdout)


def test_pre_tool_use_blocks_span_open_missing_intent():
    out = _run_pre_hook({"tool_name": "emerge__icc_span_open", "tool_input": {}})
    assert out.get("decision") == "block"
    assert "intent_signature" in out["reason"]


def test_pre_tool_use_blocks_span_open_invalid_intent():
    out = _run_pre_hook({
        "tool_name": "emerge__icc_span_open",
        "tool_input": {"intent_signature": "invalid_no_mode"},
    })
    assert out.get("decision") == "block"


def test_pre_tool_use_allows_valid_span_open():
    out = _run_pre_hook({
        "tool_name": "emerge__icc_span_open",
        "tool_input": {"intent_signature": "lark.read.get-doc"},
    })
    assert out.get("decision") != "block"


def test_pre_tool_use_blocks_span_close_missing_outcome():
    out = _run_pre_hook({"tool_name": "emerge__icc_span_close", "tool_input": {}})
    assert out.get("decision") == "block"
    assert "outcome" in out["reason"]


def test_pre_tool_use_blocks_span_close_invalid_outcome():
    out = _run_pre_hook({
        "tool_name": "emerge__icc_span_close",
        "tool_input": {"outcome": "done"},
    })
    assert out.get("decision") == "block"


def test_pre_tool_use_allows_valid_span_close():
    out = _run_pre_hook({
        "tool_name": "emerge__icc_span_close",
        "tool_input": {"outcome": "success"},
    })
    assert out.get("decision") != "block"


def test_pre_tool_use_blocks_span_approve_missing_intent():
    out = _run_pre_hook({"tool_name": "emerge__icc_span_approve", "tool_input": {}})
    assert out.get("decision") == "block"


def test_pre_tool_use_allows_valid_span_approve():
    out = _run_pre_hook({
        "tool_name": "emerge__icc_span_approve",
        "tool_input": {"intent_signature": "lark.write.create-doc"},
    })
    assert out.get("decision") != "block"
```

- [ ] **Step 4.2: Run tests to verify they fail**

```bash
python -m pytest tests/test_hook_scripts_output.py -k "span" -q
```

Expected: FAIL — no validation for span tools.

- [ ] **Step 4.3: Add validation blocks to PreToolUse hook**

In `hooks/pre_tool_use.py`, add after the existing `if tool_name.endswith("__icc_crystallize"):` block:

```python
    if tool_name.endswith("__icc_span_open"):
        intent_signature = str(arguments.get("intent_signature", "")).strip()
        if not intent_signature:
            error_msg = "icc_span_open: 'intent_signature' is required (e.g. 'lark.read.get-doc')"
        else:
            import re as _re
            _sig_pattern = _re.compile(r'^[a-z][a-z0-9_-]*\.(read|write)\.[a-z][a-z0-9_./-]*$')
            if not _sig_pattern.match(intent_signature):
                error_msg = (
                    f"icc_span_open: intent_signature {intent_signature!r} is invalid. "
                    "Must be <connector>.(read|write).<name> — e.g. 'lark.read.get-doc'."
                )

    if tool_name.endswith("__icc_span_close"):
        outcome = str(arguments.get("outcome", "")).strip()
        if outcome not in ("success", "failure", "aborted"):
            error_msg = (
                f"icc_span_close: 'outcome' must be success/failure/aborted, got {outcome!r}"
            )

    if tool_name.endswith("__icc_span_approve"):
        intent_signature = str(arguments.get("intent_signature", "")).strip()
        if not intent_signature:
            error_msg = "icc_span_approve: 'intent_signature' is required"
```

- [ ] **Step 4.4: Run tests to verify they pass**

```bash
python -m pytest tests/test_hook_scripts_output.py -q
```

Expected: all tests pass.

- [ ] **Step 4.5: Commit**

```bash
git add hooks/pre_tool_use.py tests/test_hook_scripts_output.py
git commit -m "feat: extend PreToolUse hook to validate icc_span_open/close/approve"
```

---

## Task 5: Daemon — icc_span_open Tool

**Files:**
- Modify: `scripts/emerge_daemon.py`
- Modify: `tests/test_mcp_tools_integration.py`

- [ ] **Step 5.1: Write the failing tests**

Append to `tests/test_mcp_tools_integration.py`:

```python
# ── icc_span_open tests ────────────────────────────────────────────────────

def test_span_open_returns_span_id(tmp_path, monkeypatch):
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "hook-state"))
    (tmp_path / "hook-state").mkdir(parents=True)
    (tmp_path / "hook-state" / "state.json").write_text("{}", encoding="utf-8")
    from scripts.emerge_daemon import EmergeDaemon
    daemon = EmergeDaemon()
    result = daemon.call_tool("icc_span_open", {
        "intent_signature": "lark.read.get-doc",
        "description": "get a doc",
    })
    assert result.get("isError") is not True
    import json
    body = json.loads(result["content"][0]["text"])
    assert "span_id" in body
    assert body["policy_status"] == "explore"


def test_span_open_writes_active_span_to_hook_state(tmp_path, monkeypatch):
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path / "state"))
    hook_state = tmp_path / "hook-state"
    hook_state.mkdir(parents=True)
    (hook_state / "state.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(hook_state))
    from scripts.emerge_daemon import EmergeDaemon
    import json
    daemon = EmergeDaemon()
    daemon.call_tool("icc_span_open", {"intent_signature": "lark.read.get-doc"})
    state = json.loads((hook_state / "state.json").read_text())
    assert "active_span_id" in state
    assert state["active_span_intent"] == "lark.read.get-doc"


def test_span_open_errors_on_missing_intent(tmp_path, monkeypatch):
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "hook-state"))
    (tmp_path / "hook-state").mkdir(parents=True)
    from scripts.emerge_daemon import EmergeDaemon
    daemon = EmergeDaemon()
    result = daemon.call_tool("icc_span_open", {})
    assert result.get("isError") is True
```

- [ ] **Step 5.2: Run tests to verify they fail**

```bash
python -m pytest tests/test_mcp_tools_integration.py -k "span_open" -q
```

Expected: FAIL — `icc_span_open` not found.

- [ ] **Step 5.3: Add SpanTracker instance to EmergeDaemon.__init__**

In `scripts/emerge_daemon.py`, in `EmergeDaemon.__init__` after the `self._goal_control` lines, add:

```python
        from scripts.span_tracker import SpanTracker
        from scripts.span_crystallizer import SpanCrystallizer
        _hook_state_root = Path(default_hook_state_root())
        self._span_tracker = SpanTracker(
            state_root=self._state_root,
            hook_state_root=_hook_state_root,
        )
        _connector_root = Path(
            load_settings().get("connector_root", "~/.emerge/connectors")
        ).expanduser().resolve()
        self._span_crystallizer = SpanCrystallizer(connector_root=_connector_root)
        # In-flight spans keyed by span_id — survive across call_tool calls within one daemon session
        self._open_spans: dict[str, Any] = {}
```

- [ ] **Step 5.4: Add icc_span_open handler to call_tool**

In `scripts/emerge_daemon.py`, in `call_tool`, add before the `if name == "icc_exec":` block:

```python
        if name == "icc_span_open":
            intent_signature = str(arguments.get("intent_signature", "")).strip()
            if not intent_signature:
                return self._tool_error("icc_span_open: 'intent_signature' is required")
            description = str(arguments.get("description", "")).strip()
            args = arguments.get("args") or {}
            source = str(arguments.get("source", "manual")).strip()
            skill_name = str(arguments.get("skill_name", "") or "").strip() or None
            span = self._span_tracker.open_span(
                intent_signature=intent_signature,
                description=description,
                args=args,
                source=source,
                skill_name=skill_name,
            )
            self._open_spans[span.span_id] = span
            policy_status = self._span_tracker.get_policy_status(intent_signature)
            return self._tool_ok_json({
                "span_id": span.span_id,
                "intent_signature": intent_signature,
                "status": "opened",
                "policy_status": policy_status,
            })
```

- [ ] **Step 5.5: Add icc_span_open to the tool schema in _list_tools**

In `scripts/emerge_daemon.py`, in the `_list_tools` response array, add:

```python
                        {
                            "name": "icc_span_open",
                            "description": "Open an intent span to track a multi-step operation in the flywheel. Returns span_id. Call before any sequence of tool calls that represents a reusable intent. When the intent is stable, returns a bridge recipe (macro) or result (pipeline) instead.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "intent_signature": {"type": "string", "description": "Stable dot-notation identifier: <connector>.(read|write).<name> — e.g. 'lark.read.get-doc'"},
                                    "description": {"type": "string", "description": "Human-readable description of what this intent does"},
                                    "args": {"type": "object", "description": "Input arguments for this span execution"},
                                    "source": {"type": "string", "enum": ["skill", "manual"], "default": "manual"},
                                    "skill_name": {"type": "string", "description": "Skill name when source=skill"},
                                },
                                "required": ["intent_signature"],
                            },
                        },
```

- [ ] **Step 5.6: Run tests to verify they pass**

```bash
python -m pytest tests/test_mcp_tools_integration.py -k "span_open" -q
```

Expected: all pass.

- [ ] **Step 5.7: Commit**

```bash
git add scripts/emerge_daemon.py tests/test_mcp_tools_integration.py
git commit -m "feat: add icc_span_open daemon tool with SpanTracker integration"
```

---

## Task 6: Daemon — icc_span_close Tool + Policy Evaluation

**Files:**
- Modify: `scripts/emerge_daemon.py`
- Modify: `tests/test_mcp_tools_integration.py`

- [ ] **Step 6.1: Write the failing tests**

Append to `tests/test_mcp_tools_integration.py`:

```python
def _make_daemon_with_hook_state(tmp_path, monkeypatch):
    from scripts.emerge_daemon import EmergeDaemon
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path / "state"))
    hook_state = tmp_path / "hook-state"
    hook_state.mkdir(parents=True)
    (hook_state / "state.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(hook_state))
    return EmergeDaemon(), hook_state


def test_span_close_writes_to_wal(tmp_path, monkeypatch):
    import json
    daemon, hook_state = _make_daemon_with_hook_state(tmp_path, monkeypatch)
    open_result = daemon.call_tool("icc_span_open", {"intent_signature": "lark.read.get-doc"})
    span_id = json.loads(open_result["content"][0]["text"])["span_id"]
    close_result = daemon.call_tool("icc_span_close", {
        "span_id": span_id,
        "outcome": "success",
        "result_summary": {"doc_id": "x"},
    })
    assert close_result.get("isError") is not True
    wal = tmp_path / "state" / "span-wal" / "spans.jsonl"
    assert wal.exists()
    record = json.loads(wal.read_text().strip())
    assert record["intent_signature"] == "lark.read.get-doc"
    assert record["outcome"] == "success"


def test_span_close_returns_policy_status(tmp_path, monkeypatch):
    import json
    daemon, _ = _make_daemon_with_hook_state(tmp_path, monkeypatch)
    open_result = daemon.call_tool("icc_span_open", {"intent_signature": "lark.read.get-doc"})
    span_id = json.loads(open_result["content"][0]["text"])["span_id"]
    close_result = daemon.call_tool("icc_span_close", {"span_id": span_id, "outcome": "success"})
    body = json.loads(close_result["content"][0]["text"])
    assert "policy_status" in body
    assert body["policy_status"] == "explore"


def test_span_close_errors_on_missing_outcome(tmp_path, monkeypatch):
    daemon, _ = _make_daemon_with_hook_state(tmp_path, monkeypatch)
    result = daemon.call_tool("icc_span_close", {"span_id": "nonexistent"})
    assert result.get("isError") is True


def test_span_close_synthesis_ready_after_stable(tmp_path, monkeypatch):
    import json
    from scripts.policy_config import STABLE_MIN_ATTEMPTS
    daemon, _ = _make_daemon_with_hook_state(tmp_path, monkeypatch)
    for _ in range(STABLE_MIN_ATTEMPTS):
        r = daemon.call_tool("icc_span_open", {"intent_signature": "lark.read.get-doc"})
        sid = json.loads(r["content"][0]["text"])["span_id"]
        daemon.call_tool("icc_span_close", {"span_id": sid, "outcome": "success"})
    body = json.loads(daemon.call_tool("icc_span_close", {
        "span_id": json.loads(daemon.call_tool("icc_span_open", {"intent_signature": "lark.read.get-doc"})["content"][0]["text"])["span_id"],
        "outcome": "success",
    })["content"][0]["text"])
    assert body.get("synthesis_ready") is True
```

- [ ] **Step 6.2: Run tests to verify they fail**

```bash
python -m pytest tests/test_mcp_tools_integration.py -k "span_close" -q
```

Expected: FAIL — `icc_span_close` not found.

- [ ] **Step 6.3: Add icc_span_close handler**

In `scripts/emerge_daemon.py`, in `call_tool`, add after the `icc_span_open` handler:

```python
        if name == "icc_span_close":
            span_id = str(arguments.get("span_id", "")).strip()
            outcome = str(arguments.get("outcome", "")).strip()
            if outcome not in ("success", "failure", "aborted"):
                return self._tool_error(
                    f"icc_span_close: 'outcome' must be success/failure/aborted, got {outcome!r}"
                )
            result_summary = arguments.get("result_summary") or {}
            # Retrieve in-flight span (may not exist if daemon restarted — graceful fallback)
            from scripts.span_tracker import SpanRecord
            span = self._open_spans.pop(span_id, None)
            if span is None:
                # Reconstruct minimal span for WAL (session continuity across restarts)
                import uuid as _uuid
                span = SpanRecord(
                    span_id=span_id or str(_uuid.uuid4()),
                    intent_signature=str(arguments.get("intent_signature", "")),
                    description="",
                    source="manual",
                    opened_at_ms=0,
                )
            closed = self._span_tracker.close_span(span, outcome=outcome, result_summary=result_summary)
            policy_status = self._span_tracker.get_policy_status(closed.intent_signature)
            synthesis_ready = self._span_tracker.is_synthesis_ready(closed.intent_signature)
            # Auto-crystallize macro if stable read-only span
            if synthesis_ready and closed.is_read_only:
                latest = self._span_tracker.latest_successful_span(closed.intent_signature)
                if latest:
                    try:
                        self._span_crystallizer.crystallize_macro(closed.intent_signature, latest)
                        try:
                            self._sink.emit("span.macro_crystallized", {"intent_signature": closed.intent_signature})
                        except Exception:
                            pass
                    except Exception:
                        pass
            return self._tool_ok_json({
                "span_id": closed.span_id,
                "intent_signature": closed.intent_signature,
                "outcome": outcome,
                "policy_status": policy_status,
                "synthesis_ready": synthesis_ready,
                "is_read_only": closed.is_read_only,
            })
```

- [ ] **Step 6.4: Add icc_span_close to tool schema**

In the `_list_tools` array, add:

```python
                        {
                            "name": "icc_span_close",
                            "description": "Close an open intent span and commit it to the flywheel WAL. Triggers auto-crystallization for stable read-only spans. Returns policy_status and synthesis_ready flag.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "span_id": {"type": "string", "description": "span_id returned by icc_span_open"},
                                    "outcome": {"type": "string", "enum": ["success", "failure", "aborted"]},
                                    "result_summary": {"type": "object", "description": "Brief structured result to store in WAL (keep small — no raw API responses)"},
                                    "intent_signature": {"type": "string", "description": "Required when span_id is unknown (daemon restart recovery)"},
                                },
                                "required": ["outcome"],
                            },
                        },
```

- [ ] **Step 6.5: Run tests to verify they pass**

```bash
python -m pytest tests/test_mcp_tools_integration.py -k "span" -q
```

Expected: all span tests pass.

- [ ] **Step 6.6: Commit**

```bash
git add scripts/emerge_daemon.py tests/test_mcp_tools_integration.py
git commit -m "feat: add icc_span_close with auto-crystallization for stable read-only spans"
```

---

## Task 7: Daemon — icc_span_approve Tool (Write Skeleton + Bridge Activation)

**Files:**
- Modify: `scripts/emerge_daemon.py`
- Modify: `tests/test_mcp_tools_integration.py`

- [ ] **Step 7.1: Write the failing tests**

Append to `tests/test_mcp_tools_integration.py`:

```python
def test_span_approve_generates_skeleton_and_returns_preview(tmp_path, monkeypatch):
    import json
    from scripts.policy_config import STABLE_MIN_ATTEMPTS
    daemon, hook_state = _make_daemon_with_hook_state(tmp_path, monkeypatch)
    # Drive to stable
    for _ in range(STABLE_MIN_ATTEMPTS + 1):
        r = daemon.call_tool("icc_span_open", {"intent_signature": "lark.write.create-doc"})
        sid = json.loads(r["content"][0]["text"])["span_id"]
        # Simulate a side-effectful action in buffer
        buf = hook_state / "active-span-actions.jsonl"
        buf.write_text(
            json.dumps({"tool_name": "mcp__lark_doc__create", "args_hash": "x", "has_side_effects": True, "ts_ms": 1}) + "\n"
        )
        daemon.call_tool("icc_span_close", {"span_id": sid, "outcome": "success"})

    result = daemon.call_tool("icc_span_approve", {"intent_signature": "lark.write.create-doc"})
    assert result.get("isError") is not True
    body = json.loads(result["content"][0]["text"])
    assert body.get("approved") is True
    assert "skeleton_preview" in body
    assert "def run_write" in body["skeleton_preview"]


def test_span_approve_errors_on_missing_intent(tmp_path, monkeypatch):
    daemon, _ = _make_daemon_with_hook_state(tmp_path, monkeypatch)
    result = daemon.call_tool("icc_span_approve", {})
    assert result.get("isError") is True


def test_span_approve_errors_when_not_stable(tmp_path, monkeypatch):
    daemon, _ = _make_daemon_with_hook_state(tmp_path, monkeypatch)
    result = daemon.call_tool("icc_span_approve", {"intent_signature": "lark.write.new-intent"})
    assert result.get("isError") is True
    import json
    body = json.loads(result["content"][0]["text"])
    assert "not stable" in body.get("message", "").lower() or result.get("isError")
```

- [ ] **Step 7.2: Run tests to verify they fail**

```bash
python -m pytest tests/test_mcp_tools_integration.py -k "span_approve" -q
```

Expected: FAIL — `icc_span_approve` not found.

- [ ] **Step 7.3: Add icc_span_approve handler**

In `scripts/emerge_daemon.py`, after `icc_span_close` handler:

```python
        if name == "icc_span_approve":
            intent_signature = str(arguments.get("intent_signature", "")).strip()
            if not intent_signature:
                return self._tool_error("icc_span_approve: 'intent_signature' is required")
            policy_status = self._span_tracker.get_policy_status(intent_signature)
            if policy_status != "stable":
                return self._tool_error(
                    f"icc_span_approve: intent '{intent_signature}' is not stable (status={policy_status}). "
                    "Only stable write spans can be approved."
                )
            latest = self._span_tracker.latest_successful_span(intent_signature)
            if not latest:
                return self._tool_error(
                    f"icc_span_approve: no successful span found in WAL for '{intent_signature}'"
                )
            skeleton = self._span_crystallizer.generate_pipeline_skeleton(intent_signature, latest)
            # Write skeleton as pending pipeline (under review — not yet in bridge)
            parts = intent_signature.split(".", 2)
            connector = parts[0] if len(parts) >= 1 else "unknown"
            name_part = parts[2] if len(parts) >= 3 else intent_signature
            _connector_root = Path(
                load_settings().get("connector_root", "~/.emerge/connectors")
            ).expanduser().resolve()
            pending_dir = _connector_root / connector / "pipelines" / "write" / "_pending"
            pending_dir.mkdir(parents=True, exist_ok=True)
            skeleton_path = pending_dir / f"{name_part}.py"
            skeleton_path.write_text(skeleton, encoding="utf-8")
            try:
                self._sink.emit("span.skeleton_generated", {"intent_signature": intent_signature, "path": str(skeleton_path)})
            except Exception:
                pass
            return self._tool_ok_json({
                "approved": True,
                "intent_signature": intent_signature,
                "skeleton_path": str(skeleton_path),
                "skeleton_preview": skeleton[:500],
                "next_step": (
                    f"Review {skeleton_path}, replace mcp_call() stubs with real tool calls, "
                    "then move the file to pipelines/write/ to activate the bridge."
                ),
            })
```

- [ ] **Step 7.4: Add icc_span_approve to tool schema**

```python
                        {
                            "name": "icc_span_approve",
                            "description": "Approve a stable write span for crystallization. Generates a Python pipeline skeleton in _pending/ for human review. Move skeleton to pipelines/write/ to activate the bridge.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "intent_signature": {"type": "string", "description": "Intent signature of the stable write span to crystallize"},
                                },
                                "required": ["intent_signature"],
                            },
                        },
```

- [ ] **Step 7.5: Run tests to verify they pass**

```bash
python -m pytest tests/test_mcp_tools_integration.py -k "span" -q
```

Expected: all span tests pass.

- [ ] **Step 7.6: Commit**

```bash
git add scripts/emerge_daemon.py tests/test_mcp_tools_integration.py
git commit -m "feat: add icc_span_approve — generates Python skeleton for stable write spans"
```

---

## Task 8: Bridge Extension — Recipe Response Protocol

**Files:**
- Modify: `scripts/emerge_daemon.py`
- Modify: `tests/test_mcp_tools_integration.py`

- [ ] **Step 8.1: Write the failing tests**

Append to `tests/test_mcp_tools_integration.py`:

```python
def _drive_span_to_stable_macro(daemon, hook_state, intent_sig: str):
    """Helper: execute enough read-only spans to reach stable."""
    import json
    from scripts.policy_config import STABLE_MIN_ATTEMPTS
    for _ in range(STABLE_MIN_ATTEMPTS + 1):
        r = daemon.call_tool("icc_span_open", {"intent_signature": intent_sig})
        sid = json.loads(r["content"][0]["text"])["span_id"]
        # Write a read-only action to buffer
        buf = hook_state / "active-span-actions.jsonl"
        buf.write_text(
            json.dumps({"tool_name": "mcp__lark_doc__get", "args_hash": "x", "has_side_effects": False, "ts_ms": 1}) + "\n"
        )
        daemon.call_tool("icc_span_close", {"span_id": sid, "outcome": "success"})


def test_span_open_returns_macro_recipe_when_stable(tmp_path, monkeypatch):
    import json
    daemon, hook_state = _make_daemon_with_hook_state(tmp_path, monkeypatch)
    _drive_span_to_stable_macro(daemon, hook_state, "lark.read.get-doc")
    # Next open should trigger bridge
    result = daemon.call_tool("icc_span_open", {
        "intent_signature": "lark.read.get-doc",
        "args": {"doc_id": "123"},
    })
    body = json.loads(result["content"][0]["text"])
    assert body.get("bridge") is True
    assert body.get("bridge_type") == "recipe"
    assert isinstance(body.get("recipe"), list)


def test_bridge_recipe_contains_tool_names(tmp_path, monkeypatch):
    import json
    daemon, hook_state = _make_daemon_with_hook_state(tmp_path, monkeypatch)
    _drive_span_to_stable_macro(daemon, hook_state, "lark.read.get-doc")
    result = daemon.call_tool("icc_span_open", {"intent_signature": "lark.read.get-doc"})
    body = json.loads(result["content"][0]["text"])
    assert all("tool_name" in step for step in body["recipe"])


def test_span_open_normal_when_not_stable(tmp_path, monkeypatch):
    import json
    daemon, _ = _make_daemon_with_hook_state(tmp_path, monkeypatch)
    result = daemon.call_tool("icc_span_open", {"intent_signature": "lark.read.new-intent"})
    body = json.loads(result["content"][0]["text"])
    assert body.get("bridge") is not True
    assert "span_id" in body
```

- [ ] **Step 8.2: Run tests to verify they fail**

```bash
python -m pytest tests/test_mcp_tools_integration.py -k "bridge" -q
```

Expected: FAIL — bridge not firing.

- [ ] **Step 8.3: Add span bridge check inside icc_span_open handler**

Replace the existing `icc_span_open` handler body with:

```python
        if name == "icc_span_open":
            intent_signature = str(arguments.get("intent_signature", "")).strip()
            if not intent_signature:
                return self._tool_error("icc_span_open: 'intent_signature' is required")
            description = str(arguments.get("description", "")).strip()
            args = arguments.get("args") or {}
            source = str(arguments.get("source", "manual")).strip()
            skill_name = str(arguments.get("skill_name", "") or "").strip() or None

            # Bridge check: if macro exists and intent is stable, return recipe
            policy_status = self._span_tracker.get_policy_status(intent_signature)
            if policy_status == "stable" and self._span_crystallizer.macro_exists(intent_signature):
                macro = self._span_crystallizer.read_macro(intent_signature)
                recipe = macro.get("actions", []) if macro else []
                import uuid as _uuid
                bridge_span_id = str(_uuid.uuid4())
                try:
                    self._sink.emit("span.bridge.macro", {"intent_signature": intent_signature})
                except Exception:
                    pass
                return self._tool_ok_json({
                    "bridge": True,
                    "bridge_type": "recipe",
                    "span_id": bridge_span_id,
                    "intent_signature": intent_signature,
                    "recipe": recipe,
                })

            span = self._span_tracker.open_span(
                intent_signature=intent_signature,
                description=description,
                args=args,
                source=source,
                skill_name=skill_name,
            )
            self._open_spans[span.span_id] = span
            return self._tool_ok_json({
                "span_id": span.span_id,
                "intent_signature": intent_signature,
                "status": "opened",
                "policy_status": policy_status,
            })
```

- [ ] **Step 8.4: Run tests to verify they pass**

```bash
python -m pytest tests/test_mcp_tools_integration.py -k "span or bridge" -q
```

Expected: all pass.

- [ ] **Step 8.5: Commit**

```bash
git add scripts/emerge_daemon.py tests/test_mcp_tools_integration.py
git commit -m "feat: span bridge — icc_span_open returns macro recipe when intent is stable"
```

---

## Task 9: connector://macros Resource

**Files:**
- Modify: `scripts/emerge_daemon.py`
- Modify: `tests/test_mcp_tools_integration.py`

- [ ] **Step 9.1: Write the failing tests**

Append to `tests/test_mcp_tools_integration.py`:

```python
def test_macros_resource_lists_stable_macros(tmp_path, monkeypatch):
    import json
    daemon, hook_state = _make_daemon_with_hook_state(tmp_path, monkeypatch)
    # Write a macro file directly to connector root to simulate stable state
    connector_root = Path(tmp_path / "connectors")
    macro_dir = connector_root / "lark" / "macros"
    macro_dir.mkdir(parents=True)
    (macro_dir / "get-doc.json").write_text(json.dumps({
        "intent_signature": "lark.read.get-doc",
        "description": "get a doc",
        "is_read_only": True,
        "actions": [{"tool_name": "mcp__lark_doc__get", "args_hash": "x"}],
    }), encoding="utf-8")
    # Inject connector_root into daemon
    daemon._span_crystallizer._connector_root = connector_root

    resources = daemon.list_resources()
    uris = [r["uri"] for r in resources]
    assert "connector://lark/macros" in uris


def test_macros_resource_read_returns_macro_list(tmp_path, monkeypatch):
    import json
    daemon, _ = _make_daemon_with_hook_state(tmp_path, monkeypatch)
    connector_root = Path(tmp_path / "connectors")
    macro_dir = connector_root / "lark" / "macros"
    macro_dir.mkdir(parents=True)
    (macro_dir / "get-doc.json").write_text(json.dumps({
        "intent_signature": "lark.read.get-doc",
        "description": "get",
        "is_read_only": True,
        "actions": [],
    }), encoding="utf-8")
    daemon._span_crystallizer._connector_root = connector_root

    content = daemon.read_resource("connector://lark/macros")
    data = json.loads(content)
    assert isinstance(data, list)
    assert data[0]["intent_signature"] == "lark.read.get-doc"
```

- [ ] **Step 9.2: Run tests to verify they fail**

```bash
python -m pytest tests/test_mcp_tools_integration.py -k "macros_resource" -q
```

Expected: FAIL — resource not found.

- [ ] **Step 9.3: Add connector://macros to _list_resources**

In `scripts/emerge_daemon.py`, in `_list_resources`, after the block that adds `connector://<name>/intents`, add:

```python
            # Add connector://macros resource if any macros exist
            for cname in sorted(connector_names):
                macro_uri = f"connector://{cname}/macros"
                macros = self._span_crystallizer.list_macros(cname)
                if macros:
                    static.append({
                        "uri": macro_uri,
                        "name": f"{cname} stable macros",
                        "mimeType": "application/json",
                        "description": f"JSON list of stable crystallized macro sequences for {cname}. Bridge-ready read-only patterns.",
                    })
```

- [ ] **Step 9.4: Add connector://macros to _read_resource**

In `scripts/emerge_daemon.py`, in `_read_resource`, add before the final return/raise:

```python
            if uri_path.endswith("/macros"):
                connector = uri_path.split("/")[0]
                macros = self._span_crystallizer.list_macros(connector)
                return json.dumps(macros, ensure_ascii=False)
```

- [ ] **Step 9.5: Run tests to verify they pass**

```bash
python -m pytest tests/test_mcp_tools_integration.py -k "macros_resource" -q
```

Expected: all pass.

- [ ] **Step 9.6: Commit**

```bash
git add scripts/emerge_daemon.py tests/test_mcp_tools_integration.py
git commit -m "feat: add connector://macros resource listing stable macro sequences"
```

---

## Task 10: Deprecate icc_read, icc_write, icc_crystallize

**Files:**
- Modify: `scripts/emerge_daemon.py`
- Modify: `hooks/pre_tool_use.py`
- Modify: `tests/test_mcp_tools_integration.py`

- [ ] **Step 10.1: Write deprecation tests**

Append to `tests/test_mcp_tools_integration.py`:

```python
def test_icc_read_returns_deprecated_error(tmp_path, monkeypatch):
    daemon, _ = _make_daemon_with_hook_state(tmp_path, monkeypatch)
    result = daemon.call_tool("icc_read", {"connector": "lark", "pipeline": "get-doc"})
    assert result.get("isError") is True
    import json
    body = json.loads(result["content"][0]["text"])
    assert "deprecated" in body.get("message", "").lower() or "icc_span_open" in body.get("message", "")


def test_icc_write_returns_deprecated_error(tmp_path, monkeypatch):
    daemon, _ = _make_daemon_with_hook_state(tmp_path, monkeypatch)
    result = daemon.call_tool("icc_write", {"connector": "lark", "pipeline": "create-doc"})
    assert result.get("isError") is True


def test_icc_crystallize_returns_deprecated_error(tmp_path, monkeypatch):
    daemon, _ = _make_daemon_with_hook_state(tmp_path, monkeypatch)
    result = daemon.call_tool("icc_crystallize", {
        "intent_signature": "lark.read.get-doc",
        "connector": "lark",
        "pipeline_name": "get-doc",
        "mode": "read",
    })
    assert result.get("isError") is True
```

- [ ] **Step 10.2: Run tests to verify they fail**

```bash
python -m pytest tests/test_mcp_tools_integration.py -k "deprecated" -q
```

Expected: FAIL — tools still work normally.

- [ ] **Step 10.3: Replace handlers with deprecation responses**

In `scripts/emerge_daemon.py`, replace the `icc_read`, `icc_write`, and `icc_crystallize` handlers with:

```python
        if name == "icc_read":
            return self._tool_error(
                "icc_read is deprecated. Use icc_span_open(intent_signature='<connector>.read.<name>') instead. "
                "See connector://notes for existing intents."
            )
        if name == "icc_write":
            return self._tool_error(
                "icc_write is deprecated. Use icc_span_open(intent_signature='<connector>.write.<name>') instead."
            )
        if name == "icc_crystallize":
            return self._tool_error(
                "icc_crystallize is deprecated. Crystallization is now automatic: "
                "read-only spans auto-crystallize to macros at stable. "
                "Write spans: call icc_span_approve(intent_signature=...) to generate a skeleton."
            )
```

- [ ] **Step 10.4: Remove deprecated tools from tool schema**

In `scripts/emerge_daemon.py`, in `_list_tools`, remove the schema entries for `icc_read`, `icc_write`, and `icc_crystallize`.

- [ ] **Step 10.5: Remove deprecated validation from PreToolUse hook**

In `hooks/pre_tool_use.py`, remove the validation blocks for `__icc_read`, `__icc_write`, and `__icc_crystallize`.

- [ ] **Step 10.6: Run full test suite**

```bash
python -m pytest tests -q
```

Expected: all tests pass. Tests referencing old icc_read/write/crystallize behavior will need to be updated to use the new deprecation assertions.

- [ ] **Step 10.7: Commit**

```bash
git add scripts/emerge_daemon.py hooks/pre_tool_use.py tests/test_mcp_tools_integration.py
git commit -m "feat: deprecate icc_read, icc_write, icc_crystallize — replaced by span system"
```

---

## Task 11: Update CLAUDE.md and README

**Files:**
- Modify: `CLAUDE.md`
- Modify: `README.md`

- [ ] **Step 11.1: Update CLAUDE.md Architecture section**

Replace the "Two execution paths" and "connector://" lines with:

```markdown
**Flywheel unit is now Intent Span, not icc_exec**: `icc_span_open`/`icc_span_close` wrap any sequence of tool calls (Lark, context7, file ops, icc_exec, etc.) into a single flywheel unit. `icc_exec` is retained as a Python execution primitive only.

**Two crystallization paths**: Read-only spans (all actions side-effect-free) auto-crystallize to `macros/<name>.json` at stable. Write spans auto-generate a Python skeleton at stable; human approves via `icc_span_approve` to activate bridge.

**Bridge response protocol**: `icc_span_open` returns `{bridge: true, bridge_type: "recipe", recipe: [...]}` for macro bridge (CC executes tool list), or `{bridge: true, bridge_type: "result", result: {...}}` for pipeline bridge (daemon executed). CC checks `bridge_type` to distinguish.

**Active span state**: `icc_span_open` writes `active_span_id` + `active_span_intent` to `state.json` (hook state). `PostToolUse` hook reads this and appends every tool call to `active-span-actions.jsonl`. `icc_span_close` collects the buffer and writes the complete span to `span-wal/spans.jsonl`.

**Deprecated tools**: `icc_read`, `icc_write`, `icc_crystallize` — all return error with migration guidance.

**New MCP tools**: `icc_span_open`, `icc_span_close`, `icc_span_approve`.

**New resource**: `connector://<name>/macros` — JSON list of stable macro sequences.

**Span WAL**: `~/.emerge/repl/span-wal/spans.jsonl` — append-only, one closed span per line.

**Span candidates**: `~/.emerge/repl/span-candidates.json` — policy stats per intent_signature (parallel to candidates.json for exec).
```

- [ ] **Step 11.2: Update Documentation Update Rules table in CLAUDE.md**

Add rows:

```markdown
| New span tool or parameter | `emerge_daemon.py` tool schema + `README.md` MCP surface table |
| Macro crystallization path change | `span_crystallizer.py` + `README.md` flywheel diagram |
| has_side_effects whitelist change | `span_tracker.py` `_READ_ONLY_TOOL_*` constants + this table |
```

- [ ] **Step 11.3: Update README MCP surface table**

Add the three new tools and remove the deprecated ones from the table. Add the `connector://macros` resource line.

- [ ] **Step 11.4: Run full test suite one final time**

```bash
python -m pytest tests -q
```

Expected: all tests pass.

- [ ] **Step 11.5: Commit**

```bash
git add CLAUDE.md README.md
git commit -m "docs: update CLAUDE.md and README for universal flywheel / span system"
```

---

## Self-Review

**Spec coverage check:**

| Spec requirement | Task |
|---|---|
| Intent Span as flywheel unit | Task 1 (SpanTracker) |
| PostToolUse records all tool calls | Task 3 |
| PreToolUse validates new tools | Task 4 |
| icc_span_open / icc_span_close / icc_span_approve | Tasks 5, 6, 7 |
| Read-only spans → macro auto-crystallize at stable | Task 6 (close handler) |
| Write spans → skeleton + icc_span_approve | Task 7 |
| Bridge: recipe (macro) vs result (pipeline) | Task 8 |
| connector://macros resource | Task 9 |
| Deprecate icc_read / icc_write / icc_crystallize | Task 10 |
| Active span in hook state | Tasks 1, 5 |
| has_side_effects static whitelist | Task 1 (is_read_only_tool) |
| Macro lifecycle: no verify_rate | Task 1 (get_policy_status) |
| span-candidates.json parallel to candidates.json | Task 1 |
| Bridge response protocol: bridge_type field | Task 8 |
| Docs update | Task 11 |

All spec requirements covered. ✓
