# Universal Flywheel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the muscle-memory flywheel beyond `icc_exec` to cover all MCP tool call sequences (Lark、context7、skill sequences 等), using a unified Python pipeline as the only crystallization target.

**Architecture:** Two observation paths (icc_exec WAL + Span WAL) converge on one product (Python pipeline) and one bridge (PipelineEngine). New: `SpanTracker` module tracks intent spans; `icc_exec` auto-crystallizes at synthesis_ready; three new MCP tools (`icc_span_open/close/approve`) replace `icc_read/icc_write`.

**Tech Stack:** Python 3.11+, existing `policy_config.py` thresholds, PyYAML, pytest, JSONL append-only WAL, atomic temp+rename writes.

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `scripts/span_tracker.py` | Create | SpanRecord, SpanTracker: WAL、candidates、policy lifecycle（无 verify_rate）|
| `hooks/session_start.py` | Modify | SessionStart 时清除 stale active_span_id |
| `hooks/post_tool_use.py` | Modify | active span 内追加 action（排除 icc_exec）|
| `hooks/pre_tool_use.py` | Modify | 校验 icc_span_open / icc_span_close / icc_span_approve |
| `scripts/emerge_daemon.py` | Modify | _auto_crystallize、icc_span_open/close/approve、span bridge、废弃 icc_read/icc_write |
| `tests/conftest.py` | Modify | 新增 isolate_hook_state fixture |
| `tests/test_span_tracker.py` | Create | SpanTracker 单元测试 |
| `tests/test_mcp_tools_integration.py` | Modify | span 工具集成测试 |
| `tests/test_hook_scripts_output.py` | Modify | hook 行为测试 |

---

## Task 1: SpanTracker 核心模块

**Files:**
- Create: `scripts/span_tracker.py`
- Modify: `tests/conftest.py`
- Create: `tests/test_span_tracker.py`

- [ ] **Step 1.1: 新增 isolate_hook_state fixture 到 conftest.py**

在 `tests/conftest.py` 末尾追加：

```python
@pytest.fixture
def isolate_hook_state(tmp_path, monkeypatch):
    """Give each test its own hook state dir (CLAUDE_PLUGIN_DATA).
    Also creates state.json so hooks don't crash on missing file.
    """
    hook_state = tmp_path / "hook-state"
    hook_state.mkdir()
    (hook_state / "state.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(hook_state))
    return hook_state
```

- [ ] **Step 1.2: 编写失败测试**

Create `tests/test_span_tracker.py`:

```python
from __future__ import annotations
import json
from pathlib import Path
import pytest
from scripts.span_tracker import SpanTracker, is_read_only_tool


@pytest.fixture
def tracker(tmp_path):
    hook_state = tmp_path / "hook-state"
    hook_state.mkdir()
    (hook_state / "state.json").write_text("{}", encoding="utf-8")
    return SpanTracker(state_root=tmp_path, hook_state_root=hook_state)


# ── is_read_only_tool ─────────────────────────────────────────────────────────

def test_is_read_only_known_names():
    for name in ("Read", "Glob", "Grep", "WebFetch", "WebSearch", "ToolSearch"):
        assert is_read_only_tool(name) is True, name

def test_is_read_only_context7_prefix():
    assert is_read_only_tool("mcp__context7__query-docs") is True
    assert is_read_only_tool("mcp__context7__resolve-library-id") is True

def test_is_read_only_suffix_patterns():
    assert is_read_only_tool("mcp__lark_doc__get") is True
    assert is_read_only_tool("mcp__lark_base__list") is True
    assert is_read_only_tool("mcp__lark_drive__search") is True

def test_is_not_read_only_write_tools():
    assert is_read_only_tool("mcp__lark_doc__create") is False
    assert is_read_only_tool("mcp__lark_im__send") is False
    assert is_read_only_tool("Edit") is False
    assert is_read_only_tool("Write") is False
    assert is_read_only_tool("Bash") is False

def test_is_not_read_only_icc_exec():
    # icc_exec is excluded from span recording entirely — conservatively not read-only
    assert is_read_only_tool("emerge__icc_exec") is False


# ── open / close ──────────────────────────────────────────────────────────────

def test_open_writes_active_span_to_hook_state(tracker, tmp_path):
    hook_state = tmp_path / "hook-state"
    span = tracker.open_span("lark.read.get-doc", description="test")
    state = json.loads((hook_state / "state.json").read_text())
    assert state["active_span_id"] == span.span_id
    assert state["active_span_intent"] == "lark.read.get-doc"

def test_open_clears_action_buffer(tracker, tmp_path):
    hook_state = tmp_path / "hook-state"
    buf = hook_state / "active-span-actions.jsonl"
    buf.write_text("stale\n", encoding="utf-8")
    tracker.open_span("lark.read.get-doc")
    assert buf.read_text() == ""

def test_open_errors_when_span_already_active(tracker):
    tracker.open_span("lark.read.get-doc")
    with pytest.raises(RuntimeError, match="active span"):
        tracker.open_span("lark.read.other")

def test_close_writes_span_to_wal(tracker, tmp_path):
    span = tracker.open_span("lark.write.create-doc", args={"title": "T"})
    tracker.close_span(span, outcome="success", result_summary={"doc_id": "x"})
    wal = tmp_path / "span-wal" / "spans.jsonl"
    assert wal.exists()
    record = json.loads(wal.read_text().strip())
    assert record["intent_signature"] == "lark.write.create-doc"
    assert record["outcome"] == "success"
    assert record["result_summary"] == {"doc_id": "x"}

def test_close_clears_hook_state(tracker, tmp_path):
    hook_state = tmp_path / "hook-state"
    span = tracker.open_span("lark.read.get-doc")
    tracker.close_span(span, outcome="success")
    state = json.loads((hook_state / "state.json").read_text())
    assert "active_span_id" not in state
    assert "active_span_intent" not in state

def test_close_reads_actions_from_buffer(tracker, tmp_path):
    hook_state = tmp_path / "hook-state"
    span = tracker.open_span("lark.read.get-doc")
    buf = hook_state / "active-span-actions.jsonl"
    buf.write_text(
        json.dumps({"tool_name": "mcp__lark_doc__get", "args_hash": "abc",
                    "has_side_effects": False, "ts_ms": 1}) + "\n",
        encoding="utf-8",
    )
    tracker.close_span(span, outcome="success")
    wal = tmp_path / "span-wal" / "spans.jsonl"
    record = json.loads(wal.read_text().strip())
    assert len(record["actions"]) == 1
    assert record["actions"][0]["tool_name"] == "mcp__lark_doc__get"
    assert record["is_read_only"] is True

def test_close_is_not_read_only_when_any_side_effect(tracker, tmp_path):
    hook_state = tmp_path / "hook-state"
    span = tracker.open_span("lark.write.create-doc")
    buf = hook_state / "active-span-actions.jsonl"
    buf.write_text(
        json.dumps({"tool_name": "mcp__lark_doc__create", "args_hash": "abc",
                    "has_side_effects": True, "ts_ms": 1}) + "\n",
        encoding="utf-8",
    )
    tracker.close_span(span, outcome="success")
    record = json.loads((tmp_path / "span-wal" / "spans.jsonl").read_text().strip())
    assert record["is_read_only"] is False


# ── policy lifecycle ──────────────────────────────────────────────────────────

def test_policy_starts_explore(tracker):
    assert tracker.get_policy_status("lark.read.get-doc") == "explore"

def test_policy_reaches_canary(tracker, monkeypatch):
    monkeypatch.setattr("scripts.span_tracker.PROMOTE_MIN_ATTEMPTS", 3)
    monkeypatch.setattr("scripts.span_tracker.PROMOTE_MIN_SUCCESS_RATE", 0.9)
    monkeypatch.setattr("scripts.span_tracker.PROMOTE_MAX_HUMAN_FIX_RATE", 0.1)
    for _ in range(3):
        s = tracker.open_span("lark.read.get-doc")
        tracker.close_span(s, outcome="success")
    assert tracker.get_policy_status("lark.read.get-doc") == "canary"

def test_policy_reaches_stable(tracker, monkeypatch):
    monkeypatch.setattr("scripts.span_tracker.PROMOTE_MIN_ATTEMPTS", 2)
    monkeypatch.setattr("scripts.span_tracker.PROMOTE_MIN_SUCCESS_RATE", 0.8)
    monkeypatch.setattr("scripts.span_tracker.PROMOTE_MAX_HUMAN_FIX_RATE", 0.2)
    monkeypatch.setattr("scripts.span_tracker.STABLE_MIN_ATTEMPTS", 4)
    monkeypatch.setattr("scripts.span_tracker.STABLE_MIN_SUCCESS_RATE", 0.8)
    for _ in range(4):
        s = tracker.open_span("lark.read.get-doc")
        tracker.close_span(s, outcome="success")
    assert tracker.get_policy_status("lark.read.get-doc") == "stable"

def test_policy_rollback_on_consecutive_failures(tracker, monkeypatch):
    monkeypatch.setattr("scripts.span_tracker.ROLLBACK_CONSECUTIVE_FAILURES", 2)
    for _ in range(2):
        s = tracker.open_span("lark.read.get-doc")
        tracker.close_span(s, outcome="failure")
    assert tracker.get_policy_status("lark.read.get-doc") == "rollback"

def test_latest_successful_span_returns_most_recent(tracker):
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

- [ ] **Step 1.3: 运行确认失败**

```bash
python -m pytest tests/test_span_tracker.py -q 2>&1 | head -5
```

Expected: `ModuleNotFoundError: No module named 'scripts.span_tracker'`

- [ ] **Step 1.4: 实现 SpanTracker**

Create `scripts/span_tracker.py`:

```python
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path

from scripts.policy_config import (
    PROMOTE_MAX_HUMAN_FIX_RATE,
    PROMOTE_MIN_ATTEMPTS,
    PROMOTE_MIN_SUCCESS_RATE,
    ROLLBACK_CONSECUTIVE_FAILURES,
    STABLE_MIN_ATTEMPTS,
    STABLE_MIN_SUCCESS_RATE,
    WINDOW_SIZE,
)

# ── read-only tool classification ─────────────────────────────────────────────
# Conservative: unknown tools default to has_side_effects=True.
# icc_exec is intentionally absent — it is excluded from span recording entirely.

_READ_ONLY_TOOL_NAMES = frozenset({
    "Read", "Glob", "Grep", "WebFetch", "WebSearch", "ToolSearch",
})
_READ_ONLY_TOOL_PREFIXES = ("mcp__context7__",)
_READ_ONLY_TOOL_SUFFIXES = (
    "__get", "__list", "__search", "__query", "__read", "__resolve",
)


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


# ── data classes ──────────────────────────────────────────────────────────────

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


# ── SpanTracker ───────────────────────────────────────────────────────────────

class SpanTracker:
    """Manages intent span lifecycle: open → [actions] → close → WAL → candidates → policy."""

    def __init__(self, state_root: Path, hook_state_root: Path) -> None:
        self._state_root = state_root
        self._hook_state_root = hook_state_root
        (state_root / "span-wal").mkdir(parents=True, exist_ok=True)

    # ── paths ──────────────────────────────────────────────────────────────

    def _wal_path(self) -> Path:
        return self._state_root / "span-wal" / "spans.jsonl"

    def _candidates_path(self) -> Path:
        return self._state_root / "span-candidates.json"

    def _buffer_path(self) -> Path:
        return self._hook_state_root / "active-span-actions.jsonl"

    def _state_path(self) -> Path:
        return self._hook_state_root / "state.json"

    # ── helpers ────────────────────────────────────────────────────────────

    def _atomic_write(self, path: Path, data: dict) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def _load_state(self) -> dict:
        p = self._state_path()
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except Exception:
            return {}

    def _load_candidates(self) -> dict:
        p = self._candidates_path()
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"spans": {}}
        except Exception:
            return {"spans": {}}

    # ── open ───────────────────────────────────────────────────────────────

    def open_span(
        self,
        intent_signature: str,
        description: str = "",
        args: dict | None = None,
        source: str = "manual",
        skill_name: str | None = None,
    ) -> SpanRecord:
        state = self._load_state()
        if state.get("active_span_id"):
            raise RuntimeError(
                f"active span already open: {state['active_span_id']} "
                f"({state.get('active_span_intent', '?')}). "
                "Call icc_span_close before opening a new span."
            )
        span = SpanRecord(
            span_id=str(uuid.uuid4()),
            intent_signature=intent_signature,
            description=description,
            source=source,
            skill_name=skill_name,
            opened_at_ms=int(time.time() * 1000),
            args=args or {},
        )
        state["active_span_id"] = span.span_id
        state["active_span_intent"] = intent_signature
        self._atomic_write(self._state_path(), state)
        self._buffer_path().write_text("", encoding="utf-8")
        return span

    # ── close ──────────────────────────────────────────────────────────────

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
        span.is_read_only = all(not a.has_side_effects for a in actions)

        # Persist to WAL
        with self._wal_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(span.to_dict(), ensure_ascii=False) + "\n")

        # Update candidates
        self._update_candidates(span)

        # Clear hook state
        state = self._load_state()
        state.pop("active_span_id", None)
        state.pop("active_span_intent", None)
        self._atomic_write(self._state_path(), state)
        buf.unlink(missing_ok=True)

        return span

    # ── candidates / policy ────────────────────────────────────────────────

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
            "skeleton_generated": False,
        })
        is_success = span.outcome == "success"
        entry["attempts"] += 1
        if is_success:
            entry["successes"] += 1
        entry["consecutive_failures"] = (
            0 if is_success else int(entry.get("consecutive_failures", 0)) + 1
        )
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
        Span policy intentionally omits verify_rate — spans have no verify step.
        """
        entry = self._load_candidates()["spans"].get(intent_signature, {})
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

    def mark_skeleton_generated(self, intent_signature: str) -> None:
        """Record that a skeleton has been generated for this intent."""
        candidates = self._load_candidates()
        if intent_signature in candidates["spans"]:
            candidates["spans"][intent_signature]["skeleton_generated"] = True
            self._atomic_write(self._candidates_path(), candidates)

    def skeleton_already_generated(self, intent_signature: str) -> bool:
        return bool(
            self._load_candidates()["spans"]
            .get(intent_signature, {})
            .get("skeleton_generated", False)
        )

    def latest_successful_span(self, intent_signature: str) -> dict | None:
        """Return the most recent successful span from WAL."""
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

- [ ] **Step 1.5: 运行测试确认通过**

```bash
python -m pytest tests/test_span_tracker.py -q
```

Expected: all pass.

- [ ] **Step 1.6: Commit**

```bash
git add scripts/span_tracker.py tests/test_span_tracker.py tests/conftest.py
git commit -m "feat: add SpanTracker — intent span WAL, candidates, policy lifecycle"
```

---

## Task 2: icc_exec Auto-Crystallize at synthesis_ready

**Files:**
- Modify: `scripts/emerge_daemon.py`
- Modify: `tests/test_mcp_tools_integration.py`

- [ ] **Step 2.1: 编写失败测试**

在 `tests/test_mcp_tools_integration.py` 末尾追加：

```python
# ── auto-crystallize tests ────────────────────────────────────────────────────

def _drive_exec_to_synthesis_ready(daemon, intent_sig: str, n: int = 21) -> None:
    """Run icc_exec enough times to reach synthesis_ready (canary threshold)."""
    for _ in range(n):
        daemon.call_tool("icc_exec", {
            "intent_signature": intent_sig,
            "code": "__result = [{'val': 1}]",
            "result_var": "__result",
        })


def test_auto_crystallize_creates_pipeline_at_synthesis_ready(tmp_path, monkeypatch):
    import json
    from scripts.emerge_daemon import EmergeDaemon
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path / "state"))
    connector_root = tmp_path / "connectors"
    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(connector_root))
    daemon = EmergeDaemon()
    _drive_exec_to_synthesis_ready(daemon, "mock.read.auto-crystallize-test")
    py_path = connector_root / "mock" / "pipelines" / "read" / "auto-crystallize-test.py"
    yaml_path = connector_root / "mock" / "pipelines" / "read" / "auto-crystallize-test.yaml"
    assert py_path.exists(), "auto-crystallize should have created .py"
    assert yaml_path.exists(), "auto-crystallize should have created .yaml"


def test_auto_crystallize_does_not_overwrite_existing(tmp_path, monkeypatch):
    from scripts.emerge_daemon import EmergeDaemon
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path / "state"))
    connector_root = tmp_path / "connectors"
    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(connector_root))
    # Pre-create the pipeline file
    py_dir = connector_root / "mock" / "pipelines" / "read"
    py_dir.mkdir(parents=True)
    existing = py_dir / "auto-crystallize-test.py"
    existing.write_text("# human-authored\n", encoding="utf-8")
    daemon = EmergeDaemon()
    _drive_exec_to_synthesis_ready(daemon, "mock.read.auto-crystallize-test")
    assert existing.read_text() == "# human-authored\n", "must not overwrite existing pipeline"
```

- [ ] **Step 2.2: 运行确认失败**

```bash
python -m pytest tests/test_mcp_tools_integration.py -k "auto_crystallize" -q 2>&1 | head -10
```

Expected: FAIL — pipeline files not created.

- [ ] **Step 2.3: 实现 _auto_crystallize 方法**

在 `scripts/emerge_daemon.py` 中，在 `_crystallize` 方法之后添加：

```python
    def _auto_crystallize(
        self,
        *,
        intent_signature: str,
        connector: str,
        pipeline_name: str,
        mode: str,
        target_profile: str = "default",
    ) -> None:
        """Auto-crystallize icc_exec WAL at synthesis_ready.

        Silently skips if pipeline file already exists (human-authored wins).
        Silently skips if no synthesizable WAL entry found.
        Never raises — failures are swallowed to avoid disrupting policy bookkeeping.
        """
        from scripts.pipeline_engine import _USER_CONNECTOR_ROOT
        try:
            env_root_str = os.environ.get("EMERGE_CONNECTOR_ROOT", "").strip()
            target_root = Path(env_root_str).expanduser() if env_root_str else _USER_CONNECTOR_ROOT
            pipeline_dir = target_root / connector / "pipelines" / mode
            py_path = pipeline_dir / f"{pipeline_name}.py"
            if py_path.exists():
                return  # never overwrite existing file
            self._crystallize(
                intent_signature=intent_signature,
                connector=connector,
                pipeline_name=pipeline_name,
                mode=mode,
                target_profile=target_profile,
            )
        except Exception:
            pass  # auto-crystallize is best-effort
```

- [ ] **Step 2.4: 在 _update_pipeline_registry 中调用 _auto_crystallize**

找到 `_update_pipeline_registry` 中设置 `pipeline["synthesis_ready"] = True` 的代码块（约第1488行）：

```python
                    if self._has_synthesizable_wal_entry(intent_sig, entry.get("target_profile", "default")):
                        pipeline["synthesis_ready"] = True
                        try:
                            self._sink.emit(...)
                        except Exception:
                            pass
```

在 `self._sink.emit(...)` 之后添加：

```python
                        # Auto-crystallize: derive connector/mode/name from intent_signature
                        try:
                            _parts = intent_sig.split(".", 2)
                            if len(_parts) == 3:
                                _conn, _mode, _name = _parts
                                self._auto_crystallize(
                                    intent_signature=intent_sig,
                                    connector=_conn,
                                    pipeline_name=_name,
                                    mode=_mode,
                                    target_profile=entry.get("target_profile", "default"),
                                )
                        except Exception:
                            pass
```

- [ ] **Step 2.5: 运行测试确认通过**

```bash
python -m pytest tests/test_mcp_tools_integration.py -k "auto_crystallize" -q
```

Expected: both tests pass.

- [ ] **Step 2.6: 运行完整测试套件确认无回归**

```bash
python -m pytest tests -q
```

Expected: all existing tests still pass.

- [ ] **Step 2.7: Commit**

```bash
git add scripts/emerge_daemon.py tests/test_mcp_tools_integration.py
git commit -m "feat: auto-crystallize icc_exec pipeline at synthesis_ready (no-overwrite)"
```

---

## Task 3: SessionStart Hook — 清除 Stale Active Span

**Files:**
- Modify: `hooks/session_start.py`
- Modify: `tests/test_hook_scripts_output.py`

- [ ] **Step 3.1: 编写失败测试**

在 `tests/test_hook_scripts_output.py` 末尾追加：

```python
def test_session_start_clears_stale_active_span(tmp_path, monkeypatch):
    """SessionStart must clear active_span_id left by a crashed previous session."""
    import json, subprocess, sys
    hook_state = tmp_path / "hook-state"
    hook_state.mkdir()
    stale_state = {
        "active_span_id": "stale-uuid",
        "active_span_intent": "lark.read.get-doc",
        "goal": "",
        "goal_source": "unset",
        "deltas": [],
    }
    (hook_state / "state.json").write_text(json.dumps(stale_state), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(hook_state))

    result = subprocess.run(
        [sys.executable, "hooks/session_start.py"],
        input=json.dumps({}),
        capture_output=True, text=True,
        cwd=str(Path(__file__).parents[1]),
    )
    assert result.returncode == 0
    state = json.loads((hook_state / "state.json").read_text())
    assert "active_span_id" not in state
    assert "active_span_intent" not in state
```

- [ ] **Step 3.2: 运行确认失败**

```bash
python -m pytest tests/test_hook_scripts_output.py::test_session_start_clears_stale_active_span -q 2>&1 | head -5
```

Expected: FAIL — stale fields still present.

- [ ] **Step 3.3: 修改 session_start.py**

在 `hooks/session_start.py` 的 `save_tracker(state_path, tracker)` 行之前插入：

```python
    # Clear any stale active span left by a crashed previous session.
    # If active_span_id lingers, PostToolUse would incorrectly attribute
    # new session tool calls to a dead span's action buffer.
    try:
        _raw_state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        _changed = False
        for _k in ("active_span_id", "active_span_intent"):
            if _k in _raw_state:
                del _raw_state[_k]
                _changed = True
        if _changed:
            import tempfile as _tempfile, os as _os
            _tmp = state_path.with_suffix(".tmp")
            _tmp.write_text(json.dumps(_raw_state, ensure_ascii=False), encoding="utf-8")
            _os.replace(_tmp, state_path)
    except Exception:
        pass
```

- [ ] **Step 3.4: 运行确认通过**

```bash
python -m pytest tests/test_hook_scripts_output.py -q
```

Expected: all pass.

- [ ] **Step 3.5: Commit**

```bash
git add hooks/session_start.py tests/test_hook_scripts_output.py
git commit -m "feat: SessionStart clears stale active_span_id to prevent cross-session contamination"
```

---

## Task 4: PostToolUse Hook — Action Recording

**Files:**
- Modify: `hooks/post_tool_use.py`
- Modify: `tests/test_hook_scripts_output.py`

- [ ] **Step 4.1: 编写失败测试**

```python
def _run_post_hook(payload: dict, hook_state: Path) -> dict:
    import json, subprocess, sys
    from pathlib import Path
    import os
    env = {**os.environ, "CLAUDE_PLUGIN_DATA": str(hook_state)}
    result = subprocess.run(
        [sys.executable, "hooks/post_tool_use.py"],
        input=json.dumps(payload),
        capture_output=True, text=True,
        cwd=str(Path(__file__).parents[1]),
        env=env,
    )
    return json.loads(result.stdout) if result.stdout.strip() else {}


def test_post_tool_use_records_action_when_span_active(tmp_path):
    import json
    hook_state = tmp_path / "hook-state"
    hook_state.mkdir()
    state = {"active_span_id": "span-123", "active_span_intent": "lark.read.get-doc",
             "goal": "", "goal_source": "unset", "deltas": []}
    (hook_state / "state.json").write_text(json.dumps(state), encoding="utf-8")
    _run_post_hook({"tool_name": "mcp__lark_doc__get",
                    "tool_result": {"content": [{"type": "text", "text": "{}"}]}},
                   hook_state)
    buf = hook_state / "active-span-actions.jsonl"
    assert buf.exists()
    rec = json.loads(buf.read_text().strip())
    assert rec["tool_name"] == "mcp__lark_doc__get"
    assert rec["has_side_effects"] is False  # __get is read-only


def test_post_tool_use_excludes_icc_exec_from_span(tmp_path):
    import json
    hook_state = tmp_path / "hook-state"
    hook_state.mkdir()
    state = {"active_span_id": "span-123", "active_span_intent": "lark.read.get-doc",
             "goal": "", "goal_source": "unset", "deltas": []}
    (hook_state / "state.json").write_text(json.dumps(state), encoding="utf-8")
    _run_post_hook({"tool_name": "emerge__icc_exec",
                    "tool_result": {"content": [{"type": "text", "text": "{}"}]}},
                   hook_state)
    buf = hook_state / "active-span-actions.jsonl"
    assert not buf.exists() or buf.read_text().strip() == "", \
        "icc_exec must not be recorded as a span action"


def test_post_tool_use_no_recording_without_active_span(tmp_path):
    hook_state = tmp_path / "hook-state"
    hook_state.mkdir()
    (hook_state / "state.json").write_text("{}", encoding="utf-8")
    _run_post_hook({"tool_name": "mcp__lark_doc__get",
                    "tool_result": {"content": [{"type": "text", "text": "{}"}]}},
                   hook_state)
    buf = hook_state / "active-span-actions.jsonl"
    assert not buf.exists() or buf.read_text().strip() == ""
```

- [ ] **Step 4.2: 运行确认失败**

```bash
python -m pytest tests/test_hook_scripts_output.py -k "span" -q 2>&1 | head -10
```

Expected: FAIL.

- [ ] **Step 4.3: 修改 post_tool_use.py**

在 `hooks/post_tool_use.py` 的现有 `import` 区域末尾添加：

```python
from scripts.span_tracker import is_read_only_tool  # noqa: E402
```

在 `save_tracker(state_path, tracker)` 之前插入 span action recording 块：

```python
    # ── span action recording ──────────────────────────────────────────────
    # Skip icc_exec entirely: its Python code paths are captured in ExecSession WAL.
    # A span skeleton built from icc_exec tool names would be useless.
    _is_icc_exec = tool_name.endswith("__icc_exec")
    if not _is_icc_exec and tool_name:
        try:
            _raw_state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
            _active_span_id = str(_raw_state.get("active_span_id", "") or "")
        except Exception:
            _active_span_id = ""
        if _active_span_id:
            import hashlib as _hashlib
            import time as _time
            _has_se = not is_read_only_tool(tool_name)
            _args_raw = json.dumps(payload.get("tool_input", {}), sort_keys=True, ensure_ascii=True)
            _args_hash = _hashlib.sha256(_args_raw.encode()).hexdigest()[:16]
            _action = {
                "tool_name": tool_name,
                "args_hash": _args_hash,
                "has_side_effects": _has_se,
                "ts_ms": int(_time.time() * 1000),
            }
            _buf = state_root / "active-span-actions.jsonl"
            try:
                with _buf.open("a", encoding="utf-8") as _f:
                    _f.write(json.dumps(_action, ensure_ascii=True) + "\n")
            except Exception:
                pass
    # ── end span action recording ──────────────────────────────────────────
```

- [ ] **Step 4.4: 运行确认通过**

```bash
python -m pytest tests/test_hook_scripts_output.py -q
```

Expected: all pass.

- [ ] **Step 4.5: Commit**

```bash
git add hooks/post_tool_use.py tests/test_hook_scripts_output.py
git commit -m "feat: PostToolUse records MCP tool calls into active span buffer (excludes icc_exec)"
```

---

## Task 5: PreToolUse Hook — 校验 Span 工具

**Files:**
- Modify: `hooks/pre_tool_use.py`
- Modify: `tests/test_hook_scripts_output.py`

- [ ] **Step 5.1: 编写失败测试**

```python
def _run_pre_hook(payload: dict) -> dict:
    import json, subprocess, sys, os
    from pathlib import Path
    result = subprocess.run(
        [sys.executable, "hooks/pre_tool_use.py"],
        input=json.dumps(payload),
        capture_output=True, text=True,
        cwd=str(Path(__file__).parents[1]),
    )
    return json.loads(result.stdout) if result.stdout.strip() else {}


def test_pre_hook_blocks_span_open_missing_intent():
    out = _run_pre_hook({"tool_name": "emerge__icc_span_open", "tool_input": {}})
    assert out.get("decision") == "block"

def test_pre_hook_blocks_span_open_invalid_intent():
    out = _run_pre_hook({"tool_name": "emerge__icc_span_open",
                         "tool_input": {"intent_signature": "no_mode_segment"}})
    assert out.get("decision") == "block"

def test_pre_hook_allows_valid_span_open():
    out = _run_pre_hook({"tool_name": "emerge__icc_span_open",
                         "tool_input": {"intent_signature": "lark.read.get-doc"}})
    assert out.get("decision") != "block"

def test_pre_hook_blocks_span_close_bad_outcome():
    out = _run_pre_hook({"tool_name": "emerge__icc_span_close",
                         "tool_input": {"outcome": "done"}})
    assert out.get("decision") == "block"

def test_pre_hook_allows_valid_span_close():
    out = _run_pre_hook({"tool_name": "emerge__icc_span_close",
                         "tool_input": {"outcome": "success"}})
    assert out.get("decision") != "block"

def test_pre_hook_blocks_span_approve_missing_intent():
    out = _run_pre_hook({"tool_name": "emerge__icc_span_approve", "tool_input": {}})
    assert out.get("decision") == "block"

def test_pre_hook_allows_valid_span_approve():
    out = _run_pre_hook({"tool_name": "emerge__icc_span_approve",
                         "tool_input": {"intent_signature": "lark.write.create-doc"}})
    assert out.get("decision") != "block"
```

- [ ] **Step 5.2: 运行确认失败**

```bash
python -m pytest tests/test_hook_scripts_output.py -k "span_open or span_close or span_approve" -q 2>&1 | head -5
```

Expected: FAIL.

- [ ] **Step 5.3: 在 pre_tool_use.py 中添加校验块**

在现有 `if tool_name.endswith("__icc_crystallize"):` 块之后，`if error_msg:` 之前插入：

```python
    if tool_name.endswith("__icc_span_open"):
        import re as _re
        intent_signature = str(arguments.get("intent_signature", "")).strip()
        if not intent_signature:
            error_msg = (
                "icc_span_open: 'intent_signature' is required "
                "(e.g. 'lark.read.get-doc'). "
                "Format: <connector>.(read|write).<name>"
            )
        elif not _re.compile(r'^[a-z][a-z0-9_-]*\.(read|write)\.[a-z][a-z0-9_./-]*$').match(intent_signature):
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

- [ ] **Step 5.4: 运行确认通过**

```bash
python -m pytest tests/test_hook_scripts_output.py -q
```

Expected: all pass.

- [ ] **Step 5.5: Commit**

```bash
git add hooks/pre_tool_use.py tests/test_hook_scripts_output.py
git commit -m "feat: PreToolUse validates icc_span_open / icc_span_close / icc_span_approve"
```

---

## Task 6: Daemon — icc_span_open（含 Bridge）

**Files:**
- Modify: `scripts/emerge_daemon.py`
- Modify: `tests/test_mcp_tools_integration.py`

- [ ] **Step 6.1: 编写失败测试**

```python
# ── icc_span_open ─────────────────────────────────────────────────────────────

def _make_span_daemon(tmp_path, monkeypatch):
    from scripts.emerge_daemon import EmergeDaemon
    state = tmp_path / "state"
    hook_state = tmp_path / "hook-state"
    hook_state.mkdir(parents=True)
    (hook_state / "state.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(state))
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(hook_state))
    return EmergeDaemon(), hook_state


def test_span_open_returns_span_id(tmp_path, monkeypatch):
    import json
    daemon, _ = _make_span_daemon(tmp_path, monkeypatch)
    result = daemon.call_tool("icc_span_open", {"intent_signature": "lark.read.get-doc"})
    assert result.get("isError") is not True
    body = json.loads(result["content"][0]["text"])
    assert "span_id" in body
    assert body["policy_status"] == "explore"
    assert body.get("bridge") is not True


def test_span_open_writes_active_span_to_hook_state(tmp_path, monkeypatch):
    import json
    daemon, hook_state = _make_span_daemon(tmp_path, monkeypatch)
    daemon.call_tool("icc_span_open", {"intent_signature": "lark.read.get-doc"})
    state = json.loads((hook_state / "state.json").read_text())
    assert "active_span_id" in state
    assert state["active_span_intent"] == "lark.read.get-doc"


def test_span_open_errors_when_span_already_active(tmp_path, monkeypatch):
    import json
    daemon, hook_state = _make_span_daemon(tmp_path, monkeypatch)
    daemon.call_tool("icc_span_open", {"intent_signature": "lark.read.get-doc"})
    result = daemon.call_tool("icc_span_open", {"intent_signature": "lark.read.other"})
    assert result.get("isError") is True


def test_span_open_errors_on_missing_intent(tmp_path, monkeypatch):
    daemon, _ = _make_span_daemon(tmp_path, monkeypatch)
    result = daemon.call_tool("icc_span_open", {})
    assert result.get("isError") is True
```

- [ ] **Step 6.2: 运行确认失败**

```bash
python -m pytest tests/test_mcp_tools_integration.py -k "span_open" -q 2>&1 | head -5
```

Expected: FAIL.

- [ ] **Step 6.3: 在 EmergeDaemon.__init__ 中初始化 SpanTracker**

在 `__init__` 的 `self._goal_control` 初始化之后添加：

```python
        from scripts.span_tracker import SpanTracker
        _hook_state_root = Path(default_hook_state_root())
        self._span_tracker = SpanTracker(
            state_root=self._state_root,
            hook_state_root=_hook_state_root,
        )
        self._open_spans: dict[str, Any] = {}  # span_id → SpanRecord; in-process cache
```

- [ ] **Step 6.4: 添加 icc_span_open handler**

在 `call_tool` 方法中，在 `if name == "icc_exec":` 之前插入：

```python
        if name == "icc_span_open":
            intent_signature = str(arguments.get("intent_signature", "")).strip()
            if not intent_signature:
                return self._tool_error("icc_span_open: 'intent_signature' is required")
            # Bridge check: stable policy AND pipeline file exists
            policy_status = self._span_tracker.get_policy_status(intent_signature)
            if policy_status == "stable":
                parts = intent_signature.split(".", 2)
                if len(parts) == 3:
                    connector, mode, pipeline_name = parts
                    pipeline_args = {**arguments, "connector": connector, "pipeline": pipeline_name}
                    try:
                        _rr = self._get_runner_router()
                        _client = _rr.find_client(arguments) if _rr else None
                        if _client is not None:
                            bridge_result = self._run_pipeline_remotely(mode, pipeline_args, _client)
                            _exec_path = "remote"
                        elif mode == "write":
                            bridge_result = self.pipeline.run_write(pipeline_args)
                            _exec_path = "local"
                        else:
                            bridge_result = self.pipeline.run_read(pipeline_args)
                            _exec_path = "local"
                        bridge_result["bridge_promoted"] = True
                        try:
                            self._record_pipeline_event(
                                tool_name="icc_span_open",
                                arguments=pipeline_args,
                                result=bridge_result,
                                is_error=False,
                                execution_path=_exec_path,
                            )
                        except Exception:
                            pass
                        try:
                            self._sink.emit("span.bridge.promoted", {"intent_signature": intent_signature})
                        except Exception:
                            pass
                        return self._tool_ok_json({
                            "bridge": True,
                            "bridge_type": "result",
                            "intent_signature": intent_signature,
                            "result": bridge_result,
                        })
                    except Exception:
                        pass  # PipelineMissingError or any failure → fall through to explore
            # No bridge: open a new span
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
```

- [ ] **Step 6.5: 添加 icc_span_open 到 tool schema**

在 `_list_tools` 的工具数组中添加：

```python
                        {
                            "name": "icc_span_open",
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
                        },
```

- [ ] **Step 6.6: 运行确认通过**

```bash
python -m pytest tests/test_mcp_tools_integration.py -k "span_open" -q
```

Expected: all pass.

- [ ] **Step 6.7: Commit**

```bash
git add scripts/emerge_daemon.py tests/test_mcp_tools_integration.py
git commit -m "feat: add icc_span_open with span bridge (PipelineEngine, no recipe)"
```

---

## Task 7: Daemon — icc_span_close（含 Skeleton 生成）

**Files:**
- Modify: `scripts/emerge_daemon.py`
- Modify: `tests/test_mcp_tools_integration.py`

- [ ] **Step 7.1: 编写失败测试**

```python
def test_span_close_writes_to_wal(tmp_path, monkeypatch):
    import json
    daemon, hook_state = _make_span_daemon(tmp_path, monkeypatch)
    r = daemon.call_tool("icc_span_open", {"intent_signature": "lark.read.get-doc"})
    span_id = json.loads(r["content"][0]["text"])["span_id"]
    result = daemon.call_tool("icc_span_close", {"span_id": span_id, "outcome": "success"})
    assert result.get("isError") is not True
    wal = tmp_path / "state" / "span-wal" / "spans.jsonl"
    assert wal.exists()
    record = json.loads(wal.read_text().strip())
    assert record["outcome"] == "success"
    assert record["intent_signature"] == "lark.read.get-doc"


def test_span_close_returns_policy_status(tmp_path, monkeypatch):
    import json
    daemon, _ = _make_span_daemon(tmp_path, monkeypatch)
    r = daemon.call_tool("icc_span_open", {"intent_signature": "lark.read.get-doc"})
    sid = json.loads(r["content"][0]["text"])["span_id"]
    body = json.loads(daemon.call_tool("icc_span_close", {"span_id": sid, "outcome": "success"})["content"][0]["text"])
    assert "policy_status" in body
    assert body["policy_status"] == "explore"


def test_span_close_errors_on_bad_outcome(tmp_path, monkeypatch):
    daemon, _ = _make_span_daemon(tmp_path, monkeypatch)
    result = daemon.call_tool("icc_span_close", {"outcome": "done"})
    assert result.get("isError") is True


def test_span_close_generates_skeleton_at_stable(tmp_path, monkeypatch):
    import json
    daemon, hook_state = _make_span_daemon(tmp_path, monkeypatch)
    connector_root = tmp_path / "connectors"
    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(connector_root))
    # Drive to stable using monkeypatched thresholds
    import scripts.span_tracker as st
    monkeypatch.setattr(st, "PROMOTE_MIN_ATTEMPTS", 2)
    monkeypatch.setattr(st, "PROMOTE_MIN_SUCCESS_RATE", 0.5)
    monkeypatch.setattr(st, "PROMOTE_MAX_HUMAN_FIX_RATE", 1.0)
    monkeypatch.setattr(st, "STABLE_MIN_ATTEMPTS", 4)
    monkeypatch.setattr(st, "STABLE_MIN_SUCCESS_RATE", 0.5)
    # Re-create tracker with patched constants
    from scripts.span_tracker import SpanTracker
    from pathlib import Path
    daemon._span_tracker = SpanTracker(
        state_root=tmp_path / "state",
        hook_state_root=hook_state,
    )
    for _ in range(5):
        r = daemon.call_tool("icc_span_open", {"intent_signature": "lark.write.create-doc"})
        body = json.loads(r["content"][0]["text"])
        if body.get("bridge"):
            break
        sid = body["span_id"]
        buf = hook_state / "active-span-actions.jsonl"
        buf.write_text(
            json.dumps({"tool_name": "mcp__lark_doc__create", "args_hash": "x",
                        "has_side_effects": True, "ts_ms": 1}) + "\n"
        )
        daemon.call_tool("icc_span_close", {"span_id": sid, "outcome": "success"})
    pending = connector_root / "lark" / "pipelines" / "write" / "_pending" / "create-doc.py"
    assert pending.exists(), "skeleton must be generated at stable"
    assert "def run_write" in pending.read_text()
```

- [ ] **Step 7.2: 运行确认失败**

```bash
python -m pytest tests/test_mcp_tools_integration.py -k "span_close" -q 2>&1 | head -5
```

Expected: FAIL.

- [ ] **Step 7.3: 实现 skeleton 生成方法**

在 `scripts/emerge_daemon.py` 的 `_auto_crystallize` 方法之后添加：

```python
    def _generate_span_skeleton(
        self,
        *,
        intent_signature: str,
        span: dict,
        connector_root: "Path | None" = None,
    ) -> "Path | None":
        """Generate a Python skeleton for a stable write span.

        Writes to connectors/<connector>/pipelines/<mode>/_pending/<name>.py.
        Returns the path written, or None on failure.
        Silently skips if skeleton already exists.
        """
        import textwrap
        from scripts.pipeline_engine import _USER_CONNECTOR_ROOT
        try:
            parts = intent_signature.split(".", 2)
            if len(parts) != 3:
                return None
            connector, mode, pipeline_name = parts
            env_root_str = os.environ.get("EMERGE_CONNECTOR_ROOT", "").strip()
            target_root = connector_root or (
                Path(env_root_str).expanduser() if env_root_str else _USER_CONNECTOR_ROOT
            )
            pending_dir = target_root / connector / "pipelines" / mode / "_pending"
            pending_dir.mkdir(parents=True, exist_ok=True)
            skeleton_path = pending_dir / f"{pipeline_name}.py"
            if skeleton_path.exists():
                return skeleton_path  # already generated

            actions = span.get("actions", [])
            is_read = mode == "read"
            func_name = "run_read" if is_read else "run_write"
            call_lines = []
            for a in actions:
                tool = a.get("tool_name", "unknown_tool")
                call_lines.append(
                    f"    # seq={a.get('seq', '?')}: {tool} was called here\n"
                    f"    raise NotImplementedError('implement: {tool} equivalent')"
                )
            if not call_lines:
                call_lines = ["    raise NotImplementedError('implement pipeline body')"]
            body = "\n".join(call_lines)

            if is_read:
                skeleton = textwrap.dedent(f"""\
                    # auto-generated from span: {intent_signature}
                    # Review and implement before calling icc_span_approve.

                    def run_read(metadata, args):
                    {body}
                        return []  # return list of row dicts

                    def verify_read(metadata, args, rows):
                        return {{"ok": isinstance(rows, list)}}
                """)
            else:
                skeleton = textwrap.dedent(f"""\
                    # auto-generated from span: {intent_signature}
                    # Review and implement before calling icc_span_approve.
                    # verify_write is REQUIRED by PipelineEngine.

                    def run_write(metadata, args):
                    {body}
                        return {{"ok": True}}

                    def verify_write(metadata, args, action_result):
                        raise NotImplementedError('implement verify_write')

                    def rollback(metadata, args, action_result):
                        pass  # optional
                """)

            fd, tmp = tempfile.mkstemp(prefix=".skeleton-", dir=str(pending_dir))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(skeleton)
                os.replace(tmp, skeleton_path)
            except Exception:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
            return skeleton_path
        except Exception:
            return None
```

- [ ] **Step 7.4: 添加 icc_span_close handler**

在 `icc_span_open` handler 之后插入：

```python
        if name == "icc_span_close":
            outcome = str(arguments.get("outcome", "")).strip()
            if outcome not in ("success", "failure", "aborted"):
                return self._tool_error(
                    f"icc_span_close: 'outcome' must be success/failure/aborted, got {outcome!r}"
                )
            span_id = str(arguments.get("span_id", "")).strip()
            result_summary = arguments.get("result_summary") or {}
            # Retrieve in-process span (may be absent after daemon restart)
            from scripts.span_tracker import SpanRecord
            span = self._open_spans.pop(span_id, None)
            if span is None:
                # Graceful fallback: reconstruct minimal span so WAL still gets a record
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
            # Auto-generate skeleton for stable spans (once only)
            if synthesis_ready and not self._span_tracker.skeleton_already_generated(closed.intent_signature):
                latest = self._span_tracker.latest_successful_span(closed.intent_signature)
                if latest:
                    generated = self._generate_span_skeleton(
                        intent_signature=closed.intent_signature,
                        span=latest,
                    )
                    if generated:
                        skeleton_path = str(generated)
                        self._span_tracker.mark_skeleton_generated(closed.intent_signature)
                        try:
                            self._sink.emit("span.skeleton_generated", {
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
                response["next_step"] = (
                    f"Review and complete {skeleton_path}, "
                    "then call icc_span_approve to activate the bridge."
                )
            return self._tool_ok_json(response)
```

- [ ] **Step 7.5: 添加 icc_span_close 到 tool schema**

```python
                        {
                            "name": "icc_span_close",
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
                        },
```

- [ ] **Step 7.6: 运行确认通过**

```bash
python -m pytest tests/test_mcp_tools_integration.py -k "span_close or span_open" -q
```

Expected: all pass.

- [ ] **Step 7.7: Commit**

```bash
git add scripts/emerge_daemon.py tests/test_mcp_tools_integration.py
git commit -m "feat: add icc_span_close with skeleton auto-generation at stable"
```

---

## Task 8: Daemon — icc_span_approve（激活 Bridge）

**Files:**
- Modify: `scripts/emerge_daemon.py`
- Modify: `tests/test_mcp_tools_integration.py`

- [ ] **Step 8.1: 编写失败测试**

```python
def test_span_approve_moves_pending_and_generates_yaml(tmp_path, monkeypatch):
    import json
    daemon, hook_state = _make_span_daemon(tmp_path, monkeypatch)
    connector_root = tmp_path / "connectors"
    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(connector_root))
    # Pre-create skeleton in _pending/
    pending_dir = connector_root / "lark" / "pipelines" / "write" / "_pending"
    pending_dir.mkdir(parents=True)
    skeleton = pending_dir / "create-doc.py"
    skeleton.write_text(
        "def run_write(metadata, args):\n    return {'ok': True}\n"
        "def verify_write(metadata, args, action_result):\n    return {'ok': True}\n",
        encoding="utf-8",
    )
    # Drive to stable so approve is allowed
    import scripts.span_tracker as st
    monkeypatch.setattr(st, "PROMOTE_MIN_ATTEMPTS", 2)
    monkeypatch.setattr(st, "PROMOTE_MIN_SUCCESS_RATE", 0.5)
    monkeypatch.setattr(st, "PROMOTE_MAX_HUMAN_FIX_RATE", 1.0)
    monkeypatch.setattr(st, "STABLE_MIN_ATTEMPTS", 4)
    monkeypatch.setattr(st, "STABLE_MIN_SUCCESS_RATE", 0.5)
    from scripts.span_tracker import SpanTracker
    daemon._span_tracker = SpanTracker(state_root=tmp_path / "state", hook_state_root=hook_state)
    for _ in range(5):
        s = daemon._span_tracker.open_span("lark.write.create-doc")
        daemon._open_spans[s.span_id] = s
        daemon._span_tracker.close_span(s, outcome="success")

    result = daemon.call_tool("icc_span_approve", {"intent_signature": "lark.write.create-doc"})
    assert result.get("isError") is not True
    body = json.loads(result["content"][0]["text"])
    assert body.get("approved") is True
    # .py moved to real dir
    real_py = connector_root / "lark" / "pipelines" / "write" / "create-doc.py"
    assert real_py.exists()
    assert not skeleton.exists()  # removed from _pending
    # .yaml generated alongside
    real_yaml = connector_root / "lark" / "pipelines" / "write" / "create-doc.yaml"
    assert real_yaml.exists()
    import yaml
    meta = yaml.safe_load(real_yaml.read_text())
    assert meta["intent_signature"] == "lark.write.create-doc"


def test_span_approve_errors_when_not_stable(tmp_path, monkeypatch):
    daemon, _ = _make_span_daemon(tmp_path, monkeypatch)
    result = daemon.call_tool("icc_span_approve", {"intent_signature": "lark.write.never-run"})
    assert result.get("isError") is True


def test_span_approve_errors_when_pending_missing(tmp_path, monkeypatch):
    import scripts.span_tracker as st
    monkeypatch.setattr(st, "PROMOTE_MIN_ATTEMPTS", 1)
    monkeypatch.setattr(st, "PROMOTE_MIN_SUCCESS_RATE", 0.0)
    monkeypatch.setattr(st, "PROMOTE_MAX_HUMAN_FIX_RATE", 1.0)
    monkeypatch.setattr(st, "STABLE_MIN_ATTEMPTS", 2)
    monkeypatch.setattr(st, "STABLE_MIN_SUCCESS_RATE", 0.0)
    daemon, hook_state = _make_span_daemon(tmp_path, monkeypatch)
    from scripts.span_tracker import SpanTracker
    daemon._span_tracker = SpanTracker(state_root=tmp_path / "state", hook_state_root=hook_state)
    for _ in range(3):
        s = daemon._span_tracker.open_span("lark.write.create-doc")
        daemon._open_spans[s.span_id] = s
        daemon._span_tracker.close_span(s, outcome="success")
    # No _pending file exists
    result = daemon.call_tool("icc_span_approve", {"intent_signature": "lark.write.create-doc"})
    assert result.get("isError") is True
    import json
    assert "_pending" in json.loads(result["content"][0]["text"]).get("message", "")
```

- [ ] **Step 8.2: 运行确认失败**

```bash
python -m pytest tests/test_mcp_tools_integration.py -k "span_approve" -q 2>&1 | head -5
```

Expected: FAIL.

- [ ] **Step 8.3: 添加 icc_span_approve handler**

```python
        if name == "icc_span_approve":
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
            from scripts.pipeline_engine import _USER_CONNECTOR_ROOT
            env_root_str = os.environ.get("EMERGE_CONNECTOR_ROOT", "").strip()
            target_root = Path(env_root_str).expanduser() if env_root_str else _USER_CONNECTOR_ROOT
            pending_py = target_root / connector / "pipelines" / mode / "_pending" / f"{pipeline_name}.py"
            if not pending_py.exists():
                return self._tool_error(
                    f"icc_span_approve: skeleton not found at {pending_py}. "
                    "Run icc_span_close to generate the skeleton first, "
                    "then implement it before approving."
                )
            # Move .py to real pipeline directory
            real_dir = target_root / connector / "pipelines" / mode
            real_dir.mkdir(parents=True, exist_ok=True)
            real_py = real_dir / f"{pipeline_name}.py"
            real_yaml = real_dir / f"{pipeline_name}.yaml"
            # Atomic move: write to temp in target dir, then replace
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
            # Generate minimal YAML metadata
            mode_step_key = "read_steps" if mode == "read" else "write_steps"
            mode_step_val = "run_read" if mode == "read" else "run_write"
            verify_step_val = "verify_read" if mode == "read" else "verify_write"
            yaml_data: dict[str, Any] = {
                "intent_signature": intent_signature,
                "rollback_or_stop_policy": "stop",
                mode_step_key: [mode_step_val],
                "verify_steps": [verify_step_val],
                "span_approved": True,
            }
            try:
                yaml_src = _IndentedSafeDumper.dump_yaml(yaml_data)
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
                self._sink.emit("span.approved", {"intent_signature": intent_signature})
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
```

- [ ] **Step 8.4: 添加 icc_span_approve 到 tool schema**

```python
                        {
                            "name": "icc_span_approve",
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
                        },
```

- [ ] **Step 8.5: 运行确认通过**

```bash
python -m pytest tests/test_mcp_tools_integration.py -k "span" -q
```

Expected: all span tests pass.

- [ ] **Step 8.6: Commit**

```bash
git add scripts/emerge_daemon.py tests/test_mcp_tools_integration.py
git commit -m "feat: add icc_span_approve — moves skeleton to pipeline dir, generates yaml, activates bridge"
```

---

## Task 9: 废弃 icc_read / icc_write + connector://spans Resource

**Files:**
- Modify: `scripts/emerge_daemon.py`
- Modify: `hooks/pre_tool_use.py`
- Modify: `tests/test_mcp_tools_integration.py`

- [ ] **Step 9.1: 编写废弃测试**

```python
def test_icc_read_returns_deprecated_error(tmp_path, monkeypatch):
    import json
    daemon, _ = _make_span_daemon(tmp_path, monkeypatch)
    result = daemon.call_tool("icc_read", {"connector": "mock", "pipeline": "layers"})
    assert result.get("isError") is True
    msg = json.loads(result["content"][0]["text"]).get("message", "")
    assert "icc_span_open" in msg


def test_icc_write_returns_deprecated_error(tmp_path, monkeypatch):
    import json
    daemon, _ = _make_span_daemon(tmp_path, monkeypatch)
    result = daemon.call_tool("icc_write", {"connector": "mock", "pipeline": "add-wall"})
    assert result.get("isError") is True
    msg = json.loads(result["content"][0]["text"]).get("message", "")
    assert "icc_span_open" in msg


def test_spans_resource_lists_connector_intents(tmp_path, monkeypatch):
    import json
    daemon, hook_state = _make_span_daemon(tmp_path, monkeypatch)
    from scripts.span_tracker import SpanTracker
    daemon._span_tracker = SpanTracker(state_root=tmp_path / "state", hook_state_root=hook_state)
    s = daemon._span_tracker.open_span("lark.read.get-doc")
    daemon._open_spans[s.span_id] = s
    daemon._span_tracker.close_span(s, outcome="success")
    resources = daemon.list_resources()
    uris = [r["uri"] for r in resources]
    assert "connector://lark/spans" in uris
```

- [ ] **Step 9.2: 运行确认失败**

```bash
python -m pytest tests/test_mcp_tools_integration.py -k "deprecated or spans_resource" -q 2>&1 | head -5
```

Expected: FAIL.

- [ ] **Step 9.3: 替换 icc_read / icc_write handlers**

在 `call_tool` 中，将现有 `if name == "icc_read":` 和 `if name == "icc_write":` 替换为：

```python
        if name == "icc_read":
            return self._tool_error(
                "icc_read is deprecated. "
                "Use icc_span_open(intent_signature='<connector>.read.<name>') instead. "
                "See connector://<name>/notes for existing intents."
            )
        if name == "icc_write":
            return self._tool_error(
                "icc_write is deprecated. "
                "Use icc_span_open(intent_signature='<connector>.write.<name>') instead."
            )
```

- [ ] **Step 9.4: 从 tool schema 移除 icc_read 和 icc_write**

在 `_list_tools` 中删除 `icc_read` 和 `icc_write` 的 schema 条目。

- [ ] **Step 9.5: 从 pre_tool_use.py 移除 icc_read / icc_write 校验**

删除 `if tool_name.endswith("__icc_read") or tool_name.endswith("__icc_write"):` 块。

- [ ] **Step 9.6: 添加 connector://spans resource**

在 `_list_resources` 中，在现有 connector notes 循环之后追加：

```python
        # connector://spans: per-connector span intent index
        for cname in sorted(connector_names):
            spans_uri = f"connector://{cname}/spans"
            already_noted.add(spans_uri)  # prevent duplication
            if any(
                rec.get("intent_signature", "").startswith(f"{cname}.")
                for rec in self._span_tracker._load_candidates().get("spans", {}).values()
            ):
                static.append({
                    "uri": spans_uri,
                    "name": f"{cname} span intents",
                    "mimeType": "application/json",
                    "description": (
                        f"JSON index of all flywheel-tracked span intents for {cname}, "
                        "with policy status and skeleton generation state."
                    ),
                })
```

In `_read_resource`, before the final raise, add:

```python
            if uri_path.endswith("/spans"):
                connector = uri_path.split("/")[0]
                candidates = self._span_tracker._load_candidates().get("spans", {})
                relevant = {
                    k: v for k, v in candidates.items()
                    if k.startswith(f"{connector}.")
                }
                return json.dumps(relevant, ensure_ascii=False)
```

- [ ] **Step 9.7: 运行测试**

```bash
python -m pytest tests/test_mcp_tools_integration.py -q
```

Expected: all pass (existing tests that called icc_read/icc_write directly need updating — change them to assert `isError=True` or remove them).

- [ ] **Step 9.8: Commit**

```bash
git add scripts/emerge_daemon.py hooks/pre_tool_use.py tests/test_mcp_tools_integration.py
git commit -m "feat: deprecate icc_read/icc_write, add connector://spans resource"
```

---

## Task 10: 文档更新

**Files:**
- Modify: `CLAUDE.md`
- Modify: `README.md`

- [ ] **Step 10.1: 更新 CLAUDE.md Architecture 节**

替换 "Two execution paths" 段落，新增：

```markdown
**Auto-crystallize**: `icc_exec` 的 synthesis_ready 触发时，daemon 自动从 WAL 提取代码并写入 `.py`+`.yaml` pipeline（intent_signature 已编码 connector/mode/name）。文件已存在时跳过；`icc_crystallize` 手动调用可强制覆盖。

**Span path**: `icc_span_open` → [任意 MCP tool calls，PostToolUse 录制] → `icc_span_close` → span-wal + span-candidates 更新 policy。stable 时自动生成 Python skeleton 到 `_pending/`。`icc_span_approve` 将 skeleton 移入正式目录并生成 YAML，激活 bridge。

**Span bridge**: `icc_span_open` 检测 stable + pipeline 存在 → PipelineEngine 直接执行并返回结果，零 LLM 推理。`_record_pipeline_event` 被调用，pipeline 质量进入 pipelines-registry 正常追踪。

**单 Span 约束**: 任意时刻最多一个 active span。SessionStart hook 清除 stale active_span_id。icc_exec 调用不进入 span action 录制。

**废弃**: `icc_read`、`icc_write` 返回错误并引导使用 `icc_span_open`。

**新增资源**: `connector://<name>/spans` — 该 connector 的 span intent policy 状态 JSON。
```

- [ ] **Step 10.2: 更新 README MCP surface table**

新增三行工具，移除 icc_read / icc_write，更新 Resources 行（含 `connector://{vertical}/spans`）。

- [ ] **Step 10.3: 运行完整测试套件**

```bash
python -m pytest tests -q
```

Expected: all tests pass.

- [ ] **Step 10.4: Commit**

```bash
git add CLAUDE.md README.md
git commit -m "docs: update CLAUDE.md and README for span system and auto-crystallize"
```

---

## Self-Review

**Spec coverage check:**

| Spec requirement | Task |
|---|---|
| icc_exec auto-crystallize at synthesis_ready（no-overwrite）| Task 2 |
| SessionStart clears stale active_span_id | Task 3 |
| PostToolUse records actions，excludes icc_exec | Task 4 |
| PreToolUse validates span tools | Task 5 |
| icc_span_open with bridge（PipelineEngine，no recipe）| Task 6 |
| Single active span enforcement | Task 6（open_span raises RuntimeError）|
| icc_span_close writes WAL，updates candidates | Task 7 |
| Skeleton auto-gen at stable（once only）| Task 7 |
| icc_span_approve moves .py + generates .yaml | Task 8 |
| bridge_active after approve | Task 8 |
| _record_pipeline_event after span bridge | Task 6（handler code）|
| icc_read / icc_write deprecated | Task 9 |
| connector://spans resource | Task 9 |
| Docs updated | Task 10 |

All spec requirements covered. ✓
