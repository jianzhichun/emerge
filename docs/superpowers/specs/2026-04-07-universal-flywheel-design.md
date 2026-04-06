# Universal Flywheel — Design Spec (v2)

**Date:** 2026-04-07  
**Status:** Approved for implementation

---

## 项目本质（审计结论）

Emerge 是 Claude Code 的**肌肉记忆飞轮**，核心用例是 Python 自动化特定垂直领域（CAD 软件 COM 操作、GUI 自动化、Excel 等），通过 remote runner 在 Windows 机器上执行。

项目力量的来源：
- **Pipeline 是唯一可信产物**：`.py`+`.yaml` 契约，强制 `verify_write`/`rollback`，可远程执行
- **WAL 是知识积累载体**：记录成功 Python 路径，供 crystallize 使用
- **Bridge 是零推理短路**：stable 后 daemon 内部执行 pipeline，CC 不推理 HOW/WHAT

---

## 问题陈述

现有飞轮只覆盖 `icc_exec`（Python 执行）。Lark API、context7、文件操作、skill 序列等外部 MCP tool call 模式对飞轮完全不可见——无法积累、无法 crystallize、无法短路。

---

## 核心设计原则

**一条路径，一种产物，一套 bridge。**

所有执行模式（icc_exec、Lark API、context7、任意 MCP tool）的稳定模式最终都产出 Python pipeline（`.py`+`.yaml`），共用同一套 PipelineEngine bridge 和 verify/rollback 机制。不引入第二种产物格式。

---

## 架构

### 两条观测路径，同一个目标

```
icc_exec path（现有，自动化增强）
────────────────────────────────────────────────────────
icc_exec(intent_signature=...) 
  → ExecSession 执行 Python
  → WAL 记录代码路径
  → candidates.json 更新 policy stats
  → synthesis_ready 时 daemon 自动 crystallize（不再需要 CC 手动调）
  → pipeline 进入 connectors/
  → bridge: icc_exec 检测 stable → PipelineEngine → 直接返回结果


Span path（新增）
────────────────────────────────────────────────────────
icc_span_open(intent_signature=...)
  → [CC 执行任意 MCP tool calls]  ← PostToolUse hook 全程录制
  → icc_span_close(outcome=success)
  → span-wal 记录 tool call 序列
  → span-candidates.json 更新 policy stats
  → synthesis_ready 时自动生成 Python skeleton（结构化提示）
  → CC/人工完善 skeleton → 放入 connectors/ pipeline 目录
  → bridge: icc_span_open 检测 stable pipeline 存在
           → PipelineEngine → 直接返回结果（bridge_type: result）
```

### 统一产物

```
~/.emerge/connectors/<connector>/pipelines/
  read/<name>.py      # run_read + verify_read
  read/<name>.yaml    # intent_signature, steps, verify_steps
  write/<name>.py     # run_write + verify_write（强制）+ rollback
  write/<name>.yaml   # intent_signature, rollback_or_stop_policy
```

两条路径都产出上述结构。Span path 的 skeleton 是起点，人工/CC 完善后才进入正式目录。

---

## icc_exec Path 增强：自动 Crystallize

### 现状

synthesis_ready 时只设置标志位，等待 CC 手动调 `icc_crystallize(connector, pipeline_name, mode)`。

### 增强

`intent_signature` 格式已经编码了全部信息：`zwcad.read.state` → connector=zwcad, mode=read, name=state。

synthesis_ready 触发时，daemon 自动执行 crystallize，无需 CC 介入：

```python
# _update_pipeline_registry 中，synthesis_ready 时：
connector, mode, name = intent_signature.split(".", 2)
self._auto_crystallize(
    intent_signature=intent_signature,
    connector=connector,
    pipeline_name=name,
    mode=mode,
    target_profile=entry.get("target_profile", "default"),
)
```

**碰撞策略**：pipeline 文件已存在时 `_auto_crystallize` 静默跳过（不覆盖）。人工维护的 pipeline 优先于自动生成的版本。`icc_crystallize` 手动调用始终强制覆盖，是唯一能覆盖已有文件的入口。

`icc_crystallize` 工具保留为手动覆盖入口，不废弃。

---

## Span Path 详细设计

### Span 数据结构

```jsonc
// ~/.emerge/repl/span-wal/spans.jsonl — 每行一个已关闭的 span
{
  "span_id": "uuid4",
  "intent_signature": "lark.write.create-summary",  // <connector>.(read|write).<name>
  "description": "从会议记录生成摘要文档",
  "source": "skill" | "manual",
  "skill_name": "lark-doc",           // source=skill 时有值
  "opened_at_ms": 1700000000000,
  "closed_at_ms": 1700000005000,
  "outcome": "success" | "failure" | "aborted",
  "is_read_only": false,              // 派生：所有 action.has_side_effects==false → true
  "args": {},
  "result_summary": {},
  "actions": [
    {
      "seq": 0,
      "tool_name": "mcp__lark_doc__create",
      "args_hash": "sha256[:16]",     // 不存完整 args，防敏感数据落盘
      "has_side_effects": true,
      "ts_ms": 1700000001000
    }
  ]
}
```

### intent_signature 格式

继承现有约束：`<connector>.(read|write).<name>`，与 `_PIPELINE_KEY_RE` 完全对齐。中段 `read|write` 即 `is_read_only` 的天然编码，也是 bridge 解析 connector/mode/name 的依据。

### Active Span 状态传递

`icc_span_open` 时 daemon 将 `active_span_id` + `active_span_intent` 写入 hook state 的 `state.json`。PostToolUse hook 读取后把每次 tool call 追加到 `active-span-actions.jsonl`（buffer）。`icc_span_close` 时 daemon 读取 buffer，写入 span-wal，清理 hook state。

```jsonc
// state.json 新增字段（icc_span_open 写入，icc_span_close 清除）
{
  "active_span_id": "uuid4",
  "active_span_intent": "lark.write.create-summary"
}
```

**单 Span 强制约束**：`icc_span_open` 检测到 `active_span_id` 已存在时返回错误，要求先调 `icc_span_close` 关闭前一个 span。不允许嵌套或并发 span，防止 action buffer 混入多个意图的 tool call。

**SessionStart 清理**：`hooks/session_start.py` 在会话开始时清除 `state.json` 中的 `active_span_id` 和 `active_span_intent`。防止上次会话崩溃遗留的 stale span ID 污染新会话的 action 录制。

### has_side_effects 判定

PostToolUse hook 维护静态白名单（保守：未知工具默认 `has_side_effects=true`）：

```python
_READ_ONLY_TOOL_NAMES = {"Read", "Glob", "Grep", "WebFetch", "WebSearch", "ToolSearch"}
_READ_ONLY_TOOL_PREFIXES = ("mcp__context7__",)
_READ_ONLY_TOOL_SUFFIXES = ("__get", "__list", "__search", "__query", "__read", "__resolve")
```

### Span Policy 生命周期

与现有 exec candidates 完全相同阈值，但**去掉 verify_rate 约束**（span 阶段无 verify step；verify 在完善后的 pipeline 里体现）：

| 阶段 | 条件 |
|---|---|
| explore → canary | attempts≥20, success_rate≥0.95, human_fix_rate≤0.05 |
| canary → stable | attempts≥40, success_rate≥0.97 |
| rollback | consecutive_failures≥2 |

Span candidates 存储在 `~/.emerge/repl/span-candidates.json`，与 `candidates.json` 平行。

### Span Crystallize：生成 Python Skeleton

stable 时 daemon 自动从 span-wal 取最近一次成功 span，生成 Python skeleton：

```python
# auto-generated from span: lark.write.create-summary
# intent_signature: lark.write.create-summary
# IMPORTANT: Replace stubs with actual implementation.
# Each mcp_call comment shows the tool that was called during exploration.
# Write pipelines MUST implement verify_write() and optionally rollback().

def run_write(metadata, args):
    # seq=0: mcp__lark_doc__create was called here
    raise NotImplementedError("implement: mcp__lark_doc__create equivalent")
    # seq=1: mcp__lark_doc__append was called here
    raise NotImplementedError("implement: mcp__lark_doc__append equivalent")
    return {"ok": True}

def verify_write(metadata, args, action_result):
    # Verify the write succeeded
    raise NotImplementedError("implement verify_write")

def rollback(metadata, args, action_result):
    pass  # optional
```

Skeleton 写入 `connectors/<connector>/pipelines/<mode>/_pending/<name>.py`。CC/人工完善后移入正式目录激活 bridge。

### Span Bridge

`icc_span_open` 时检查：intent_signature stable 且对应 pipeline 文件存在 → 调 PipelineEngine → 返回结果（bridge_type: "result"）。

```python
# icc_span_open 内部 bridge check
policy_status = span_tracker.get_policy_status(intent_signature)
if policy_status == "stable":
    parts = intent_signature.split(".", 2)
    connector, mode, name = parts
    pipeline_args = {**arguments, "connector": connector, "pipeline": name}
    try:
        if mode == "read":
            result = self.pipeline.run_read(pipeline_args)
        else:
            result = self.pipeline.run_write(pipeline_args)
        result["bridge_promoted"] = True
        # 记录 pipeline 执行事件，使 pipelines-registry.json 追踪 span pipeline 质量
        self._record_pipeline_event(
            tool_name="icc_span_open",
            arguments={**arguments, "connector": connector, "pipeline": name},
            result=result,
            is_error=False,
            execution_path="local",
        )
        return self._tool_ok_json({
            "bridge": True,
            "bridge_type": "result",
            "result": result,
        })
    except PipelineMissingError:
        pass  # pipeline 未就绪，正常走探索路径
```

**Bridge 后 pipeline 进入 pipelines-registry 生命周期**：每次 bridge 成功后调 `_record_pipeline_event`，span pipeline 的 success_rate / verify_rate 得到追踪，进而也能参与正常的 canary → stable 晋升（从 pipeline 角度再度验证）。

---

## MCP 工具接口

### 新增工具

```
icc_span_open(intent_signature, description?, args?, source?, skill_name?)
  → 正常：{span_id, status: "opened", policy_status}
  → Bridge 触发：{bridge: true, bridge_type: "result", result: {...}}

icc_span_close(span_id?, outcome: "success"|"failure"|"aborted", result_summary?)
  → {span_id, intent_signature, policy_status, synthesis_ready: bool}

icc_span_approve(intent_signature)
  → 审核并确认 skeleton 已就绪，daemon 将 _pending/<name>.py 移入正式目录
  → 同时生成配套 <name>.yaml（minimal：intent_signature + rollback_or_stop_policy + steps）
  → {approved: true, pipeline_path, yaml_path, bridge_active: bool}
```

### 工具状态

```
icc_exec          — 保留，现有行为不变，新增 synthesis_ready 时自动 crystallize
icc_crystallize   — 保留，作为手动覆盖入口（优先级高于自动生成）
icc_read          — 废弃，bridge 通过 icc_span_open 触发
icc_write         — 废弃，bridge 通过 icc_span_open 触发
icc_reconcile     — 保留，独立关注点
icc_goal_*        — 保留，独立关注点
```

### PreToolUse Hook 扩展

新增 `icc_span_open`/`icc_span_close`/`icc_span_approve` 的参数校验，与现有风格一致。

---

## PostToolUse Hook 扩展

读取 `state.json` 中的 `active_span_id`，若存在则追加当前 tool call 到 `active-span-actions.jsonl`（包含 tool_name、args_hash、has_side_effects、ts_ms）。不影响现有 delta tracking 逻辑。

**icc_exec 排除**：`tool_name` 以 `__icc_exec` 结尾时跳过 span 录制。icc_exec 有自己的 WAL，其 Python 代码路径已被 ExecSession 完整记录；把 icc_exec 记为 span action 既冗余又无法用于 skeleton 生成（skeleton 只有工具名，没有代码）。

---

## 新增 Resource

```
connector://<name>/spans    — 该 connector 下所有 span 意图的 policy 状态 JSON
```

（不引入 connector://macros，因为不存在 macro 产物）

---

## 文件布局

```
~/.emerge/
  repl/
    span-wal/
      spans.jsonl              # 已关闭 span 的 WAL（append-only）
    span-candidates.json       # span 级别 policy stats
    candidates.json            # 现有 exec candidates（保留不变）
    pipelines-registry.json    # bridge 查表（span + exec 共用同一张表）
  connectors/
    <connector>/
      pipelines/
        read/<name>.py+yaml    # 现有，exec 和 span 都产出到这里
        write/<name>.py+yaml   # 同上
        read/_pending/<name>.py    # span skeleton 暂存区（待完善）
        write/_pending/<name>.py   # 同上
  hook-state/
    state.json                 # 新增 active_span_id, active_span_intent 字段
    active-span-actions.jsonl  # span action buffer（临时，close 后删除）
```

---

## 不变量

- Python pipeline 是唯一 crystallize 产物；不引入 macro 格式
- 所有 write pipeline 必须实现 `verify_write`（PipelineEngine 强制）
- Bridge 始终由 PipelineEngine 执行，返回 result，不返回 recipe
- `span-wal` 和 exec `wal.jsonl` 相互独立，共用 `pipelines-registry.json` 作为 bridge 查表
- `_PIPELINE_KEY_RE` 和 intent_signature 格式约束对 span 和 exec 统一适用
- Atomic write（temp + rename）适用于所有新增 JSON 文件
- `_pending/` 目录下的 skeleton 不进入 bridge；移入正式目录后才激活
- 任何时刻最多一个 active span；`icc_span_open` 检测到已有 span 时报错
- SessionStart hook 清除 stale `active_span_id`；防止崩溃遗留状态污染新会话
- `icc_exec` 调用不进入 span action 录制；其代码路径由 ExecSession WAL 独立记录
- `_auto_crystallize` 不覆盖已有 pipeline 文件；`icc_crystallize` 手动调用才能覆盖
- `icc_span_approve` 必须同时生成 `.py`（从 _pending 移入）和 `.yaml`（新生成）
