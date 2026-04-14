# Daemon HTTP + Agents-Team Phase 1+ Design

**Date**: 2026-04-14  
**Status**: Approved  
**Scope**: Daemon stdio→HTTP migration, runner SSE push, unified event streams, cockpit auto-start, agents-team Monitors tab

---

## Overview

This spec covers the architectural upgrade that makes agents-team mode fully optimal:

1. **Daemon stdio → HTTP MCP server** — single persistent process shared across all CC sessions and watcher subagents
2. **Runner → Daemon push via SSE** — runner connects to daemon, events pushed in real-time, eliminating 5s poll
3. **Popup via SSE + correlation ID** — watcher calls `runner_notify()` MCP tool, daemon routes via SSE, runner returns result
4. **Unified per-scope event streams** — `events.jsonl`, `events-{profile}.jsonl`, `events-local.jsonl`; single `watch_emerge.py` script
5. **Cockpit auto-start** — daemon spawns cockpit and injects controls automatically
6. **Agents-team Monitors tab** — cockpit status-only view of connected runners and recent alerts

---

## 1. Daemon HTTP MCP Server

### 1.1 Architecture

```
现在（stdio）:
  CC session A → spawn emerge_daemon.py → process A (独立状态)
  CC session B → spawn emerge_daemon.py → process B (独立状态)

改后（HTTP）:
  CC session A ──HTTP──►
  CC session B ──HTTP──►  emerge_daemon.py (单一持久进程)
  Watcher 1    ──HTTP──►    共享状态：PatternDetector、runner SSE 连接池
  Watcher 2    ──HTTP──►    共享状态：events.jsonl 写入
```

### 1.2 DaemonHTTPServer

`emerge_daemon.py` 新增 `DaemonHTTPServer`，在后台线程启动 HTTP server，监听端口（默认 8789，可通过 `EMERGE_DAEMON_PORT` 覆盖）。

**端点：**

| 端点 | 方法 | 用途 |
|---|---|---|
| `/mcp` | POST | MCP HTTP transport（CC sessions 连接） |
| `/runner/sse` | GET | Runner 持久 SSE 连接 |
| `/runner/event` | POST | Runner 上报操作事件 |
| `/runner/online` | POST | Runner 上线通知 |
| `/runner/popup-result` | POST | Runner 弹窗结果回调 |

### 1.3 单例守护

PID file：`~/.emerge/daemon.pid`

启动逻辑：
```python
def ensure_running():
    pid_path = Path.home() / ".emerge" / "daemon.pid"
    if pid_path.exists():
        pid = int(pid_path.read_text().strip())
        try:
            os.kill(pid, 0)  # 进程存在
            return  # 已在跑，退出
        except ProcessLookupError:
            pass  # 僵尸 PID，继续启动
    # 写 PID，启动 server
    pid_path.write_text(str(os.getpid()))
```

atexit 清理 PID file。

### 1.4 plugin.json 变更

```json
{
  "mcpServers": {
    "emerge": {
      "url": "http://localhost:8789/mcp"
    }
  }
}
```

`command` 字段移除（HTTP mode 下 CC 不 spawn 进程）。

### 1.5 SessionStart Hook 升级

`hooks/session_start.py` 升级为 ensure-running launcher：

```python
import subprocess, sys
subprocess.Popen(
    [sys.executable, str(plugin_root / "scripts" / "emerge_daemon.py"), "--ensure-running"],
    start_new_session=True,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
```

`emerge_daemon.py --ensure-running`：检查 PID file → 进程存在则退出，否则启动 HTTP server 并 daemonize。

---

## 2. Runner ↔ Daemon 通信

### 2.1 Runner 启动流程

```
runner 启动
  1. GET  http://{team_lead_url}/runner/sse?runner_profile={profile}
     → 持久连接，接收 daemon 推送的命令
  2. POST http://{team_lead_url}/runner/online
     body: {"runner_profile": "mycader-1", "machine_id": "workstation-A"}
     → daemon append runner_discovered 到 events.jsonl
     → daemon append runner_online 到 events-{profile}.jsonl
```

### 2.2 Runner 上报事件

```
Human ops → runner 本地 EventBus
  → POST http://{team_lead_url}/runner/event
     body: {"runner_profile": "mycader-1", "machine_id": "...", "type": "...", ...}
  → daemon 写入 events-{profile}.jsonl
  → daemon EventRouter 触发 PatternDetector
  → pattern detected → append pattern_alert 到 events-{profile}.jsonl
```

### 2.3 Popup 下发（SSE + Correlation ID）

```
Watcher 调用 MCP tool runner_notify(runner_profile="mycader-1", ui_spec={...})
  → daemon 生成 popup_id = uuid4()
  → daemon SSE push to runner: {"type":"notify","popup_id":"abc","ui_spec":{...}}
  → daemon 创建 Future，等待结果（timeout=ui_spec.timeout_s + 5）

Runner 显示弹窗（operator 操作）
  → POST /runner/popup-result {"popup_id":"abc","value":"接管"}
  → daemon resolve Future

runner_notify MCP tool 返回 {"value": "接管", "popup_id": "abc"}
```

超时时 Future 返回 `{"value": null, "timed_out": true}`。

### 2.4 Daemon SSE 命令格式

```json
{"type": "notify",  "popup_id": "uuid", "ui_spec": {...}}
{"type": "ping"}
```

Keepalive：每 15s 发 `: keepalive\n\n`。

### 2.5 runner-bootstrap 变更

新增 `--team-lead-url` 参数：

```bash
python3 scripts/repl_admin.py runner-bootstrap \
  --ssh-target "user@host" \
  --target-profile "mycader-1" \
  --runner-url "http://host:8787" \
  --team-lead-url "http://192.168.1.100:8789"
```

`team_lead_url` 写入 runner 机器的 `~/.emerge/runner-config.json`，供 `remote_runner.py` 读取。

### 2.6 Daemon 连接状态追踪

```python
_connected_runners: dict[str, dict] = {
    "mycader-1": {
        "connected_at_ms": 1234567890,
        "last_event_ts_ms": 1234567890,
        "machine_id": "workstation-A",
        "last_alert": {"stage": "canary", "intent_signature": "..."}
    }
}
```

SSE 连接断开时从 dict 移除，记录 disconnected 事件。

---

## 3. 统一事件流

### 3.1 文件结构

```
~/.emerge/repl/
  events.jsonl              ← 全局流（team lead Monitor 订阅）
  events-{profile}.jsonl    ← 每 runner 独立流（watcher Monitor 订阅）
  events-local.jsonl        ← 本机本地事件流
```

### 3.2 事件类型

**全局流 events.jsonl：**

| type | 写入方 | 用途 |
|---|---|---|
| `runner_discovered` | daemon `/runner/online` | team lead spawn watcher |
| `runner_disconnected` | daemon SSE 断开 | team lead 感知 runner 下线 |
| `cockpit_action` | EventRouter（现有） | cockpit 提交的 actions |

**Per-runner 流 events-{profile}.jsonl：**

| type | 写入方 | 用途 |
|---|---|---|
| `runner_online` | daemon `/runner/online` | watcher 感知 runner 就绪 |
| `runner_event` | daemon `/runner/event` | 操作者原始事件 |
| `pattern_alert` | PatternDetector | watcher 响应 alert |
| `popup_result` | daemon `/runner/popup-result` | watcher 感知弹窗结果 |

**本地流 events-local.jsonl：**

| type | 写入方 | 用途 |
|---|---|---|
| `local_pattern_alert` | OperatorMonitor.process_local_file | 本机 adapter 触发的 alert |

### 3.3 事件格式

```json
{
  "type": "pattern_alert",
  "ts_ms": 1234567890,
  "runner_profile": "mycader-1",
  "stage": "canary",
  "intent_signature": "hypermesh.mesh.batch",
  "meta": {"occurrences": 6, "window_minutes": 12}
}
```

所有事件必含 `type` 和 `ts_ms`。

### 3.4 watch_emerge.py

替代 `watch_patterns.py`、`watch_pending.py`，tail 指定 events.jsonl 文件，按 type 格式化输出。

```bash
# team lead：订阅全局流
python3 watch_emerge.py

# watcher：订阅 runner 流
python3 watch_emerge.py --runner-profile mycader-1

# 本地模式
python3 watch_emerge.py --local
```

实现：tail-follow 模式（`follow=True`，记录 inode + offset），文件不存在时等待创建，按 `type` 路由到对应 formatter。

旧脚本 `watch_patterns.py`、`watch_pending.py` 保留，内部改为调用 `watch_emerge.py` 对应模式。

---

## 4. Cockpit 自动化

### 4.1 Daemon 自动启动 Cockpit

Daemon 启动时（`DaemonHTTPServer.__init__`）：
```python
def _ensure_cockpit(self):
    # 检查是否已有 cockpit 进程（PID file: ~/.emerge/cockpit.pid）
    # 没有则 spawn: python3 repl_admin.py serve --port 0
    # 读取 stdout 获取实际端口，写入 ~/.emerge/cockpit-url.txt
```

### 4.2 Daemon 自动注入 Controls

Daemon 启动后，读取所有 connector assets 并生成 controls HTML 注入 cockpit：

```python
def _inject_controls(self):
    for connector in list_connectors():
        notes = read_notes(connector)       # ~/.emerge/connectors/{c}/NOTES.md
        scenarios = read_scenarios(connector)  # scenarios/*.yaml
        html = render_controls_html(connector, notes, scenarios)
        cockpit_post_inject(connector, html)
```

Controls 模板（标准化，不需要 LLM）：
- Notes 摘要（前 10 行）
- Scenario 卡片（name + Run 按钮）
- Diagnostic 快捷按钮（ping、connection health）

### 4.3 /emerge:cockpit 简化

`commands/cockpit.md` 简化为：

```
1. 读取 ~/.emerge/cockpit-url.txt 获取 URL
2. 打印 URL 给用户
3. 启动 Monitor: watch_emerge.py（全局流）
4. 启动 Monitor: watch_emerge.py --runner-profile {每个已连接 runner}
```

不再负责：启动 server、注入 controls、启动旧 watch 脚本。

---

## 5. Agents-Team Monitors Tab

### 5.1 /api/control-plane/monitors 端点

`repl_admin.py` 新增只读端点：

```
GET /api/control-plane/monitors
返回：
{
  "runners": [
    {
      "runner_profile": "mycader-1",
      "connected": true,
      "connected_at_ms": 1234567890,
      "last_event_ts_ms": 1234567890,
      "machine_id": "workstation-A",
      "last_alert": {
        "stage": "canary",
        "intent_signature": "hypermesh.mesh.batch",
        "ts_ms": 1234567890
      }
    }
  ],
  "team_active": false
}
```

数据来源：daemon `_connected_runners` dict（通过 IPC 或共享文件 `~/.emerge/runner-monitor-state.json`）。

### 5.2 Cockpit Monitors Tab

位置：全局 tab 栏右侧（与 Audit / Session / State / Operator 并列）。

显示内容：
- 每行一个 runner：profile 名、连接状态（绿点/灰点）、连接时长、最近 alert（stage badge + intent）
- 无 runners 时：空状态文字 "No runners connected"
- `team_active: true` 时顶部显示 "Agents-team active" badge

只读，无按钮（spawn/stop 通过 CC 对话完成）。

SSE 推送：daemon 检测到 runner 连接/断开时，`_sse_broadcast({type: "monitors_updated"})` 通知 cockpit 刷新。

---

## 6. 文件变更

| 文件 | 变更 |
|---|---|
| `scripts/emerge_daemon.py` | 新增 `DaemonHTTPServer`（端口 8789）；`/runner/sse`、`/runner/event`、`/runner/online`、`/runner/popup-result` 端点；`_connected_runners` 追踪；新 MCP tool `runner_notify`；`--ensure-running` 启动模式；自动启动 cockpit + 注入 controls |
| `scripts/remote_runner.py` | 新增 SSE client，启动时连 daemon `/runner/sse` 和 POST `/runner/online`；`POST /operator-event` 同时 forward 到 daemon `/runner/event` |
| `scripts/runner_client.py` | `notify()` 改为通过 daemon SSE（兼容旧 HTTP 模式作为 fallback） |
| `scripts/operator_monitor.py` | 删除 HTTP poll loop；本地文件监听路径保留 |
| `scripts/watch_emerge.py` | 新文件；tail events.jsonl（全局/per-runner/local 三种模式） |
| `scripts/watch_patterns.py` | 改为 shim，调用 `watch_emerge.py --runner-profile` |
| `scripts/watch_pending.py` | 改为 shim，调用 `watch_emerge.py` |
| `scripts/repl_admin.py` | 新增 `GET /api/control-plane/monitors`；`_sse_broadcast` 增加 `monitors_updated` 事件 |
| `scripts/cockpit_shell.html` | 新增 Monitors tab；SSE 监听 `monitors_updated` |
| `scripts/pending_actions.py` | 新增 `format_runner_discovered`、`format_runner_event` 等 formatter |
| `.claude-plugin/plugin.json` | `mcpServers.emerge` 改为 `url: "http://localhost:8789/mcp"` |
| `hooks/session_start.py` | 升级为 ensure-running launcher |
| `commands/cockpit.md` | 简化：只打印 URL + 启动 watch_emerge.py |
| `commands/monitor.md` | 更新 watcher prompt：使用 `runner_notify` MCP tool 代替直接调 HTTP |
| `README.md` | 更新架构图、组件表、runner 操作章节 |
| `CLAUDE.md` | 更新 daemon 架构、事件流、hook 行为、notification delivery 章节 |

---

## 7. 向后兼容

- `watch_patterns.py` / `watch_pending.py` 保留为 shim，现有 cockpit 步骤不中断
- `runner_client.notify()` 降级策略：先尝试 daemon SSE 路径，失败则 fallback 到直接 HTTP `/notify`（保持 Phase 1 watcher 可用）
- Daemon 检测 plugin.json `command` vs `url`：开发模式下保留 stdio 兼容路径

---

## 8. 实现阶段

### Phase A — Daemon HTTP（前置）
- `DaemonHTTPServer` 基础框架（`/mcp` 端点，MCP HTTP transport）
- `--ensure-running` 模式 + PID file
- `plugin.json` 迁移
- `session_start.py` 升级

### Phase B — Runner SSE Push
- `remote_runner.py` SSE client + `/runner/online` + `/runner/event` forward
- Daemon `/runner/sse` + `/runner/event` + `/runner/online` + `_connected_runners`
- 统一事件流 `events-{profile}.jsonl`

### Phase C — Popup via SSE
- Daemon `/runner/popup-result` + Future correlation
- `runner_notify` MCP tool
- `runner_client.notify()` 改为 MCP tool 调用

### Phase D — 统一事件流 + watch_emerge.py
- `watch_emerge.py` 实现（三种模式）
- 旧 watch 脚本改为 shim

### Phase E — Cockpit 自动化 + Monitors Tab
- Daemon 自动启动 cockpit + 注入 controls
- `/api/control-plane/monitors` 端点
- cockpit_shell.html Monitors tab
- `commands/cockpit.md` 简化
