# Universal Flywheel — Design Spec

**Date:** 2026-04-07  
**Status:** Approved for implementation

---

## Problem

The current flywheel only covers `icc_exec` (Python execution). Every other tool call — Lark APIs, context7, file ops, skill sequences — is invisible to the flywheel. The root cause: `icc_exec` conflates "execution primitive" with "flywheel unit". This is the ceiling.

---

## Core Insight: Two-Layer Separation

```
飞轮层（Intent Span）
  span = 一个意图 + 它的所有动作序列
  span 是 flywheel 的原子单元

执行层（Actions）
  icc_exec / lark-* / context7 / Read / Write / Bash / ...
  任何 tool call 都是 span 内的 action
```

`icc_exec` 退化为 action 类型之一，不再是 flywheel 入口。

---

## Intent Span

### 定义

一个 span 代表一个完整的意图执行单元：

```jsonc
// ~/.emerge/span-wal/<session_id>/spans.jsonl — 每行一个已关闭的 span
{
  "span_id": "uuid4",
  "intent_signature": "lark.write.create-summary",   // <connector>.(read|write).<name>
  "description": "从会议记录生成摘要文档",
  "source": "skill" | "manual",
  "skill_name": "lark-doc",                          // source=skill 时有值
  "opened_at_ms": 1700000000000,
  "closed_at_ms": 1700000005000,
  "outcome": "success" | "failure" | "aborted",
  "is_read_only": false,                             // 派生：所有 action.has_side_effects==false → true
  "args": {},                                         // span 输入参数
  "result_summary": {},                               // span 输出摘要
  "actions": [
    {
      "seq": 0,
      "tool_name": "mcp__lark_doc__create",
      "args_hash": "sha256[:16]",                    // 不存完整 args，防止敏感数据落盘
      "has_side_effects": true,
      "ts_ms": 1700000001000
    }
  ]
}
```

### intent_signature 格式

继承现有约束：`<connector>.(read|write).<name>`

- 中段 `read|write` 即 is_read_only 的天然编码
- 与现有 `pre_tool_use.py` 校验、bridge 解析逻辑完全对齐
- Skills 自动绑定：skill 调用时用 skill 名映射到 intent_signature

### Span 的开启与关闭

- **Skills**：skill 模板首步调 `icc_span_open`，末步调 `icc_span_close`
- **非 skill 的手动多步操作**：CC 显式调 `icc_span_open` / `icc_span_close`
- **单步原子操作**：不需要 span，直接执行，不进入 flywheel

---

## 生命周期

### Span Candidates

每个 intent_signature 维护 `span-candidates.json`（对齐现有 `candidates.json` 结构）：

```jsonc
{
  "spans": {
    "lark.write.create-summary": {
      "intent_signature": "lark.write.create-summary",
      "is_read_only": false,
      "attempts": 0,
      "successes": 0,
      "consecutive_failures": 0,
      "recent_outcomes": [],       // 最近 WINDOW_SIZE 次结果
      "human_fixes": 0,
      "last_ts_ms": 0,
      "description": ""
    }
  }
}
```

### 阈值

**写 span（pipeline 产物）**：与现有 policy 完全一致

| 阶段 | 条件 |
|---|---|
| explore → canary | attempts≥20, success_rate≥0.95, human_fix_rate≤0.05 |
| canary → stable | attempts≥40, success_rate≥0.97 |
| stable → rollback | consecutive_failures≥2 |

**只读 span（macro 产物）**：去掉 verify_rate 约束（macro 无 verify step），verify 自动 pass

| 阶段 | 条件 |
|---|---|
| explore → canary | attempts≥20, success_rate≥0.95 |
| canary → stable | attempts≥40, success_rate≥0.97 |
| stable → rollback | consecutive_failures≥2 |

---

## 双轨 Crystallize

### 决策边界

`span.is_read_only` → macro；`!is_read_only` → Python pipeline

### 轨道一：只读 span → Macro（全自动）

stable 时 daemon 自动写文件，零人工干预：

```jsonc
// ~/.emerge/connectors/<connector>/macros/<name>.json
{
  "intent_signature": "lark.read.get-summary",
  "is_read_only": true,
  "args_schema": { "doc_id": "string" },
  "actions": [
    {
      "tool_name": "mcp__lark_doc__get",
      "args_template": { "doc_id": "{{args.doc_id}}" }
    }
  ]
}
```

### 轨道二：写 span → Python Pipeline（自动生成 + 人工 approve）

stable 时 daemon 从 span-wal 取最近一次成功 span，自动生成 Python skeleton：

```python
# auto-generated from span: lark.write.create-summary
# review before approving — call icc_span_approve to activate bridge
def run_write(metadata, args):
    doc = mcp_call("mcp__lark_doc__create", {"title": args["title"]})
    mcp_call("mcp__lark_doc__append", {"doc_id": doc["doc_id"], "content": args["content"]})
    return {"doc_id": doc["doc_id"]}
```

Daemon emit `policy.synthesis_ready`，CC 展示给用户 review，用户调 `icc_span_approve` 后 pipeline 进入 bridge。

---

## Bridge

### 触发条件

`icc_span_open` 时 daemon 查 `pipelines-registry.json`：intent_signature stable 且产物存在 → bridge 触发。

### Bridge 响应协议

新增 `bridge_type` 字段区分两种情况（避免 CC 歧义）：

```jsonc
// Macro bridge — 返回 recipe，CC 按步骤执行
{
  "bridge": true,
  "bridge_type": "recipe",
  "span_id": "abc",
  "recipe": [
    { "tool": "mcp__lark_doc__get", "args": { "doc_id": "xxx" } }
  ]
}

// Pipeline bridge — 直接返回结果
{
  "bridge": true,
  "bridge_type": "result",
  "span_id": "abc",
  "result": { "doc_id": "xyz" },
  "bridge_promoted": true
}
```

CC 检查 `bridge_type`：`recipe` → 按步骤执行 tool calls；`result` → 直接使用结果。

### Macro Bridge 执行流

```
CC: icc_span_open("lark.read.get-summary", args={doc_id})
  ← {bridge: true, bridge_type: "recipe", recipe: [{tool, args}]}
CC: 按 recipe 顺序执行 tool calls（无 LLM 推理）
CC: icc_span_close("success", result_summary)
```

省掉的是 LLM 推理（WHAT/HOW），不是 tool execution。CC 是执行者，daemon 是路由器。

---

## 状态可见性

### Active Span 跨两个 State Root 的问题

- Daemon exec state: `~/.emerge/repl/`
- Hook state: `CLAUDE_PLUGIN_DATA`（PostToolUse 只能访问这里）

**解决方案**：`icc_span_open` 时 daemon 把 `active_span_id` 写入 hook state（`state.json` 新增字段）。PostToolUse hook 读取该字段，把当前 tool call 归到对应 span。

```jsonc
// state.json 新增字段
{
  "active_span_id": "uuid4",
  "active_span_intent": "lark.write.create-summary"
}
```

`icc_span_close` 时清除这两个字段。

---

## has_side_effects 判定

Hook 维护静态只读白名单（保守策略：未知 tool 默认 `has_side_effects=true`）：

```python
_READ_ONLY_PATTERNS = [
    "mcp__context7__",          # context7 全是查询
    "mcp__*__get",
    "mcp__*__list",
    "mcp__*__search",
    "mcp__*__query",
    "Read",                      # Claude 内置
    "Glob",
    "Grep",
    "WebFetch",
    "WebSearch",
]
```

---

## MCP 工具接口

### 新增工具

```
icc_span_open(intent_signature, description?, args?, source?)
  → 正常：{span_id, status: "opened", policy_status: "explore"|"canary"|"stable"}
  → Bridge（macro）：{span_id, bridge: true, bridge_type: "recipe", recipe: [...]}
  → Bridge（pipeline）：{span_id, bridge: true, bridge_type: "result", result: {...}}

icc_span_close(outcome: "success"|"failure"|"aborted", result_summary?)
  → {span_id, policy_status, synthesis_ready: bool}

icc_span_approve(intent_signature)
  → 审核并 approve 生成的 write skeleton，激活 bridge
  → {approved: true, pipeline_path}
```

### 废弃工具

```
icc_read          → 被 icc_span_open bridge 替代
icc_write         → 被 icc_span_open bridge 替代
icc_crystallize   → 被自动 crystallize + icc_span_approve 替代
```

### 保留工具

```
icc_exec          — Python 执行原语（span 内的 action 之一）
icc_reconcile     — delta 状态追踪（独立关注点）
icc_goal_*        — 目标管理（独立关注点）
```

### PreToolUse Hook 扩展

新增 `icc_span_open` / `icc_span_close` / `icc_span_approve` 的参数校验，与现有工具校验风格一致。

---

## PostToolUse Hook 扩展

读取 `state.json` 中的 `active_span_id`，若存在则把当前 tool call 记录追加到内存 buffer；`icc_span_close` 时 flush 到 `span-wal.jsonl`。

Buffer 存储方案：写入 hook state 的临时文件 `active-span-actions.jsonl`（per span_id），close 时 daemon 读取并写入 WAL。

---

## 新增资源

```
connector://<name>/macros    — 列出该 connector 下所有 stable macro
```

---

## 文件布局

```
~/.emerge/
  span-wal/
    <session_id>/
      spans.jsonl                  # 已关闭 span 的 WAL
  connectors/
    <connector>/
      macros/
        <intent-name>.json         # macro crystallize 产物
      pipelines/                   # 现有，写 span 的 Python pipeline 产物
  repl/
    <session_id>/
      span-candidates.json         # span 级别的 policy stats
      wal.jsonl                    # 现有，icc_exec 的 WAL（保留）
  hook-state/
    state.json                     # 新增 active_span_id, active_span_intent
    active-span-actions.jsonl      # 临时 buffer，span close 后清除
```

---

## 不变量

- `icc_exec` 继续正常工作，WAL 和 candidates 保持现有行为，不受 span 系统影响
- Span 和 exec 共用 `pipelines-registry.json` 作为 bridge 查表，key 均为 `intent_signature`
- `pre_tool_use.py` 的 intent_signature 格式校验规则不变：`<connector>.(read|write).<name>`
- Atomic write（temp + rename）适用于所有新增 JSON 文件
- `no_replay` 语义仅适用于 `icc_exec` WAL，与 span WAL 无关
