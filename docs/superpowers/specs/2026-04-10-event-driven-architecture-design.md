# Event-Driven Architecture — Design Spec

**Date:** 2026-04-10  
**Status:** Approved for implementation  
**Scope:** emerge daemon, emerge_sync, cockpit (repl_admin.py)

---

## Background

emerge 的现有架构基于多处文件轮询：`PendingActionMonitor`（2s）、`OperatorMonitor._poll_local`（5s）、`emerge_sync` poll loop（sleep 10s）、cockpit wait-for-submit（sleep 0.5s）。这些轮询导致：

1. **CC 阻塞**：cockpit wait-for-submit 让 CC 完全挂起，最长等到 deadline 超时
2. **高延迟**：提交动作最多等 0.5–5s 才被感知
3. **daemon thread 膨胀**：4 个独立 polling thread，维护成本高
4. **配置变更需重启**：poll_interval 等参数读一次后不热重载

本次重构用三个 CC 新能力彻底替换上述模式：

- **watchdog**（OS 级文件事件：macOS FSEvents / Linux inotify / Windows ReadDirectoryChangesW）
- **MCP ElicitRequest**（协议版本 2025-03-26，服务端向客户端请求结构化输入）
- **Server-Sent Events**（替换 cc-listening.json 心跳文件机制）

---

## 架构总览

```
现有架构
──────────────────────────────────────────────
[Browser] ──write──▶ pending-actions.json
                          ▲ poll 0.5s
[CC] ──blocks──▶ [repl_admin _wait_for_submit]
                          ▲ poll 2s
                 [PendingActionMonitor thread]
                          ▲ poll 5s
                 [OperatorMonitor._poll_local]──▶ events.jsonl
                          ▲ sleep 10s
                 [emerge_sync poll loop]──▶ sync-queue.jsonl

新架构
──────────────────────────────────────────────
[Browser] ──POST──▶ /api/pending-actions/submit (无等待)
                   ──SSE──▶ /api/sse/status (在线状态推送)

[CC] ──elicitations/create──▶ 原生弹窗 ──▶ 用户填写 ──▶ 立即返回

[EventRouter]  ← watchdog inotify/FSEvents (亚毫秒)
  ├── watch sync-queue.jsonl      →  _run_stable_events()
  ├── watch operator-events/**/   →  PatternDetector.ingest()
  └── watch <session>/            →  on_created(pending-actions.json) → MCP push

[Daemon ThreadPool]  ← tool handlers 在子线程执行
  └── _elicit() 挂在 Event.wait()，主 stdin loop 继续路由消息
```

**删除的组件：**
- `PendingActionMonitor` class（~70 行）
- `OperatorMonitor._poll_local`（~50 行）
- `repl_admin._wait_for_submit` 阻塞循环（~25 行）
- `_write_cc_listening` + cc-listening.json 心跳机制（~50 行）

**新增的组件：**
- `scripts/event_router.py`（EventRouter，~120 行）
- `/api/sse/status` SSE endpoint（cockpit，~30 行）
- `_elicit()` helper + ThreadPool stdio 架构（daemon，~80 行）

---

## 组件设计

### 1. EventRouter（`scripts/event_router.py`）

watchdog `Observer` 包装器，统一管理所有文件系统事件订阅。

**接口：**

```python
class EventRouter:
    def __init__(self, handlers: dict[Path, Callable[[Path], None]]) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
    @property
    def mode(self) -> Literal["inotify", "polling"]: ...
```

**行为：**

- 启动时先同步 drain 一次所有被监视文件（处理 EventRouter 启动前已积压的事件），再交给 watchdog 接管
- watchdog 未安装时自动 fallback 到 polling 模式（保留原有 polling 线程逻辑作降级路径），启动日志记录 `mode=polling`
- 每个 handler 在 watchdog 的回调线程执行；handler 内部需自行加锁（现有逻辑已有锁保护）

**使用方（daemon）：**

```python
router = EventRouter({
    sync_queue_path(): lambda _: _run_stable_events(),
    event_root / "**" / "events.jsonl": lambda p: _ingest_local_events(p),
    state_root / "pending-actions.json": lambda _: self._on_pending_actions(),
})
router.start()
```

**使用方（emerge_sync）：**

```python
router = EventRouter({sync_queue_path(): _run_stable_events})
router.start()
# periodic pull 改为 threading.Timer(poll_interval, _schedule_pull)
```

### 2. Daemon stdio 架构升级

**前置条件**：ElicitRequest 要求 tool handler 在子线程运行，主 stdin loop 持续路由。

**新架构：**

```
stdin loop（主线程）
  │
  ├── 解析 JSON-RPC 消息
  ├── tool call → ThreadPoolExecutor → handler 在子线程运行
  │                                        └── _elicit() → 写 stdout
  │                                            Event.wait(timeout=60)
  │
  ├── elicitations response → 查 _elicit_events → Event.set()
  │
  └── 其他消息（ping、logging/setLevel 等）→ 同步处理
```

**相关数据结构（EmergeDaemon 实例变量）：**

```python
self._elicit_events:  dict[str, threading.Event] = {}
self._elicit_results: dict[str, dict] = {}
self._executor = ThreadPoolExecutor(max_workers=4)
```

**`handle_jsonrpc` 路由新增：**

```python
# 收到 elicitations response（id 在 correlation map 中）
if req_id and req_id in self._elicit_events:
    self._elicit_results[req_id] = params.get("content", {})
    self._elicit_events.pop(req_id).set()
    return None  # 不需要 response
```

### 3. ElicitRequest Helper

**协议声明（2 处改动）：**

```python
"protocolVersion": "2025-03-26",   # 从 2024-11-05 升级
"capabilities": {
    "tools": {},
    "resources": {"subscribe": False},
    "prompts": {},
    "logging": {},
    "elicitation": {},              # 新增
},
```

**Helper：**

```python
def _elicit(
    self,
    message: str,
    schema: dict,
    timeout: float = 60.0,
) -> dict | None:
    """Send elicitations/create, block current (sub)thread until response."""
    elicit_id = f"elicit-{uuid.uuid4().hex[:8]}"
    event = threading.Event()
    self._elicit_events[elicit_id] = event
    self._write_mcp_push({
        "jsonrpc": "2.0",
        "id": elicit_id,
        "method": "elicitations/create",
        "params": {"message": message, "requestedSchema": schema},
    })
    fired = event.wait(timeout=timeout)
    if not fired:
        self._elicit_events.pop(elicit_id, None)
        self._elicit_results.pop(elicit_id, None)
        return None
    return self._elicit_results.pop(elicit_id, None)
```

**使用场景：**

| Tool | message | schema |
|------|---------|--------|
| `icc_span_approve` | "确认激活 pipeline `{name}`？移出 _pending/ 并启用桥接。" | `{confirmed: boolean}` |
| `icc_hub resolve` | "选择冲突解决策略：" | `{resolution: enum[ours, theirs, skip]}` |
| `icc_reconcile` | "确认 delta `{id}` 的处置结果：" | `{outcome: enum[confirm, correct, retract]}` |

### 4. Cockpit 提交路径重构

**删除：**

- `_wait_for_submit(deadline)` 函数（`repl_admin.py`）
- `_write_cc_listening(deadline)` + `_cc_listening_path()` + cc-listening.json 写入
- `start_pending_monitor()` / `stop_pending_monitor()` + `PendingActionMonitor` class（daemon）

**新路径：**

```
Browser POST /api/pending-actions/submit
  → 写 pending-actions.json（路径不变，atomically）
  → EventRouter(inotify) 亚毫秒触发
  → _on_pending_actions() 读文件 → _write_mcp_push(notifications/claude/channel)
  → CC 收到 notification，完全异步响应
```

`/api/pending-actions/submit` endpoint 直接写文件后返回 `{"ok": true}`，不等待 CC 响应。

### 5. Cockpit SSE（替换 cc-listening.json）

**新 endpoint**（加入现有 HTTPServer）：

```python
GET /api/sse/status
Content-Type: text/event-stream
Cache-Control: no-cache
```

**事件流：**

```
data: {"status": "online", "pid": 12345, "ts_ms": 1712345678000}\n\n
# 连接保持；daemon 关闭时 atexit 推送：
data: {"status": "offline"}\n\n
```

**Browser 端：**

```javascript
const sse = new EventSource('/api/sse/status');
sse.onmessage = e => updateCCStatusIndicator(JSON.parse(e.data));
sse.onerror = () => setTimeout(() => reconnectSSE(), backoff());
```

---

## 数据流

### Cockpit 提交（重构后）

```
1. User submits in browser
2. POST /api/pending-actions/submit → 200 OK (immediate)
3. pending-actions.json written atomically
4. EventRouter fires on_created callback (< 1ms on macOS/Linux)
5. _on_pending_actions() reads + processes file
6. _write_mcp_push(notifications/claude/channel) → CC
7. CC handles notification asynchronously
```

### ElicitRequest 流（icc_span_approve）

```
1. CC calls icc_span_approve
2. ThreadPool submits handler to worker thread
3. Handler calls _elicit("确认激活 pipeline？", schema)
4. _elicit writes elicitations/create to stdout; worker thread blocks on Event
5. Main stdin loop continues running
6. CC receives elicitations/create → shows native dialog
7. User clicks "确认"
8. CC sends elicitations response to daemon stdin
9. Main loop receives response → Event.set()
10. Worker thread unblocks → continues approve logic
11. Returns tool result to CC
```

---

## 错误处理

| 失败场景 | 处理 |
|---------|------|
| `watchdog` 未安装 | 启动时 `ImportError` → 打印安装指引 + fallback 到原有 polling 线程 |
| EventRouter 启动失败 | daemon 记录 WARNING，polling fallback 自动接管 |
| ElicitRequest 超时（60s） | `_elicit()` 返回 `None` → tool 返回 error "elicitation timed out, operation cancelled" |
| ThreadPool 子线程异常 | `Future.exception()` 捕获 → 返回标准 MCP error response，主循环不受影响 |
| SSE 连接断开 | Browser 指数退避重连（3s/6s/12s），`/api/sse/status` 无状态，重连立即恢复 |
| Stale elicitations response | `_elicit_events` 中无对应 id → 忽略 |
| EventRouter 启动前已积压事件 | 启动前先同步 drain 一次队列文件，再交给 watchdog |

---

## 测试策略

### EventRouter 单元测试（`tests/test_event_router.py`）

```python
def test_event_router_dispatches_callback():
    called = []
    router = EventRouter({Path("/fake/queue.jsonl"): lambda p: called.append(p)})
    router._dispatch(Path("/fake/queue.jsonl"))
    assert len(called) == 1

def test_event_router_fallback_when_watchdog_missing():
    with patch.dict("sys.modules", {"watchdog": None}):
        router = EventRouter({})
        router.start()
        assert router.mode == "polling"
```

### ElicitRequest 集成测试（`tests/test_mcp_tools_integration.py`）

```python
def test_span_approve_uses_elicitation(daemon):
    with patch.object(daemon, "_elicit", return_value={"confirmed": True}) as mock_elicit:
        result = daemon.call_tool("icc_span_approve", {"intent_signature": "gmail:read:fetch"})
    mock_elicit.assert_called_once()
    assert result["ok"] == True

def test_span_approve_elicitation_timeout(daemon):
    with patch.object(daemon, "_elicit", return_value=None):
        result = daemon.call_tool("icc_span_approve", {"intent_signature": "gmail:read:fetch"})
    assert "timed out" in result["error"].lower()
```

### ThreadPool 并发测试

```python
def test_concurrent_tool_calls_dont_corrupt_stdout(daemon):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(daemon.call_tool, "icc_exec", {
            "intent_signature": f"test:sig:{i}", "code": "result = {}"
        }) for i in range(5)]
        results = [f.result() for f in futures]
    assert all(r is not None for r in results)
```

### Cockpit SSE 测试（`tests/test_cockpit_sse.py`）

```python
def test_sse_status_emits_online_on_connect(cockpit_client):
    with cockpit_client.stream("GET", "/api/sse/status") as stream:
        first_line = next(l for l in stream.iter_lines() if l.startswith("data:"))
        data = json.loads(first_line[5:])
    assert data["status"] == "online"
    assert "pid" in data
```

---

## 实现顺序

1. **EventRouter**（独立新文件，无破坏性改动）
2. **emerge_sync 接入 EventRouter**（替换 `sleep(10)` 内层，Timer 替换 pull 周期）
3. **daemon stdio ThreadPool 升级**（最复杂，先做再接入 ElicitRequest）
4. **协议版本升级 + `_elicit()` helper**
5. **icc_span_approve / icc_hub resolve / icc_reconcile 接入 ElicitRequest**
6. **Cockpit SSE endpoint**
7. **Cockpit 提交路径重构（移除阻塞等待）**
8. **删除 PendingActionMonitor + cc-listening.json 相关代码**

---

## 影响范围

| 文件 | 变更类型 |
|------|---------|
| `scripts/event_router.py` | 新增 |
| `scripts/emerge_daemon.py` | 改动（ThreadPool、_elicit、协议版本、删 PendingActionMonitor） |
| `scripts/emerge_sync.py` | 改动（EventRouter 替换 sleep loop，Timer 替换 pull 周期） |
| `scripts/operator_monitor.py` | 改动（删 _poll_local，接入 EventRouter） |
| `scripts/repl_admin.py` | 改动（删 _wait_for_submit、cc-listening；加 SSE endpoint） |
| `tests/test_event_router.py` | 新增 |
| `tests/test_cockpit_sse.py` | 新增 |
| `tests/test_mcp_tools_integration.py` | 新增测试用例 |
| `README.md` | 更新架构图、组件表 |
| `CLAUDE.md` | 更新 Architecture + Key Invariants |
