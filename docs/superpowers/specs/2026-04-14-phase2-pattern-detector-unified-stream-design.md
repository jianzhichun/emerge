# Phase 2: PatternDetector Unified Stream Design

**Date**: 2026-04-14  
**Status**: Approved  
**Scope**: PatternDetector 接入 runner push 路径；统一事件流输出；cockpit 实时 SSE；`_push_pattern` 清除

---

## Overview

Phase 1+ 完成了 runner SSE push 基础设施和统一事件流文件格式，但 PatternDetector 从未被接入 runner push 路径。结果：

- `events-{profile}.jsonl` 只有 `runner_event` 记录，永远没有 `pattern_alert`
- agents-team watcher 的 Monitor 永远静默
- `events-local.jsonl` 从未被写入（本地检测结果仍走旧 `pattern-alerts.json`）
- cockpit Monitors tab 的 `team_active` 永远是 `false`
- `monitors_updated` SSE 没有 IPC 通路

Phase 2 修复所有这些，消除 `_push_pattern` 间接层，PatternDetector 直接内嵌于事件到达点。

---

## 设计原则

**事件在哪里到达，就在哪里检测。消除中间层。**

不考虑向后兼容性：`pattern-alerts-{profile}.json` 和 `pattern-alerts.json` 文件格式废弃，统一输出到 `events-*.jsonl` 流。

---

## 1. 架构总览

```
POST /runner/event
  → DaemonHTTPServer._on_runner_event()
       ├─ [已有] 写 ~/.emerge/operator-events/{machine_id}/events.jsonl
       ├─ [已有] 写 events-{profile}.jsonl  (type=runner_event)
       ├─ [新增] 更新 per-runner deque buffer，裁剪超窗口事件
       ├─ [新增] PatternDetector.ingest(buffer_snapshot)
       └─ for each summary:
            ├─ [新增] daemon._span_tracker.get_policy_status(intent_signature) → stage
            ├─ [新增] 写 events-{profile}.jsonl (type=pattern_alert)
            ├─ [新增] 更新 _connected_runners[profile]["last_alert"]
            └─ [新增] _write_monitor_state()  → SSE 链路触发

EventRouter（本地文件变化）
  → daemon._on_local_operator_events()
       → OperatorMonitor.process_local_file()
            ├─ [已有] PatternDetector.ingest()
            └─ [修改] 写 events-local.jsonl (type=local_pattern_alert)  ← 不再写 pattern-alerts.json

runner-monitor-state.json 文件变化
  → repl_admin 后台 mtime 轮询线程（1s）
       └─ _sse_broadcast({"monitors_updated": True})
```

---

## 2. DaemonHTTPServer 变更

### 2.1 新增字段

```python
# scripts/daemon_http.py — DaemonHTTPServer.__init__

from collections import deque
from scripts.pattern_detector import PatternDetector

self._detector = PatternDetector()
self._runner_event_buffers: dict[str, deque] = {}   # runner_profile → deque[event_dict]
self._runner_buffers_lock = threading.Lock()
```

### 2.2 `_on_runner_event` 新增检测逻辑

在现有写文件逻辑之后追加：

```python
# 仅在有效 runner_profile 时运行检测
if runner_profile:
    now_ms = ts_ms
    window_ms = PatternDetector.FREQ_WINDOW_MS
    with self._runner_buffers_lock:
        buf = self._runner_event_buffers.setdefault(runner_profile, deque())
        # 注入 ts_ms 和 runner_profile（供 PatternDetector 使用）
        event_with_meta = {**{k: v for k, v in payload.items()
                               if k not in ("runner_profile",)},
                           "ts_ms": ts_ms,
                           "machine_id": machine_id or runner_profile}
        buf.append(event_with_meta)
        # 裁剪超出时间窗口的旧事件
        while buf and now_ms - buf[0].get("ts_ms", 0) > window_ms:
            buf.popleft()
        snapshot = list(buf)

    summaries = self._detector.ingest(snapshot)
    for summary in summaries:
        # 从 SpanTracker 查真实 policy stage
        try:
            stage = self._daemon._span_tracker.get_policy_status(summary.intent_signature)
        except Exception:
            stage = summary.policy_stage  # fallback: "explore"

        alert_event = {
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
        self._append_event(self._state_root / f"events-{runner_profile}.jsonl", alert_event)

        with self._runners_lock:
            if runner_profile in self._connected_runners:
                self._connected_runners[runner_profile]["last_alert"] = {
                    "stage": stage,
                    "intent_signature": summary.intent_signature,
                    "ts_ms": ts_ms,
                }
    if summaries:
        self._write_monitor_state()
```

### 2.3 `_write_monitor_state` 修正 `team_active`

```python
"team_active": len(self._connected_runners) > 0,   # 原来硬编码 False
```

---

## 3. EmergeDaemon 变更

### 3.1 `_push_pattern` 删除

`_push_pattern` 不再被调用（runner push 路径由 DaemonHTTPServer 直接处理，本地路径由 OperatorMonitor 直接写文件）。方法删除，`OperatorMonitor` 构造时的 `push_fn=self._push_pattern` 改为 `push_fn=None`（或移除参数，见第 4 节）。

### 3.2 `_build_explore_message` 删除

watcher 从 `events-{profile}.jsonl` 的结构化字段读取 `stage`/`intent_signature`/`meta`，不依赖 `message` 字符串。方法可删除。

---

## 4. OperatorMonitor 变更

### 4.1 `push_fn` 参数移除

`push_fn` 的唯一用途是 `_push_pattern` 回调，Phase 2 后不再需要。移除 `push_fn` 参数，OperatorMonitor 直接写 `events-local.jsonl`。

构造时需注入 `state_root`（用于写事件文件）：

```python
class OperatorMonitor(threading.Thread):
    def __init__(
        self,
        machines: dict,           # 保留 API 兼容，传入 {} 即可
        poll_interval_s: float = 5.0,
        event_root: Path | None = None,
        adapter_root: Path | None = None,
        state_root: Path | None = None,   # 新增，用于写 events-local.jsonl
    ) -> None:
```

### 4.2 `process_local_file` 修改

将 `self._push_fn(summary.policy_stage, context, summary)` 替换为直接写文件：

```python
import json as _json, time as _time
ts_ms = int(_time.time() * 1000)
alert_event = {
    "type": "local_pattern_alert",
    "ts_ms": ts_ms,
    "stage": summary.policy_stage,   # PatternDetector 默认 "explore"
    "intent_signature": summary.intent_signature,
    "meta": {
        "occurrences": summary.occurrences,
        "window_minutes": round(summary.window_minutes, 1),
        "machine_ids": summary.machine_ids,
        "detector_signals": summary.detector_signals,
        "app": summary.context_hint.get("app", ""),
    },
}
events_local = (self._state_root or Path.home() / ".emerge" / "repl") / "events-local.jsonl"
events_local.parent.mkdir(parents=True, exist_ok=True)
with events_local.open("a", encoding="utf-8") as f:
    f.write(_json.dumps(alert_event, ensure_ascii=False) + "\n")
```

注意：本地检测的 `policy_stage` 仍然是 `"explore"`（PatternDetector 默认值）。如果本地模式也需要感知 canary/stable，需要单独查 SpanTracker — Phase 2 不做，因为本地模式目前无 watcher agent 使用。

---

## 5. repl_admin 变更

### 5.1 后台 mtime 轮询线程

在 `cmd_serve` 启动 HTTP server 之前启动：

```python
def _start_monitor_state_watcher(state_root: Path) -> None:
    """1s 轮询 runner-monitor-state.json mtime，变化时 SSE broadcast monitors_updated."""
    monitor_path = state_root / "runner-monitor-state.json"
    last_mtime = 0.0

    def _poll():
        nonlocal last_mtime
        while True:
            time.sleep(1.0)
            try:
                mtime = monitor_path.stat().st_mtime
                if mtime != last_mtime:
                    last_mtime = mtime
                    _sse_broadcast({"monitors_updated": True})
            except FileNotFoundError:
                pass
            except Exception:
                pass

    t = threading.Thread(target=_poll, daemon=True, name="MonitorStateWatcher")
    t.start()
```

调用时机：`cmd_serve()` 中 server 启动前。

---

## 6. 废弃的文件和格式

| 废弃 | 替代 |
|---|---|
| `~/.emerge/repl/pattern-alerts-{profile}.json` | `events-{profile}.jsonl` 中 `type=pattern_alert` |
| `~/.emerge/repl/pattern-alerts.json` | `events-local.jsonl` 中 `type=local_pattern_alert` |
| `EmergeDaemon._push_pattern()` 方法 | DaemonHTTPServer 内联 + OperatorMonitor 直写 |
| `EmergeDaemon._build_explore_message()` 方法 | 删除（watcher 读结构化字段） |
| `OperatorMonitor.push_fn` 参数 | 删除 |

---

## 7. 文件变更列表

| 文件 | 变更 |
|---|---|
| `scripts/daemon_http.py` | 新增 `_detector`, `_runner_event_buffers`, `_runner_buffers_lock`；`_on_runner_event` 追加检测逻辑；`_write_monitor_state` `team_active` 修正 |
| `scripts/emerge_daemon.py` | 删除 `_push_pattern`, `_build_explore_message`；`OperatorMonitor` 构造改为 `push_fn=None` / 移除参数；传入 `state_root` |
| `scripts/operator_monitor.py` | 移除 `push_fn` 参数；`process_local_file` 直接写 `events-local.jsonl`；新增 `state_root` 参数 |
| `scripts/repl_admin.py` | 新增 `_start_monitor_state_watcher()`，在 `cmd_serve` 中调用 |
| `scripts/pending_actions.py` | 确认 `format_local_pattern_alert` formatter 存在（`watch_emerge.py` 需要） |
| `CLAUDE.md` | 更新 Runner push architecture + Key Invariants |
| `README.md` | 更新架构图事件流 |
| `tests/test_daemon_http.py` | 新增 runner push → pattern_alert 检测测试 |
| `tests/test_operator_monitor.py` | 更新 process_local_file 测试（写 events-local.jsonl） |
| `tests/test_repl_admin.py` | 新增 monitor state watcher SSE 测试 |

---

## 8. 测试要点

1. `_on_runner_event` 连续推送 ≥3 条相同 app/event_type 事件 → `events-{profile}.jsonl` 出现 `pattern_alert`，`stage` 值来自 SpanTracker
2. `_connected_runners[profile]["last_alert"]` 在检测后更新
3. `_write_monitor_state` 后 `runner-monitor-state.json` 的 `team_active = True`（有连接 runner 时）
4. `process_local_file` 推送 ≥3 条事件 → `events-local.jsonl` 出现 `local_pattern_alert`
5. `runner-monitor-state.json` mtime 变化后 repl_admin SSE 广播 `monitors_updated`

---

## 9. 不在 Phase 2 范围内

- 本地 `local_pattern_alert` 的 canary/stable stage 查询（本地无 watcher，YAGNI）
- agents-team 动态 runner 上线自动 spawn watcher（Phase 3 — 当前需手动调 `/emerge:monitor`）
- `_inject_controls`（cockpit 自动注入 connector controls，Phase 1+ spec 中提及但推迟）
