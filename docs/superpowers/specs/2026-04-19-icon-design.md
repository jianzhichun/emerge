# emerge 图标系统设计文档

**日期:** 2026-04-19  
**范围:** assets/ · remote_runner.py · cockpit/index.html · cockpit/src/App.svelte · operator_popup.py

---

## 核心设计

**双飞轮 ∞**：两个相交的椭圆环，左蓝右紫，共享白色中心点，右环顶部有方向箭头。

| 元素 | 颜色 | 语义 |
|---|---|---|
| 左环 | `#60a5fa`（蓝） | 正向飞轮：exec→span→stable→零推理执行 |
| 右环 | `#a78bfa`（紫） | 反向飞轮：operator 监控→PatternDetector→AI 接管 |
| 中心白点 | `#ffffff` | 结晶点：两飞轮交汇，pipeline 诞生处 |
| 方向箭头 | `#c4b5fd` | stable 自动推进方向（右环顶端） |
| 背景 | `#0f172a`（深海蓝） | 圆角方块，rx=14（64px 坐标系） |

---

## 变体

### 彩色版（color）
用于：Cockpit favicon、Cockpit header logo、macOS .icns、Windows .ico

- 背景：`#0f172a` 圆角方块
- ∞ 双色环 + 白点 + 箭头
- 尺寸：16 / 32 / 64 / 128 / 256 px PNG

### 单色版（monochrome）
用于：macOS 系统托盘（template image）、Windows 托盘

- 无背景（透明）
- ∞ 线条 + 中心点：深色模式用 `white`，浅色模式用 `#1e293b`
- macOS pystray 传入白色 PIL Image（系统自动处理 template 反色）
- 尺寸：64px PIL RGBA，透明背景

### Cockpit header wordmark
- 28px 彩色图标 + `emerge` 文字（`#e2e8f0`，`font-weight:600`）
- 两者间距 10px

---

## 文件清单

```
assets/
  icon.svg          # 主 SVG（彩色版，64px 坐标系，直接可用）
  icon-16.png       # 彩色 16×16
  icon-32.png       # 彩色 32×32
  icon-64.png       # 彩色 64×64
  icon-128.png      # 彩色 128×128
  icon-256.png      # 彩色 256×256
  icon-tray.png     # 单色 64×64 RGBA 透明（pystray 用）
```

PNG 由 `scripts/generate_icons.py` 从 SVG 数据用 Pillow 生成，不依赖外部工具。

---

## SVG 规范（64px 坐标系）

```svg
<svg viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg">
  <!-- 背景 -->
  <rect width="64" height="64" rx="14" fill="#0f172a"/>
  <!-- 微光晕 -->
  <circle cx="32" cy="32" r="8" fill="#6366f1" opacity="0.12"/>
  <!-- 左环（正向飞轮） -->
  <path d="M32 32 C32 32 26 21 20 21 C13 21 13 43 20 43 C26 43 32 32 32 32 Z"
        stroke="#60a5fa" stroke-width="4.5" fill="none" stroke-linejoin="round"/>
  <!-- 右环（反向飞轮） -->
  <path d="M32 32 C32 32 38 21 44 21 C51 21 51 43 44 43 C38 43 32 32 32 32 Z"
        stroke="#a78bfa" stroke-width="4.5" fill="none" stroke-linejoin="round"/>
  <!-- 方向箭头 -->
  <polygon points="44,21 48,26 40,24" fill="#c4b5fd" opacity="0.9"/>
  <!-- 结晶点 -->
  <circle cx="32" cy="32" r="3.5" fill="white" opacity="0.95"/>
</svg>
```

单色变体（托盘）：去掉 `<rect>` 背景，所有 stroke/fill 改为 `white`（或 `#1e293b`）。

---

## 改动范围

| 文件 | 改动 |
|---|---|
| `assets/icon.svg` | 新建，主 SVG 源文件 |
| `scripts/generate_icons.py` | 新建，从 SVG 路径数据用 Pillow 生成所有 PNG |
| `scripts/remote_runner.py` | `_start_tray`：改用 `assets/icon-tray.png`（PIL 加载），fallback 保留原有纯色方块 |
| `scripts/operator_popup.py` | `show_notify` / `_render_*`：`tk.Tk().iconphoto()` 设置窗口图标 |
| `scripts/admin/cockpit/index.html` | 内联 SVG favicon（data URI）；`<title>` 改为 `emerge` |
| `scripts/admin/cockpit/src/App.svelte` | header 区域加 logo（inline SVG + "emerge" 文字） |
| `scripts/admin/cockpit/dist/index.html` | src 改完后执行 `npm run build` 重新生成；dist 提交到 repo |

**不改动：** MCP schemas、policy_config、任何测试文件。

---

## 生成脚本接口

```bash
python3 scripts/generate_icons.py          # 生成全套 PNG 到 assets/
python3 scripts/generate_icons.py --verify # 检查所有尺寸文件存在且可被 PIL 加载
```

脚本用 Pillow 的 `ImageDraw` 直接绘制（不调用 `cairosvg` / `inkscape`），保证零额外依赖。

---

## 不在范围内

- 动画图标（托盘旋转动效）
- Windows `.ico` 多分辨率打包（后续可用 `Pillow` 的 `save(..., format='ICO')`）
- macOS `.icns`（后续用 `iconutil`）
- 深色/浅色自适应 SVG（cockpit 固定深色主题）
