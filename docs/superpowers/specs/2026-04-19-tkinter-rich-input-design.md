# RichInputWidget + /runner/upload 设计文档

**日期:** 2026-04-19  
**范围:** operator_popup.py · daemon_http.py · watch_emerge.py · remote_runner.py  

---

## 背景

emerge 现有两处 tkinter 输入 UI：

- `_render_input`：daemon 主动弹出的输入框（`type=input` popup）
- `show_input_bubble`：托盘"发送消息"触发的最小输入框

两者都是纯文本，无法附带图片或文件。operator 遇到需要传截图/日志/配置的场景只能用文字描述，增加了 LLM 推理负担。

---

## 目标

1. 统一两处输入 UI 为一个可复用的 `RichInputWidget`，支持图片和文件附件。
2. daemon 新增通用 `POST /runner/upload` 端点，供 widget 和未来 adapter/skill 使用。
3. 本地 runner 和远程 runner 走完全相同的代码路径（区别仅为 daemon URL）。

---

## 架构

```
runner (local or remote)
  └─ RichInputWidget
       ├─ 用户选择/拖拽文件
       ├─ 每个附件 POST /runner/upload → {file_id, path, mime}
       └─ POST /runner/event {type: operator_message, text, attachments:[{path,mime,name}]}

daemon :8789
  ├─ POST /runner/upload → state_root/uploads/{file_id}/{filename}
  └─ POST /runner/event  → events-{profile}.jsonl

watch_emerge.py
  └─ 读 events-{profile}.jsonl → 格式化输出到 CC session
       [ACTION REQUIRED][Operator:profile] 消息文本
       [附件: /abs/path/file.png (image/png)]
```

**设计决策：** local runner 不特殊处理，统一走 `localhost:8789`，零分支。

---

## 组件设计

### RichInputWidget (`operator_popup.py`)

```python
class RichInputWidget:
    def __init__(
        self,
        parent: tk.Tk,
        on_submit: Callable[[str, list[Attachment]], None],
        upload_url: str,          # http://<daemon>:8789/runner/upload
        title: str = "emerge",
    ): ...
```

```python
Attachment = TypedDict("Attachment", path=str, mime=str, name=str)
```

**UI 结构（A 风格，Claude Code 风格）：**

```
┌─────────────────────────────────────────┐
│ 多行 Text 区（4 行，可拖拽扩展）          │
├─────────────────────────────────────────┤
│ [📎 file.py ×]  [🖼 error.png ×]        │  ← chips 行（附件列表）
├─────────────────────────────────────────┤
│ [📁 文件] [🖼 图片]          [发送 ↵]   │  ← 底部工具栏
└─────────────────────────────────────────┘
```

**交互行为：**

| 行为 | 说明 |
|---|---|
| Ctrl+Enter / ⌘+Enter | 触发发送 |
| 拖拽文件到窗口 | 自动添加到附件队列 |
| 点击 [📁 文件] / [🖼 图片] | 打开系统文件选择器 |
| 上传中 | chip 显示 spinner，发送按钮禁用 |
| 上传失败 | chip 变红 + tooltip 显示错误 |
| 发送前 | 等待所有上传完成后再 POST /runner/event |
| 点击 chip 上的 × | 从列表移除（若已上传则丢弃 file_id） |

**上传实现：** 每个文件用 `threading.Thread` 异步上传，回调到主线程更新 chip 状态。

**调用方改动：**

```python
# _render_input: 改为
widget = RichInputWidget(root, on_submit=..., upload_url=upload_url)

# show_input_bubble: 改为
widget = RichInputWidget(root, on_submit=lambda text, att: on_submit(text, att), upload_url=upload_url)
```

`upload_url` 由调用方传入。`remote_runner.py` 从 `self._daemon_url` 派生：`f"{self._daemon_url}/runner/upload"`。

---

### POST /runner/upload (`daemon_http.py`)

**请求：**

```
POST /runner/upload
Content-Type: multipart/form-data

file:           binary  (required)
runner_profile: str     (optional)
```

**响应：**

```json
200: {"file_id": "uuid4-string", "path": "/abs/state_root/uploads/uuid/filename", "mime": "image/png"}
400: {"error": "no file provided"}
413: {"error": "file too large"}
```

**存储：** `state_root/uploads/{file_id}/{original_filename}`

**限制：** `EMERGE_UPLOAD_MAX_BYTES`（默认 50MB）。超限返回 413。

**安全：** 文件名做 `Path(...).name` sanitize，禁止路径穿越。

**清理：** 无自动 TTL。文件随 daemon 运行积累，operator 或脚本手动清理。文件路径由 CC 直接 `Read`，无需额外服务。

---

### watch_emerge.py — 格式化

`operator_message` 事件新增 `attachments` 字段后，`format_operator_message` 追加附件行：

```
[ACTION REQUIRED][Operator:mycader-1] 帮我看看这个报错
[附件: /state/uploads/abc123/error.png (image/png)]
[附件: /state/uploads/def456/config.yaml (text/yaml)]
```

CC 用 `Read` 工具读取文件路径（CC 支持图片 Read，直接可视化）。

---

## 改动范围

| 文件 | 改动 |
|---|---|
| `scripts/operator_popup.py` | 新增 `RichInputWidget` 类；`_render_input` + `show_input_bubble` 改为实例化它 |
| `scripts/daemon_http.py` | 新增 `POST /runner/upload` handler；multipart 解析 + 文件存储 |
| `scripts/watch_emerge.py` | `format_operator_message` 加附件行格式化 |
| `scripts/remote_runner.py` | 传 `upload_url` 给 widget（< 5 行改动） |

**不改动：** `mcp/schemas.py`、`policy_config.py`、`emerge_sync.py`，无新依赖（`multipart` 用标准库 `cgi` 或 `email` 解析）。

---

## 不在范围内

- 附件 TTL / 自动清理（后续可加）
- upload 端点鉴权（daemon 本身无鉴权，保持一致）
- 图片缩略图预览（chip 名称已够用）
- 多文件并发上传进度聚合 UI（每个 chip 独立状态已够用）
