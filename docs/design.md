# larksh — 设计文档

## 项目目标

larksh 的目标是将飞书（Lark）作为安全的命令行控制通道，让用户在网络受限环境中（无法直接 SSH）通过飞书消息控制远端主机上的 shell，体验尽量接近 SSH。

核心约束：
- 远端主机无需开放任何端口，仅需出站 HTTPS 连接飞书
- 基本 shell 操作无需安装任何客户端，飞书即界面；`larksh-client` 用于增强本地编辑体验
- 所有操作留有审计日志

---

## 整体架构

```
本地用户（飞书客户端）         飞书云（通道）              远端主机（larksh bot）
────────────────────          ──────────────             ──────────────────────
发消息 / 点卡片                消息事件 / 卡片更新          main.py（Python 服务）
larksh-client                  WebSocket 长连接             PTY shell 会话
                               飞书文档（中继编辑）          文件系统
```

### 核心组件

| 组件 | 位置 | 说明 |
|------|------|------|
| `main.py` | 远端主机 | 服务入口，初始化各模块并启动事件监听 |
| `bot/listener.py` | 远端主机 | 建立飞书 WebSocket 长连接，接收事件 |
| `bot/dispatcher.py` | 远端主机 | 路由消息事件和卡片回调，执行对应命令 |
| `shell/session_manager.py` | 远端主机 | 管理 PTY shell 会话（创建、复用、清理） |
| `messaging/streamer.py` | 远端主机 | 飞书 API 封装：发消息、更新卡片、流式输出 |
| `security/guard.py` | 远端主机 | 用户白名单、命令黑名单、路径访问控制、审计日志 |
| `larksh-client` | 本地 | 单文件脚本，提供 `edit` 等本地命令入口 |

### 线程模型

```
主线程                         daemon 线程：async-worker        daemon 线程：ws-listener
──────                         ──────────────────────────        ────────────────────────
等待 _shutdown_event            asyncio event loop                lark_oapi ws.Client
信号处理（SIGTERM/SIGINT）       shell 会话管理                    飞书 WebSocket 长连接
                                飞书 API 调用
                                后台 task（edit poll、cleanup）
```

事件从 ws-listener 通过 `asyncio.run_coroutine_threadsafe` 投递到 async-worker 的 event loop 处理。

---

## 输出处理管道

```
PTY 输出 → ANSI 剥离 → 哨兵检测完成 → 流式推送（每 2s PATCH）→ 截断 → 最终卡片
```

**ANSI 剥离**（`utils/ansi.py`）：剥除颜色、光标移动、OSC、退格等全部控制码。`ls --color` 的颜色去掉，但列名和格式完整保留。

**命令完成检测（哨兵机制）**：执行用户命令后，bot 在同一 shell 追加写入 `echo __LARKSH_DONE_<uuid>__`，从输出流中检测到该字符串即判断命令已完成。

**流式推送**：命令执行期间每 `push_interval_ms`（默认 2000ms）PATCH 一次卡片，显示当前累积输出，命令完成后由 `finalize_card` 做最终更新。

**截断规则**：超过 8000 字符或 100 行时保留末尾部分（确保用户看到最新输出）。

---

## `/edit` 命令：飞书文档中继方案

### 核心流程

```
本地 terminal                飞书文档（中转）              远端主机（larksh bot）
──────────────               ────────────────              ──────────────────────
larksh-client edit /etc/foo.conf
  │
  ├─ 1. 发送 /edit /etc/foo.conf 给 bot
  │                              ← 2. bot 读取文件内容
  │                              ← 3. bot 创建飞书文档，以 fence 格式写入内容
  │                              ← 4. bot 回复 document_id
  │
  ├─ 5. 本地拉取 raw_content，提取文件内容
  ├─ 6. 写入本地临时文件 /tmp/larksh-edit-xxxxx
  ├─ 7. 打开 $EDITOR（vi）编辑临时文件
  ├─ 8. 检测文件保存（inotify / mtime 轮询，间隔 500ms）
  │
  ├─ 9. 防抖 2s 后将新内容以 fence 格式写入飞书文档
  │                              ← 10. bot 收到 P2DriveFileEditV1 事件
  │                              ← 11. 检查 operator_id，跳过自身写入
  │                              ← 12. bot 读取 raw_content，提取新内容
  │                              ← 13. bot 写回 /etc/foo.conf
  │                              ← 14. bot 发消息确认 "✅ /etc/foo.conf 已更新"
  │
  └─ 15. 本地显示确认，清理临时文件
```

### 文档写入格式

```
{文件路径}
```{语言（可选）}
{文件原始内容，不做任何转义}
```
```

提取规则：找到第一个 ` ``` ` 行，其后直到下一个单独 ` ``` ` 行之间为文件内容，不做任何反转义。

### 格式验证结论

**验证日期**：2026-04-06 | **工具**：`tools/verify_docx_fidelity.py`

覆盖：普通文本、代码、Tab/空格缩进、特殊字符（`\x00`、`\\`、`<>&`）、Unicode、emoji、尾部换行、空行。

选型 **Markdown fence**（段落文本 API）而非 code block（Block API）：写入只需一次 POST，`raw_content` 中明文可见便于调试，解析逻辑简单。

### 关键 API

| 用途 | 方法 | 端点 |
|------|------|------|
| 创建文档 | POST | `/docx/v1/documents` |
| 追加段落 | POST | `/docx/v1/documents/{id}/blocks/{id}/children` |
| 读取内容 | GET | `/docx/v1/documents/{id}/raw_content` |
| 更新段落 | PATCH | `/docx/v1/documents/{id}/blocks/{block_id}` |
| 订阅事件 | — | `P2DriveFileEditV1`（需开通云文档事件权限） |

### 云文档事件前置条件

`P2DriveFileEditV1` 需在飞书开放平台手动订阅。bot 创建的文档须在 **bot 有访问权限的共享云空间**中，个人云盘文档无法触发事件。

### 设计决策

| 问题 | 决策 | 原因 |
|------|------|------|
| `P2DriveFileEditV1` 是否响应 API 写入 | 是；通过 `operator_id` 过滤 bot 自身写入 | 避免 bot 初始写入时触发回环 |
| 并发写入保护 | 本地客户端防抖 2s；bot 端 per-document_id 写锁 | 确保多次快速保存只写入最终版本 |
| 文档生命周期 | 不删除，保留作为历史记录 | 无需 `drive:drive` 写权限，减少实现复杂度 |
| 本地客户端分发 | 单文件脚本 | 零依赖管理，`curl` 下载即用 |
| edit-commit 所有权 | 验证 open_id 匹配会话创建者 | 防止跨用户提交 |
