# larksh

通过飞书机器人控制内网服务器 Shell 的工具。支持命令执行、文件传输、远端文件编辑，带审计日志、用户白名单和命令黑名单。

## 适用系统

| 平台 | 支持情况 | 说明 |
|---|---|---|
| Linux（标准发行版） | ✅ 推荐 | systemd 管理，PTY 完整支持 |
| OpenWrt | ✅ 支持 | procd init.d 管理，需安装 `python3`、`pip3`（`opkg install python3 python3-pip`） |
| macOS | ⚠️ 可运行 | PTY 可用，但无 systemd，需手动管理进程 |
| Windows | ❌ 不支持 | 依赖 PTY 和 POSIX Shell |

**Python 版本要求**：3.10+

## 功能

- **Shell 命令执行**：直接在飞书对话框发送命令，结果以卡片形式实时展示
- **Ctrl+C 中断**：运行中的命令可点击卡片上的 Ctrl+C 按钮中断
- **文件下载**：`/get <文件或目录>` 将文件发送到飞书（目录自动打包 zip）
- **文件上传**：拖拽文件到对话框，点击保存按钮选择服务器路径
- **远端文件编辑**：`/edit <文件>` 配合 `larksh-client` 用本地 `$EDITOR` 编辑服务器文件
- **会话隔离**：每个用户/群聊独立 PTY Shell 会话，支持跨命令状态保持（工作目录、环境变量）
- **安全管控**：用户白名单、命令黑名单、全量审计日志

## 快速开始

### 1. 安装依赖

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2. 配置

```bash
cp deploy/config.example.yaml config.yaml
# 编辑 config.yaml，填入飞书 app_id、app_secret 及用户白名单
```

### 3. 启动

```bash
.venv/bin/python main.py --config config.yaml
```

---

## 飞书后台配置

### 创建应用

1. 进入[飞书开放平台](https://open.feishu.cn/)，点击「开发者后台」
2. 「企业自建应用」→「创建应用」，填写名称和描述
3. 在「凭据与基础信息」页面获取 **App ID** 和 **App Secret**，填入 `config.yaml`

### 启用机器人能力

「应用功能」→「机器人」→ 开启

### 配置事件订阅

「开发配置」→「事件与回调」→「事件配置」

**接入方式**：选择「使用长连接接收事件」（WebSocket 模式，无需公网地址）

**添加事件订阅**（点击「添加事件」逐一搜索添加）：

| 事件标识符 | 用途 |
|---|---|
| `im.message.receive_v1` | 接收用户发送的消息和文件 |
| `drive.file.edit_v1` | 接收飞书文档编辑事件（`/edit` 命令使用） |

**添加回调**（同页面「卡片回调」部分）：

| 回调类型 | 用途 |
|---|---|
| 卡片行动（`card.action.trigger`） | 处理卡片按钮点击（Ctrl+C、保存文件等） |

### 配置权限

「开发配置」→「权限管理」→「API 权限」，搜索并开通以下权限：

**消息相关**

| 权限名称 | 权限标识符 | 说明 |
|---|---|---|
| 获取与发送单聊、群组消息 | `im:message` | 发送命令结果卡片 |
| 上传、下载文件或附件 | `im:resource` | 文件上传下载 |
| 读取用户发给机器人的单聊消息 | `im:message.receive_p2p:readonly` | 接收私聊命令 |
| 获取群组中所有消息 | `im:message.group_at_msg:readonly` | 接收群聊命令（需 @ 机器人） |

**机器人相关**

| 权限名称 | 权限标识符 | 说明 |
|---|---|---|
| 获取机器人基本信息 | `bot:info:readonly` | 过滤机器人自身消息 |

**文档相关**（`/edit` 命令使用）

| 权限名称 | 权限标识符 | 说明 |
|---|---|---|
| 查看、评论、编辑和管理文档 | `docx:document` | 读写飞书文档 |
| 查看、编辑和管理云空间中所有文件 | `drive:drive` | 创建文档 |

> 权限申请后需发布版本才能生效（企业版需管理员审批）。

### 发布应用

「应用发布」→「版本管理与发布」→ 创建版本并发布

---

## 配置文件说明

```yaml
feishu:
  app_id: "cli_xxxxxxxxxxxxxxxx"
  app_secret: "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
  event_mode: "websocket"       # websocket 或 webhook

security:
  allowed_users:                # open_id 白名单（ou_ 开头）
    - "ou_xxxxxxxxxxxxxxxxxxxxxxxx"
  allowed_groups: []            # chat_id 白名单（oc_ 开头，群内所有成员可用）
  command_blacklist:            # 命令黑名单，支持 fnmatch 通配
    - "rm -rf /*"
  audit_log: "/var/log/larksh/audit.jsonl"

shell:
  bash_path: "/bin/bash"
  session_timeout: 3600         # 会话超时（秒）
  pty_cols: 220                 # PTY 终端列数（影响命令行折行宽度）
  pty_rows: 40                  # PTY 终端行数
  env:                          # 注入到 Shell 会话的额外环境变量（可选）
    TERM: "xterm-256color"
    LANG: "en_US.UTF-8"

output:
  max_output_chars: 8000        # 单次最大输出字符数（超出截断）
  max_lines: 100                # 代码块最大行数（超出只显示末尾 N 行）
  push_interval_ms: 2000        # 流式推送间隔（毫秒，0 为关闭）
  idle_timeout_ms: 500          # 命令完成静默超时（毫秒）

logging:
  level: "INFO"
```

---

## 命令参考

直接发送 shell 命令即可执行，也支持以下特殊命令：

| 命令 | 说明 |
|---|---|
| `/help` | 显示帮助 |
| `/status` | 查看当前会话状态（PID、空闲时间等） |
| `/cd <目录>` | 切换工作目录 |
| `/get <文件或目录>` | 将服务器文件发送到飞书（目录打包为 zip） |
| `/save <路径>` | 保存最近上传的文件到指定路径 |
| `/edit <文件>` | 创建飞书文档中转，配合 `larksh-client` 用本地编辑器编辑（会话有效期 1 小时） |
| `/edit-commit <doc_id>` | 将文档内容写回服务器文件（由 `larksh-client` 编辑完成后自动触发） |
| `/kill` | 强制终止并重建当前 Shell 会话 |
| `/exit` | 关闭当前 Shell 会话 |

**`/save` 路径规则：**
- 路径以 `/` 结尾，或解析后已是已存在目录 → 追加原始文件名
  - `~/logs/` → `/home/user/logs/<原始文件名>`
- 否则视为完整文件路径（含文件名）
  - `/tmp/myfile.txt` → `/tmp/myfile.txt`
  - `logs/backup.log` → `<当前工作目录>/logs/backup.log`

---

## larksh-client（本地编辑器）

`larksh-client` 是本地命令行工具，配合 `/edit` 命令实现用本地 `$EDITOR` 编辑服务器文件。

**配置**（`~/.larksh.json`，首次运行会引导创建）：
```json
{
  "app_id":     "cli_xxx",
  "app_secret": "xxx",
  "open_id":    "ou_xxx"
}
```

> `open_id` 是你自己的飞书用户 ID（`ou_` 开头），可在飞书管理后台「成员管理」中获取。

**使用方式**：
```bash
# 在飞书中发送：/edit /etc/nginx/nginx.conf
# 本地直接运行（会自动向 bot 发送 /edit 命令并等待回复）：
larksh-client edit /etc/nginx/nginx.conf
```

编辑器每次保存后，客户端监听到文件变化，在最后一次修改 2 秒后将内容同步到飞书文档，bot 约 5 秒内自动写回服务器文件。

---

## 打包分发

用 `make dist` 将项目打包为可分发的源码压缩包，适合部署到无法直接访问代码仓库的服务器：

```bash
make dist
# 生成 dist/larksh-<版本>.tar.gz
```

版本号取自 `git describe --tags`，未打 tag 时为 `dev`。打包内容包括所有源代码、`install.sh`、`deploy/`（服务文件和配置模板）和 `README.md`。

**在目标机器上安装**：

```bash
tar xzf larksh-<版本>.tar.gz
cd larksh-<版本>
sh install.sh
```

`install.sh` 会自动：
- 将文件复制到 `/opt/larksh`（可通过 `INSTALL_DIR` 环境变量覆盖）
- 创建虚拟环境并安装依赖
- 打印后续配置步骤（systemd 或 OpenWrt procd）

---

## 部署（systemd）

```bash
# 1. 安装到 /opt/larksh
sudo mkdir -p /opt/larksh /etc/larksh /var/log/larksh
sudo cp -r . /opt/larksh/
sudo cp config.yaml /etc/larksh/config.yaml
cd /opt/larksh && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 2. 安装 systemd 服务
sudo cp deploy/larksh.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now larksh

# 3. 查看日志
journalctl -u larksh -f
```

> `deploy/larksh.service` 中 `User=nobody`，请根据实际运行用户修改。

**更新代码**（在开发目录执行）：
```bash
sudo make install   # 同步代码、更新依赖、重启服务（需要 sudo 权限写入 /opt/larksh）
```

---

## 部署（OpenWrt）

OpenWrt 需先安装 Python：

```bash
opkg update && opkg install python3 python3-pip
```

然后用 `install.sh` 安装（见[打包分发](#打包分发)），安装完成后配置 procd 开机自启：

```bash
cat > /etc/init.d/larksh <<'EOF'
#!/bin/sh /etc/rc.common
START=99

start() {
    /opt/larksh/.venv/bin/python /opt/larksh/main.py \
        --config /etc/larksh/config.yaml > /var/log/larksh/larksh.log 2>&1 &
}

stop() {
    killall -f "python.*main.py" 2>/dev/null || true
}
EOF
chmod +x /etc/init.d/larksh
/etc/init.d/larksh enable
/etc/init.d/larksh start
```

> OpenWrt 内存较小，建议关闭审计日志：在 `config.yaml` 中将 `audit_log` 设为空字符串（`audit_log: ""`），或将其指向外部存储（如 `audit_log: "/mnt/usb/larksh/audit.jsonl"`）。

---

## 注意事项

- `top`/`htop`/`vim` 等全屏 TUI 程序不支持，建议用 `ps aux`、`cat`、`head` 替代
- 飞书单次文件传输限制 30 MB
- 输出超过最大字符数（默认 8000）或最大行数（默认 100）时只显示末尾部分，可用 `/get` 取回完整文件；两个阈值均可在 `config.yaml` 的 `output` 字段调整
- 所有操作均记录审计日志（路径见 `config.yaml` 的 `audit_log`）
- `larksh.service` 包含 `NoNewPrivileges=yes`，Shell 会话内无法使用 `sudo`；如需特权操作，请去掉该限制或改用其他部署方式

## 常见问题排查

**Bot 启动后无响应**

- 确认 `config.yaml` 中 `app_id` / `app_secret` 填写正确
- 确认飞书后台已开启「机器人」能力及「接收消息」事件订阅
- 查看日志：`journalctl -u larksh -n 50`

**命令发出后卡住不回复**

- 网络原因：确认服务器能访问 `open.feishu.cn`（`curl https://open.feishu.cn`）
- token 过期：重启服务即可自动刷新；bot 会在 401 时自动重试一次

**`/edit` 提交后文件未更新**

- 确认飞书后台已订阅 `drive.file.edit_v1` 事件
- bot 创建的文档须位于 bot 有访问权限的共享云空间，个人云盘中的文档无法触发事件
- 编辑会话默认 1 小时 TTL，超时后需重新执行 `/edit`

**`/get` 上传失败（权限错误）**

- 确认飞书应用已开通 `im:resource` 权限（开放平台控制台 → 权限管理）

**systemd 服务反复重启**

- `StartLimitBurst=5`：60 秒内崩溃超过 5 次后 systemd 会停止尝试
- 运行 `journalctl -u larksh -n 100` 查看崩溃原因后再 `systemctl start larksh`
