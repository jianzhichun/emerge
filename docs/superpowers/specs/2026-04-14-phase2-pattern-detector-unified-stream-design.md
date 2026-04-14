# Phase 2: PatternDetector 统一流 + daemon/cockpit 进程合并

**Date**: 2026-04-14  
**Status**: Approved  
**Scope**: PatternDetector 接入 runner push；统一事件流输出；daemon/cockpit 进程合并消除文件 IPC

---

## Overview

Phase 1+ 完成了 runner SSE push 基础设施和统一事件流文件格式，但存在三个关键缺口：

1. **PatternDetector 未接入 runner push 路径** — `events-{profile}.jsonl` 永远没有 `pattern_alert`，agents-team watcher 永远静默
2. **cockpit 与 daemon 是独立进程** — `monitors_updated` SSE 依赖文件 mtime 轮询，`runner-monitor-state.json` 是唯一耦合点，但需要绕行三层（daemon 写文件 → repl_admin 轮询 → SSE 广播）
3. **`_push_pattern` 间接层** — 本地事件检测结果写旧格式 `pattern-alerts.json`，不写 `events-local.jsonl`

Phase 2 一次性修复：PatternDetector 内嵌于事件到达点，cockpit 变成 daemon 进程内的嵌入式 HTTP server，直接调 broadcast。

---

## 设计原则

- **事件在哪里到达，就在哪里检测**：消除 OperatorMonitor → `push_fn` → `_push_pattern` 间接层
- **进程内共享，消除文件 IPC**：`_connected_runners` 内存直接读，无需 `runner-monitor-state.json` 作通路
- **不破坏 CLI 工具**：`runner-monitor-state.json` 继续写（调试/CLI 用），但 cockpit 从内存读

---

## 1. 架构总览

```
emerge_daemon 进程
  ├─ EmergeDaemon                         MCP 工具实现，span/pipeline/goal 状态
  │    └─ _operator_monitor: OperatorMonitor   本地文件事件检测
  ├─ DaemonHTTPServer (port 8789)          MCP HTTP + runner 端点
  │    └─ _connected_runners              runner 连接状态（内存）
  │    └─ _detector, _runner_event_buffers 新增：runner push 检测
  └─ CockpitHTTPServer (port 0, dynamic)   cockpit UI（进程内嵌入）
       └─ 直接读 DaemonHTTPServer._connected_runners
       └─ broadcast() 直接推 SSE，无 IPC

事件流：

POST /runner/event → DaemonHTTPServer._on_runner_event()
  ├─ [已有] 写 operator-events/{machine_id}/events.jsonl
  ├─ [已有] 写 events-{profile}.jsonl  type=runner_event
  ├─ [新增] buffer + PatternDetector.ingest()
  └─ [新增] 若检测到 pattern:
       ├─ 写 events-{profile}.jsonl  type=pattern_alert  ← watcher Monitor 读此
       ├─ 更新 _connected_runners[profile]["last_alert"]
       ├─ _write_monitor_state()   继续写文件（调试用）
       └─ daemon._cockpit_server.broadcast({"monitors_updated": True})  直接推 SSE

本地文件变化 → EventRouter → OperatorMonitor.process_local_file()
  └─ [修改] 写 events-local.jsonl  type=local_pattern_alert
```

---

## 2. CockpitHTTPServer 类（repl_admin.py）

### 2.1 类定义

新增到 `scripts/repl_admin.py`，提取现有 `cmd_serve()` 的 HTTP server 逻辑：

```python
class CockpitHTTPServer:
    def __init__(
        self,
        daemon: Any,          # EmergeDaemon 引用
        port: int = 0,
        repl_root: Path | None = None,
        connector_root: Path | None = None,
    ) -> None:
        self._daemon = daemon
        self._port = port
        self._repl_root = repl_root or _resolve_repl_root()
        self._connector_root = connector_root or _resolve_connector_root()
        self._server: socketserver.TCPServer | None = None
        self._thread: threading.Thread | None = None
        # 从模块级迁入的状态
        self._sse_clients: list = []
        self._sse_lock = threading.Lock()
        self._injected_html: dict[str, list[dict]] = {}
        self._inject_lock = threading.Lock()
        self.url: str | None = None

    def start(self) -> str:
        """启动 cockpit HTTP server，返回 URL。"""
        handler = _make_cockpit_handler(self)
        self._server = _ReuseAddrTCPServer(("127.0.0.1", self._port), handler)
        port = self._server.server_address[1]
        self.url = f"http://localhost:{port}"
        # 写 cockpit.pid 和 cockpit-url.txt
        pid_data = {"pid": os.getpid(), "port": port, "cwd": str(Path.cwd())}
        _cockpit_pid_path(self._repl_root).write_text(
            json.dumps(pid_data), encoding="utf-8"
        )
        (self._repl_root / "cockpit-url.txt").write_text(self.url, encoding="utf-8")
        atexit.register(self.stop)
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="CockpitHTTPServer"
        )
        self._thread.start()
        return self.url

    def stop(self) -> None:
        if self._server:
            try:
                self._server.shutdown()
            except Exception:
                pass
        _cockpit_pid_path(self._repl_root).unlink(missing_ok=True)

    def broadcast(self, event: dict) -> None:
        """直接推 SSE 给所有已连接的浏览器客户端。"""
        data = f"data: {json.dumps(event)}\n\n".encode()
        with self._sse_lock:
            dead = []
            for wfile in self._sse_clients:
                try:
                    wfile.write(data)
                    wfile.flush()
                except OSError:
                    dead.append(wfile)
            for wfile in dead:
                self._sse_clients.remove(wfile)

    def get_monitor_data(self) -> dict:
        """从 daemon 内存读 runner 状态，零文件 I/O。"""
        hsrv = getattr(self._daemon, "_http_server", None)
        if not hsrv:
            return {"runners": [], "team_active": False}
        with hsrv._runners_lock:
            items = list(hsrv._connected_runners.items())
        runners = [
            {
                "runner_profile": profile,
                "connected": True,
                "connected_at_ms": info.get("connected_at_ms", 0),
                "last_event_ts_ms": info.get("last_event_ts_ms", 0),
                "machine_id": info.get("machine_id", ""),
                "last_alert": info.get("last_alert"),
            }
            for profile, info in items
        ]
        return {"runners": runners, "team_active": len(runners) > 0}
```

### 2.2 `_make_cockpit_handler(cockpit)` 工厂

当前 `_CockpitHandler(BaseHTTPRequestHandler)` 类通过模块级全局访问 `_sse_clients`、`_COCKPIT_INJECTED_HTML`。改为工厂函数，返回以 `cockpit` 为 closure 的子类：

```python
def _make_cockpit_handler(cockpit: CockpitHTTPServer):
    class _Handler(_CockpitHandler):
        _cockpit = cockpit
    return _Handler
```

`_CockpitHandler` 内所有 `_sse_clients` → `self._cockpit._sse_clients`，`_COCKPIT_INJECTED_HTML` → `self._cockpit._injected_html`，`_sse_broadcast()` → `self._cockpit.broadcast()`。

`/api/control-plane/monitors` handler 改为：
```python
self._send_json(self._cockpit.get_monitor_data())
```

### 2.3 保留向后兼容的模块级函数

`cmd_serve()` 保留（供直接 CLI 运行 `python repl_admin.py serve`），内部改为：
```python
def cmd_serve(port=0, open_browser=False, ...):
    # 独立运行时无 daemon 引用，创建一个 stub daemon
    cockpit = CockpitHTTPServer(daemon=_StandaloneDaemonStub(), port=port)
    url = cockpit.start()
    ...
```

`_StandaloneDaemonStub` 是一个极简 object，`_http_server = None`。`get_monitor_data()` 在 `_http_server is None` 时 fallback 到读 `runner-monitor-state.json`（旧行为）。

`_sse_broadcast()` 模块级函数保留为 shim，但 Phase 2 后不再被主路径调用。

---

## 3. DaemonHTTPServer 变更（runner push 检测）

### 3.1 新增字段

```python
# scripts/daemon_http.py — DaemonHTTPServer.__init__

from collections import deque
from scripts.pattern_detector import PatternDetector

self._detector = PatternDetector()
self._runner_event_buffers: dict[str, deque] = {}
self._runner_buffers_lock = threading.Lock()
```

### 3.2 `_on_runner_event` 追加检测逻辑

在现有写文件逻辑后追加：

```python
if runner_profile:
    window_ms = PatternDetector.FREQ_WINDOW_MS
    with self._runner_buffers_lock:
        buf = self._runner_event_buffers.setdefault(runner_profile, deque())
        buf.append({
            **{k: v for k, v in payload.items() if k != "runner_profile"},
            "ts_ms": ts_ms,
            "machine_id": machine_id or runner_profile,
        })
        while buf and ts_ms - buf[0].get("ts_ms", 0) > window_ms:
            buf.popleft()
        snapshot = list(buf)

    summaries = self._detector.ingest(snapshot)
    for summary in summaries:
        try:
            stage = self._daemon._span_tracker.get_policy_status(
                summary.intent_signature
            )
        except Exception:
            stage = summary.policy_stage  # fallback: "explore"

        alert = {
            "type": "pattern_alert",
            "ts_ms": ts_ms,
            "runner_profile": runner_profile,
            "stage": stage,
            "intent_signature": summary.intent_signature,
            "meta": {
                "occurrences": summary.occurrences,
                "window_minutes": round(summary.window_minutes, 1),
                "machine_ids": summary.machine_ids,
                "detector_signals": summary.detector_signals,
            },
        }
        self._append_event(
            self._state_root / f"events-{runner_profile}.jsonl", alert
        )
        with self._runners_lock:
            if runner_profile in self._connected_runners:
                self._connected_runners[runner_profile]["last_alert"] = {
                    "stage": stage,
                    "intent_signature": summary.intent_signature,
                    "ts_ms": ts_ms,
                }

    if summaries:
        self._write_monitor_state()
        # 直接推 SSE 给 cockpit 浏览器客户端，无任何文件 IPC
        cockpit = getattr(self._daemon, "_cockpit_server", None)
        if cockpit is not None:
            cockpit.broadcast({"monitors_updated": True})
```

### 3.3 runner 上下线也触发 broadcast

`_on_runner_online` 末尾追加：
```python
cockpit = getattr(self._daemon, "_cockpit_server", None)
if cockpit is not None:
    cockpit.broadcast({"monitors_updated": True})
```

SSE 断线清理时同样触发（`_handle_runner_sse` 的断线 finally 块）。

### 3.4 `_write_monitor_state` 修正 `team_active`

```python
"team_active": len(self._connected_runners) > 0,
```

---

## 4. EmergeDaemon 变更

### 4.1 删除 `_push_pattern` 和 `_build_explore_message`

runner push 路径由 DaemonHTTPServer 内联处理。本地路径由 OperatorMonitor 直接写文件。两个方法不再被调用，删除。

### 4.2 `start_operator_monitor` 传入 `state_root`

```python
self._operator_monitor = OperatorMonitor(
    machines={},
    poll_interval_s=poll_s,
    event_root=Path.home() / ".emerge" / "operator-events",
    adapter_root=Path.home() / ".emerge" / "adapters",
    state_root=self._state_root,   # 新增，用于写 events-local.jsonl
)
```

### 4.3 `run_http()` 进程内启动 cockpit

```python
def run_http(port: int = 8789) -> None:
    daemon = EmergeDaemon()
    daemon._http_mode = True
    daemon.start_operator_monitor()
    daemon.start_event_router()

    from scripts.daemon_http import DaemonHTTPServer
    srv = DaemonHTTPServer(daemon=daemon, port=port, ...)
    daemon._http_server = srv
    srv.start()

    # 进程内启动 cockpit，不 spawn 子进程
    from scripts.repl_admin import CockpitHTTPServer
    cockpit = CockpitHTTPServer(daemon=daemon, port=0)
    url = cockpit.start()
    daemon._cockpit_server = cockpit
    print(f"[emerge] Cockpit: {url}", flush=True)

    atexit.register(srv.stop)
    atexit.register(cockpit.stop)
    threading.Event().wait()   # block until SIGTERM/KeyboardInterrupt
```

### 4.4 删除 `_ensure_cockpit()`

`cmd_serve_stop()` 在 `repl_admin.py` 中继续支持 standalone CLI 模式（CLI 进程用 cockpit.pid kill），合并模式下进程退出即停止。

---

## 5. OperatorMonitor 变更

### 5.1 新增 `state_root` 参数，移除 `push_fn`

```python
class OperatorMonitor(threading.Thread):
    def __init__(
        self,
        machines: dict,
        poll_interval_s: float = 5.0,
        event_root: Path | None = None,
        adapter_root: Path | None = None,
        state_root: Path | None = None,   # 新增
    ) -> None:
        ...
        self._state_root = state_root or (Path.home() / ".emerge" / "repl")
        # push_fn 参数移除
```

### 5.2 `process_local_file` 直接写 `events-local.jsonl`

```python
import json as _json, time as _time
ts_ms = int(_time.time() * 1000)
events_local = self._state_root / "events-local.jsonl"
events_local.parent.mkdir(parents=True, exist_ok=True)
alert = {
    "type": "local_pattern_alert",
    "ts_ms": ts_ms,
    "stage": summary.policy_stage,   # "explore"（PatternDetector 默认）
    "intent_signature": summary.intent_signature,
    "meta": {
        "occurrences": summary.occurrences,
        "window_minutes": round(summary.window_minutes, 1),
        "machine_ids": summary.machine_ids,
        "detector_signals": summary.detector_signals,
        "app": summary.context_hint.get("app", ""),
    },
}
with events_local.open("a", encoding="utf-8") as f:
    f.write(_json.dumps(alert, ensure_ascii=False) + "\n")
```

---

## 6. 废弃的文件、函数、格式

| 废弃 | 替代 |
|---|---|
| `EmergeDaemon._push_pattern()` | DaemonHTTPServer 内联 + OperatorMonitor 直写 |
| `EmergeDaemon._build_explore_message()` | watcher 读结构化字段，不再需要 message 字符串 |
| `EmergeDaemon._ensure_cockpit()` | `CockpitHTTPServer(daemon).start()` 进程内 |
| `OperatorMonitor.push_fn` 参数 | 移除 |
| `pattern-alerts-{profile}.json` | `events-{profile}.jsonl` type=pattern_alert |
| `pattern-alerts.json` | `events-local.jsonl` type=local_pattern_alert |
| `runner-monitor-state.json` 作为 IPC 通路 | 文件继续写（调试用），cockpit 从内存读 |
| repl_admin 模块级 `_sse_clients`、`_COCKPIT_INJECTED_HTML` | `CockpitHTTPServer` 实例变量 |

---

## 7. 文件变更列表

| 文件 | 变更类型 | 核心改动 |
|---|---|---|
| `scripts/repl_admin.py` | 重要修改 | 新增 `CockpitHTTPServer` 类；`_make_cockpit_handler(cockpit)` 工厂；`/api/control-plane/monitors` 改为内存读；模块级全局迁入实例；`_StandaloneDaemonStub` 兼容 CLI |
| `scripts/daemon_http.py` | 重要修改 | 新增 `_detector`、`_runner_event_buffers`；`_on_runner_event` 追加检测逻辑；`_on_runner_online`/断线触发 `cockpit.broadcast`；`team_active` 修正 |
| `scripts/emerge_daemon.py` | 重要修改 | 删除 `_push_pattern`、`_build_explore_message`、`_ensure_cockpit`；`run_http()` 进程内启 `CockpitHTTPServer`；`start_operator_monitor` 传 `state_root` |
| `scripts/operator_monitor.py` | 中等修改 | 移除 `push_fn`；新增 `state_root`；`process_local_file` 直写 `events-local.jsonl` |
| `scripts/pending_actions.py` | 小修改 | 确认 `format_local_pattern_alert` formatter 存在 |
| `CLAUDE.md` | 文档更新 | 更新 Runner push、Cockpit control plane、Key Invariants 章节 |
| `README.md` | 文档更新 | 更新架构图、组件表 |
| `tests/test_daemon_http.py` | 新增测试 | runner push → pattern_alert 检测；broadcast 调用验证 |
| `tests/test_operator_monitor.py` | 更新测试 | process_local_file → events-local.jsonl |
| `tests/test_repl_admin.py` | 新增测试 | CockpitHTTPServer 启动；get_monitor_data 内存读 |

---

## 8. 测试要点

1. `_on_runner_event` 推送 ≥3 条相同 app/event_type 事件 → `events-{profile}.jsonl` 出现 `type=pattern_alert`，`stage` 来自 SpanTracker
2. `_connected_runners[profile]["last_alert"]` 在检测后更新
3. `_write_monitor_state` 写入文件后 `team_active=True`（有连接 runner 时）
4. `cockpit.broadcast` 在 pattern 检测、runner 上线、runner 断线时被调用
5. `CockpitHTTPServer.get_monitor_data()` 直接读 `_connected_runners` 内存，与 `runner-monitor-state.json` 内容一致
6. `process_local_file` 检测结果写入 `events-local.jsonl`（type=local_pattern_alert）
7. `CockpitHTTPServer` standalone 模式（`_http_server=None`）fallback 读文件

---

## 9. 不在 Phase 2 范围内

- 本地 `local_pattern_alert` 的 canary/stable stage 查询（本地无 watcher agent，YAGNI）
- agents-team 动态 runner 上线自动 spawn watcher（Phase 3）
- `_inject_controls`（daemon 自动注入 connector HTML 到 cockpit，推迟）
