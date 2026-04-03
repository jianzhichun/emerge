# Emerge 最简内核设计说明（CC + A轨 + Delta）

## 1. 一句话定义

Emerge 是基于最简 Claude Code 三原语（`read` / `write` / `bash`）的执行内核：

- 用 `pipeline A轨` 承接可重复、可验证的稳定任务
- 用 `state delta` 管理上下文，降低噪声与无效感知
- 用最小上下文注入策略减少 token 消耗

---

## 2. 设计边界

### 2.1 本方案只做三件事

1. 定义三原语执行模型（CC 最简抽象）
2. 定义 A轨 pipeline 的演进机制（evolve）
3. 定义 state delta 驱动的上下文压缩机制

### 2.2 本方案明确不做

- 不引入重型领域框架
- 不把 Adapter 作为一等中心抽象
- 不追求“一版覆盖所有行业语义”
- 不在本阶段做品牌、UI、商业策略扩展

---

## 3. 核心目标（按优先级）

1. **减少 token**：不再把全量状态反复注入模型
2. **减少不必要感知**：模型只看“和当前任务相关”的变化
3. **提高稳定性**：高频路径优先走 A轨，减少自由推理波动
4. **保留可追溯性**：关键动作保留证据链和回读验证结果

---

## 4. 执行模型：最简三原语

### 4.1 `read`

用于结构化读取当前世界状态，输出可比对的数据快照。

### 4.2 `write`

用于显式执行动作，要求动作可回读验证。

### 4.3 `bash`

用于探索、排障、临时处理。默认不作为高频生产路径。

### 4.4 调度优先级

- 命中 A轨 pipeline：优先 `read/write`，限制自由 `bash`
- 未命中 A轨：允许 `bash` 探索，但动作后必须收敛回 `read/write` 闭环

---

## 5. A轨 Pipeline（evolve 机制）

## 5.1 A轨定位

A轨是“稳定执行轨道”，用于承载：

- 频繁出现
- 输入结构清晰
- 输出可验证
- 错误处理可模板化

## 5.2 A轨最小单元

每条 pipeline 包含：

- `intent_signature`：意图签名
- `read_steps[]`：前置读取
- `write_steps[]`：执行动作
- `verify_steps[]`：写后验证
- `rollback_or_stop_policy`：失败回滚或中止规则

## 5.3 evolve 规则

- 若同类任务多次成功且人工修正少，则提升为 A轨候选
- 若 A轨连续失败超过阈值，则自动降级为探索态
- A轨版本化管理：`pipeline_id@version`

---

## 6. State Delta 上下文管理（降噪核心）

## 6.1 原则

上下文注入不再依赖“全量 state”，而是依赖“自上轮以来的变化”。

## 6.2 Delta 分层保证

1. **Core Critical（强保证）**
  - 核心对象和关键字段不漏报
  - 允许重复，不允许遗漏
2. **Core Secondary（弱保证）**
  - 次关键变化可聚合、可阈值过滤
3. **Peripheral（近似）**
  - 仅摘要统计，不逐项展开

## 6.3 上下文注入模板（固定三段）

每轮仅注入：

1. `Goal`：当前任务目标
2. `Delta`：本轮关键变化
3. `Open Risks`：未闭环风险与未验证项

超预算时的裁剪顺序：

- 先裁 `Peripheral`
- 再聚合 `Secondary`
- `Core Critical` 不裁剪

---

## 7. 证据链与一致性

## 7.1 证据链字段

关键动作附带：

- `before_hash`
- `after_hash`
- `evidence_events[]`
- `verification_state`（`verified` / `unverified` / `degraded`）

## 7.2 一致性窗口

引入 `consistency_window_ms` 处理异步回报：

- 窗口内先给 provisional 结果
- 窗口结束后 reconcile（确认/修正/撤回）

## 7.3 不一致处理

- 状态与事件互证失败即标记 `degraded`
- `degraded` 下禁止自动续写高风险动作

---

## 8. Hook 协同（与 CC 原生机制对齐）

- `SessionStart`：注入当前基线与 A轨可用集合
- `UserPromptSubmit`：按需注入最小任务上下文
- `PostToolUse`：注入本轮 delta 与验证状态
- `PreCompact`：输出压缩保留指令（非 `additionalContext`）
- `SessionStart(source=compact)`：压缩后恢复关键状态

---

## 9. 输出契约（给模型和用户）

统一三段式：

1. `Core Delta`
2. `Secondary Summary`
3. `Verification & Risk`

示例：

- `+Object: Order#A123 [core]`
- `~Object: Wall#2B0.length 5000->4980 [core]`
- `Secondary changes: 12 (aggregated)`
- `Verification: degraded (awaiting reconciliation)`

---

## 10. 风险与降级

### 10.1 常见失效模式

- 读取失败或快照缺失
- 写后回读失败
- 事件晚到/乱序
- A轨脚本版本与环境不匹配

### 10.2 降级策略

- 优先降级到只读解释模式
- 限制自动连续写入
- 所有降级必须显式输出原因与恢复条件

---

## 11. 测试与验收（围绕“省 token + 稳执行”）

### 11.1 核心验收指标

- Core Critical 漏报率：0（基准集）
- 平均 prompt token：相较全量注入显著下降
- A轨命中任务成功率：稳定高于探索态
- `degraded` 路径可解释且可恢复

### 11.2 测试层次

- 单元测试：delta 分类、注入裁剪、一致性窗口
- 流程测试：`write -> read-back -> delta-check`
- 回放测试：A轨命中与未命中两类对照
- 故障注入：延迟、丢包、乱序、回读失败

---

## 12. 里程碑（MVP 优先）

1. 三原语执行骨架（`read/write/bash`）打通
2. Delta 注入三段模板上线（Goal/Delta/Open Risks）
3. A轨 pipeline v1（至少 2 条高频任务）
4. 一致性窗口 + degraded 降级机制上线
5. token 与成功率看板上线，驱动后续 evolve

---

## 13. 与 Plugin Foundation 计划对齐（实现映射）

本节把本 spec 与现有 Plugin Foundation 计划对齐，确保从概念到实现可直接落地。

### 13.1 插件与运行骨架

- `.claude-plugin/plugin.json`：插件元信息（名称、版本、mcp 配置入口）
- `.mcp.json`：注册 Python stdio MCP server，暴露 `icc_read` / `icc_write` / `icc_exec`
- `hooks/hooks.json`：注册 `SessionStart` / `UserPromptSubmit` / `PostToolUse` / `PreCompact`

### 13.2 执行平面实现

- `scripts/repl_state.py`：持久化 REPL 状态（跨调用变量保持）
- `scripts/repl_daemon.py`：MCP JSON-RPC 路由与工具调用分发
- `scripts/pipeline_engine.py`：YAML + Python 双文件 pipeline 执行器
- `connectors/mock/pipelines/`：A轨最小样例（`read/layers`、`write/add-wall`）

### 13.3 Delta 与上下文注入实现

- `scripts/state_tracker.py`：`before/after` 差异汇总与分层标注
- `hooks/session_start.py`：注入初始化最小上下文
- `hooks/user_prompt_submit.py`：注入任务相关上下文
- `hooks/post_tool_use.py`：注入本轮 delta
- `hooks/pre_compact.py`：输出压缩保留指令（纯文本）

### 13.4 验证清单（MVP 必跑）

- `tests/test_plugin_static_config.py`
- `tests/test_repl_daemon_exec.py`
- `tests/test_pipeline_engine.py`
- `tests/test_hook_scripts_output.py`
- `tests/test_mcp_tools_integration.py`

验收重点：

- 三原语路径可用（`read/write/bash`）
- A轨 pipeline 可执行且可验证
- Delta 三段注入可稳定裁剪 token
- 异步下可进入并恢复 `degraded`

---

## 14. 决策记录（本轮）

- 名称固定：`Emerge`
- 范式固定：最简 CC 三原语，不引入重型抽象
- 演进路径：A轨 pipeline + 数据驱动 evolve
- 上下文策略：state delta 注入优先，目标降噪降 token
