# CC Protocol Compliance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring emerge's MCP protocol usage to full compliance with MCP 2025-03-26: resource subscriptions, eliminate JSON-in-text duplication in resource responses, budget-aware context injection in PostToolUse, and PreToolUse `updatedInput` for intent_signature normalization.

**Architecture:** Three changes to `emerge_daemon.py` (resource subscribe capability, `_read_resource` text→blob, registry-change notification) + one change to `hooks/post_tool_use.py` + `scripts/state_tracker.py` (risk budgeting) + one change to `hooks/pre_tool_use.py` (updatedInput). No new files except tests.

**Tech Stack:** Python 3.11+, MCP protocol `resources/subscribe`, `notifications/resources/list_changed`, JSON MimeType resource responses.

---

## File Map

| File | Change |
|------|--------|
| `scripts/emerge_daemon.py` | Enable `resources.subscribe=True`; `_read_resource` returns blob not text; emit `list_changed` on registry write |
| `scripts/state_tracker.py` | `format_context()` — trim risks by budget_chars, not just deltas |
| `hooks/post_tool_use.py` | Pass `budget_chars` down for risk trimming (already passed for deltas) |
| `hooks/pre_tool_use.py` | Return `updatedInput` to normalize intent_signature (add missing connector prefix hint) |
| `tests/test_mcp_tools_integration.py` | New tests for subscribe, blob format, risk budgeting |
| `tests/test_pre_tool_use.py` | New test for updatedInput normalization |

---

### Task 1: Enable resource subscriptions + emit `list_changed` on registry write

**Files:**
- Modify: `scripts/emerge_daemon.py` (initialize capabilities, `_atomic_write_json` or `_update_pipeline_registry`)
- Test: `tests/test_mcp_tools_integration.py`

- [ ] **Step 1: Write the failing test**

```python
def test_initialize_declares_resource_subscribe_capability():
    """initialize response must set resources.subscribe=True (MCP 2025-03-26)."""
    from scripts.emerge_daemon import EmergeDaemon
    daemon = EmergeDaemon()
    req = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
           "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                      "clientInfo": {"name": "test", "version": "0"}}}
    resp = daemon.handle_jsonrpc(req)
    assert resp["result"]["capabilities"]["resources"]["subscribe"] is True


def test_registry_write_emits_list_changed_notification(tmp_path):
    """Writing pipelines-registry.json must push resources/list_changed notification."""
    import json, os
    from scripts.emerge_daemon import EmergeDaemon
    os.environ["EMERGE_STATE_ROOT"] = str(tmp_path)
    daemon = EmergeDaemon()
    pushed = []
    daemon._write_mcp_push = lambda p: pushed.append(p)

    # Trigger a registry write via _update_pipeline_registry
    daemon._update_pipeline_registry("gmail.read.fetch", outcome="success")

    os.environ.pop("EMERGE_STATE_ROOT", None)

    list_changed = [p for p in pushed if p.get("method") == "notifications/resources/list_changed"]
    assert len(list_changed) >= 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_initialize_declares_resource_subscribe_capability tests/test_mcp_tools_integration.py::test_registry_write_emits_list_changed_notification -xvs
```

Expected: first test fails (`subscribe` is `False`); second test fails (no notification pushed)

- [ ] **Step 3: Enable `resources.subscribe=True` in initialize response**

In `scripts/emerge_daemon.py`, find the `initialize` response block (where `"capabilities"` is set) and change:

```python
"resources": {"subscribe": False},
```
to:
```python
"resources": {"subscribe": True},
```

- [ ] **Step 4: Emit `notifications/resources/list_changed` after registry write**

In `scripts/emerge_daemon.py`, find `_update_pipeline_registry` (the method that calls `self._atomic_write_json(_reg_path, ...)`). After the `_atomic_write_json` call, add:

```python
            # Notify CC that the resource list has changed (MCP 2025-03-26 subscriptions)
            try:
                self._write_mcp_push({
                    "jsonrpc": "2.0",
                    "method": "notifications/resources/list_changed",
                    "params": {},
                })
            except Exception:
                pass
```

Note: find the exact location with `grep -n "_atomic_write_json.*pipelines-registry\|_atomic_write_json.*_reg_path" scripts/emerge_daemon.py`.

- [ ] **Step 5: Run tests to verify they pass**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_initialize_declares_resource_subscribe_capability tests/test_mcp_tools_integration.py::test_registry_write_emits_list_changed_notification -xvs
```

Expected: both `PASSED`

- [ ] **Step 6: Run full suite**

```bash
python -m pytest tests/ -q --tb=short
```

Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add scripts/emerge_daemon.py tests/test_mcp_tools_integration.py
git commit -m "feat: enable MCP resource subscriptions and emit list_changed on registry write"
```

---

### Task 2: Resource responses — eliminate `text: json.dumps()` duplication

**Files:**
- Modify: `scripts/emerge_daemon.py` (`_read_resource`, lines ~1752–1840)
- Test: `tests/test_mcp_tools_integration.py`

Context: MCP 2025-03-26 allows `blob` (base64) for binary data or `text` for text. For `application/json` resources, `text` with a JSON string forces CC to parse JSON from a string. The fix: keep `text` but make it the *only* representation — no duplicate `structuredContent`. (Note: resource responses are distinct from tool responses; they live in `resources/read` result, not `tools/call` result. The `text` field is correct for MCP resource responses — the fix here is ensuring we're NOT double-encoding by accidentally having both `text` and some other field.)

Run this audit first to understand actual duplication:

```bash
grep -n '"text": json.dumps\|"blob"\|structuredContent' scripts/emerge_daemon.py | grep -A2 -B2 "1752\|1756\|1760\|1770\|1773\|1776"
```

- [ ] **Step 1: Write the failing test**

```python
def test_resource_read_policy_current_has_no_extra_fields():
    """resources/read policy://current must return exactly uri+mimeType+text, no extras."""
    from scripts.emerge_daemon import EmergeDaemon
    daemon = EmergeDaemon()
    req = {
        "jsonrpc": "2.0", "id": 1,
        "method": "resources/read",
        "params": {"uri": "policy://current"},
    }
    resp = daemon.handle_jsonrpc(req)
    contents = resp["result"]["contents"]
    assert len(contents) == 1
    item = contents[0]
    # Only these three keys allowed; no structuredContent, no blob alongside text
    allowed = {"uri", "mimeType", "text"}
    extra = set(item.keys()) - allowed
    assert not extra, f"Unexpected fields in resource response: {extra}"
    import json
    json.loads(item["text"])  # must be valid JSON


def test_resource_read_state_deltas_is_valid_json():
    """resources/read state://deltas text field must be parseable JSON."""
    import json
    from scripts.emerge_daemon import EmergeDaemon
    daemon = EmergeDaemon()
    req = {"jsonrpc": "2.0", "id": 1, "method": "resources/read",
           "params": {"uri": "state://deltas"}}
    resp = daemon.handle_jsonrpc(req)
    item = resp["result"]["contents"][0]
    data = json.loads(item["text"])
    assert "open_risks" in data or "deltas" in data or "goal" in data
```

- [ ] **Step 2: Run tests to verify they fail (or identify actual duplication)**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_resource_read_policy_current_has_no_extra_fields tests/test_mcp_tools_integration.py::test_resource_read_state_deltas_is_valid_json -xvs
```

If the resource responses are already clean (`uri+mimeType+text` only), these tests will pass immediately — that means no fix needed here, just add as regression tests and commit.

If they fail (extra fields present), continue to Step 3.

- [ ] **Step 3: If Step 2 found extra fields — clean `_read_resource`**

Inspect each `return` in `_read_resource`. Each must be exactly:
```python
return {"uri": uri, "mimeType": "application/json", "text": json.dumps(data)}
```
Remove any extra keys. Do not add `blob` alongside `text`.

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_mcp_tools_integration.py::test_resource_read_policy_current_has_no_extra_fields tests/test_mcp_tools_integration.py::test_resource_read_state_deltas_is_valid_json -xvs
```

Expected: both `PASSED`

- [ ] **Step 5: Commit**

```bash
git add scripts/emerge_daemon.py tests/test_mcp_tools_integration.py
git commit -m "test: add resource response schema regression tests (MCP compliance)"
```

---

### Task 3: Budget-aware risk injection in PostToolUse

**Files:**
- Modify: `scripts/state_tracker.py` (`format_context`, lines ~160–212)
- Test: `tests/test_state_tracker.py` (existing file, add tests)

Current behavior: `format_context(budget_chars=N)` only trims delta text — the risk list is always fully injected regardless of budget. With 100+ risks this causes severe context inflation.

- [ ] **Step 1: Write the failing test**

```python
# In tests/test_state_tracker.py (or test_mcp_tools_integration.py if test_state_tracker.py doesn't exist)

def test_format_context_trims_risks_when_over_budget():
    """format_context must trim risk list when budget_chars is exceeded."""
    from scripts.state_tracker import StateTracker

    tracker = StateTracker.__new__(StateTracker)
    # Build 50 risks — all open
    tracker.state = {
        "deltas": [],
        "open_risks": [
            {"risk_id": f"r{i}", "text": f"Risk item {i} " * 10, "status": "open",
             "created_at_ms": i, "snoozed_until_ms": 0, "handled_reason": "",
             "source_delta_id": "", "intent_signature": ""}
            for i in range(50)
        ],
        "goal": "test goal",
        "goal_source": "test",
    }

    # Tiny budget that can't hold all 50 risks
    ctx = tracker.format_context(budget_chars=500)
    risks_text = ctx["Open Risks"]
    # Must be truncated and include a hint that more exist
    assert len(risks_text) <= 600  # some slack for the truncation message
    assert "more" in risks_text.lower() or len([l for l in risks_text.splitlines() if l.startswith("- ")]) < 50
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/ -k "test_format_context_trims_risks" -xvs
```

Expected: `FAILED` — risk text is the full 50-item list regardless of budget

- [ ] **Step 3: Add risk budgeting to `format_context`**

In `scripts/state_tracker.py`, in `format_context()`, replace the risks section (lines ~186–194):

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/ -k "test_format_context_trims_risks" -xvs
```

Expected: `PASSED`

- [ ] **Step 5: Run full suite**

```bash
python -m pytest tests/ -q --tb=short
```

Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add scripts/state_tracker.py tests/
git commit -m "feat: budget-aware risk injection in format_context — trim risks to 1/3 of budget_chars"
```

---

### Task 4: PreToolUse `updatedInput` — normalize intent_signature

**Files:**
- Modify: `hooks/pre_tool_use.py`
- Test: `tests/test_pre_tool_use.py` (add new test)

Context: CC sometimes calls `icc_exec` or `icc_span_open` with a short `intent_signature` like `"read.layers"` instead of the full `"zwcad.read.layers"`. The PreToolUse hook currently just blocks with an error. Instead, when the signature format is wrong but fixable (e.g., exactly 2 parts instead of 3), inject `updatedInput` to block with a specific correction hint. If the connector can be inferred from active context, return `updatedInput` with the corrected signature.

Note: CC 2025-03-26 supports `hookSpecificOutput.updatedInput` — the hook returns a modified `input` dict that replaces the tool's arguments before execution.

- [ ] **Step 1: Write the failing test**

```python
# In tests/test_pre_tool_use.py — add this test

def test_pre_tool_use_blocks_missing_intent_with_correction_hint():
    """PreToolUse for icc_exec missing intent_signature must block with correction hint."""
    import json, subprocess, sys
    from pathlib import Path
    ROOT = Path(__file__).resolve().parents[1]
    payload = {
        "tool_name": "emerge__icc_exec",
        "tool_input": {"code": "result = {}", "mode": "inline_code"},
    }
    result = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "pre_tool_use.py")],
        input=json.dumps(payload),
        capture_output=True, text=True, cwd=str(ROOT),
    )
    out = json.loads(result.stdout)
    assert out.get("decision") == "block"
    assert "intent_signature" in out.get("reason", "").lower()


def test_pre_tool_use_approves_valid_intent_signature():
    """PreToolUse for icc_exec with valid intent_signature must approve."""
    import json, subprocess, sys
    from pathlib import Path
    ROOT = Path(__file__).resolve().parents[1]
    payload = {
        "tool_name": "emerge__icc_exec",
        "tool_input": {
            "intent_signature": "zwcad.read.layers",
            "code": "result = {}",
            "mode": "inline_code",
        },
    }
    result = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "pre_tool_use.py")],
        input=json.dumps(payload),
        capture_output=True, text=True, cwd=str(ROOT),
    )
    out = json.loads(result.stdout)
    assert "decision" not in out  # no block
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
```

- [ ] **Step 2: Run tests to verify they pass (baseline)**

```bash
python -m pytest tests/test_pre_tool_use.py::test_pre_tool_use_blocks_missing_intent_with_correction_hint tests/test_pre_tool_use.py::test_pre_tool_use_approves_valid_intent_signature -xvs
```

If these already pass, the existing behavior is correct. Continue to Step 3 to add `updatedInput` for 2-part signatures.

- [ ] **Step 3: Write test for `updatedInput` on 2-part signature**

```python
def test_pre_tool_use_blocks_two_part_intent_with_fix_hint():
    """When intent_signature has 2 parts (missing connector), block and explain required format."""
    import json, subprocess, sys
    from pathlib import Path
    ROOT = Path(__file__).resolve().parents[1]
    payload = {
        "tool_name": "emerge__icc_exec",
        "tool_input": {
            "intent_signature": "read.layers",   # 2 parts — missing connector
            "code": "result = {}",
            "mode": "inline_code",
        },
    }
    result = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "pre_tool_use.py")],
        input=json.dumps(payload),
        capture_output=True, text=True, cwd=str(ROOT),
    )
    out = json.loads(result.stdout)
    assert out.get("decision") == "block"
    reason = out.get("reason", "")
    # Must explain correct format
    assert "connector.mode.name" in reason or "zwcad.read.layers" in reason or "3 parts" in reason.lower()
```

- [ ] **Step 4: Run test to verify it fails or passes**

```bash
python -m pytest tests/test_pre_tool_use.py::test_pre_tool_use_blocks_two_part_intent_with_fix_hint -xvs
```

If it fails, the hook doesn't provide a specific error for 2-part signatures. Fix it in Step 5. If it already passes, skip to Step 6.

- [ ] **Step 5: Improve 2-part signature error in `hooks/pre_tool_use.py`**

Find the intent_signature validation block (around lines 40–65 in `pre_tool_use.py`). Add a specific check before the generic regex check:

```python
    # Check for common 2-part mistake (connector omitted)
    if intent_signature and len(intent_signature.split(".")) == 2:
        error_msg = (
            f"icc_exec: intent_signature {intent_signature!r} has only 2 parts. "
            "Required format: connector.mode.name (e.g. 'zwcad.read.layers'). "
            "Add the connector name as the first part."
        )
```

This check goes immediately after extracting `intent_signature` from `tool_input`, before the regex validation.

- [ ] **Step 6: Run all pre_tool_use tests**

```bash
python -m pytest tests/test_pre_tool_use.py -xvs
```

Expected: all pass

- [ ] **Step 7: Run full suite**

```bash
python -m pytest tests/ -q --tb=short
```

Expected: all pass

- [ ] **Step 8: Commit**

```bash
git add hooks/pre_tool_use.py tests/test_pre_tool_use.py
git commit -m "feat: improve PreToolUse validation — specific error for 2-part intent_signature"
```

---

### Task 5: Docs + memory update

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update CLAUDE.md Key Invariants**

Add to the Key Invariants section:

```markdown
- **Resource subscriptions**: daemon advertises `resources.subscribe=True` (MCP 2025-03-26). After every `_update_pipeline_registry` write, daemon emits `notifications/resources/list_changed` so CC can re-read `policy://current` without polling.
- **Context injection budgeting**: `format_context(budget_chars=N)` in `StateTracker` allocates at most 1/3 of `budget_chars` to the risk list, sorted by recency. Risks beyond the budget are collapsed to a count with a pointer to `state://deltas`. This prevents context inflation in high-risk-count sessions.
- **PreToolUse 2-part intent**: `pre_tool_use.py` provides a specific error message when `intent_signature` has exactly 2 parts, explaining the required `connector.mode.name` format.
```

- [ ] **Step 2: Run full suite one final time**

```bash
python -m pytest tests/ -q --tb=short
```

Expected: all pass

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document resource subscriptions, context budgeting, PreToolUse 2-part intent invariants"
```
