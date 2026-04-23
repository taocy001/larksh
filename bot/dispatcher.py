"""Command dispatcher — handles Feishu message events and card callbacks, executes shell commands"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Optional

from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTriggerResponse, CallBackToast,
)

from messaging.streamer import CardStreamer
from security.guard import SecurityGuard
from shell.session_manager import ShellSessionManager

logger = logging.getLogger(__name__)


def _build_fence(path: str, content: str) -> str:
    """Build the fence-formatted content to write into a Feishu document."""
    return f"{path}\n```\n{content}\n```"


def _extract_fence_content(raw: str) -> Optional[str]:
    """Extract the file content wrapped inside a fence block from document raw_content; returns None on failure."""
    lines = raw.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip().startswith("```"):
            if start is None:
                start = i + 1
            else:
                return "\n".join(lines[start:i])
    return None

HELP_TEXT = """\
**larksh 控制台 — 帮助**

直接发送命令即可执行，例如：`ls -la`、`df -h`

**特殊命令：**
• `/help` — 显示此帮助
• `/exit` — 关闭当前 shell 会话
• `/cd <目录>` — 切换工作目录
• `/get <文件>` — 将远端文件发送到飞书
• `/save <路径>` — 保存最近上传的文件到指定路径
• `/edit <文件>` — 创建飞书文档中转，配合本地 larksh-client 用 $EDITOR 编辑
• `/edit-commit <doc_id>` — 将文档内容写回服务器文件（larksh-client 编辑完自动触发）
• `/status` — 查看当前会话状态
• `/kill` — 强制终止并重建当前 shell

**文件传输：**
• 下载：`/get <文件或目录>` 发到飞书（目录自动打包为 zip）
• 上传：直接将文件拖入对话框发送，按提示填写保存路径；或用 `/save <路径>` 命令保存
• 飞书单次传输限制 30 MB

**注意：**
• `top`/`htop`/`vim` 等全屏 TUI 程序不支持，建议用 `ps aux --sort=-%cpu | head -20` 替代 top
• 输出超过 100 行时只显示末尾部分，可将输出重定向到文件后用 `/get` 取回

> ⚠️ 所有命令均有审计日志，请合法使用。
"""


class CommandDispatcher:
    """
    Receives Feishu events and routes them to the appropriate handler logic.

    Supports two event sources:
    1. im.message.receive_v1  — user sends a text message directly
    2. card.action.trigger    — user submits a card form or clicks a button
    """

    def __init__(
        self,
        session_manager: ShellSessionManager,
        streamer: CardStreamer,
        security: SecurityGuard,
        config,
        main_loop=None,
    ) -> None:
        self._sm = session_manager
        self._streamer = streamer
        self._security = security
        self._config = config
        self._main_loop = main_loop
        self._last_get: dict[str, str] = {}  # open_id → last /get path
        # open_id → {file_key, file_name, message_id, receive_id, receive_id_type}
        self._last_upload: dict[str, dict] = {}

        # Document relay edit state
        # doc_token → (file_path, open_id, receive_id, receive_id_type, created_at)
        self._edit_map: dict[str, tuple[str, str, str, str, float]] = {}
        # doc_token → content block_id (used for subsequent updates)
        self._edit_block_map: dict[str, str] = {}
        # doc_token → asyncio.Lock (prevents concurrent writes)
        self._edit_locks: dict[str, asyncio.Lock] = {}
        # bot's own open_id (fetched asynchronously at startup, used to filter self-write events)
        self._bot_open_id: str = ""
        # edit session TTL (seconds); entries are removed from _edit_map after expiry
        self._edit_ttl: float = 3600.0
        # doc_token → hash of last written content (used by polling to avoid redundant writes)
        self._edit_content_hash: dict[str, str] = {}
        # strong references to background Tasks (prevents GC cancellation)
        self._bg_tasks: set[asyncio.Task] = set()

        # Persistence: restore _edit_map after restart (prevents /edit sessions from being lost on restart)
        state_dir = Path(getattr(config, "state_dir", None) or "/var/log/larksh")
        self._edit_state_file = state_dir / "edit_state.json"
        self._load_edit_state()

    # ------------------------------------------------------------------
    # edit_map persistence (retain /edit sessions across restarts)
    # ------------------------------------------------------------------

    def _load_edit_state(self) -> None:
        try:
            if not self._edit_state_file.exists():
                return
            data = json.loads(self._edit_state_file.read_text())
            now = time.time()
            for doc_id, entry in data.items():
                created_at = entry.get("created_at", 0)
                if now - created_at > self._edit_ttl:
                    continue  # skip expired entries
                self._edit_map[doc_id] = (
                    entry["path"], entry["open_id"],
                    entry["receive_id"], entry["receive_id_type"],
                    created_at,
                )
                if "block_id" in entry:
                    self._edit_block_map[doc_id] = entry["block_id"]
                if "content_hash" in entry:
                    self._edit_content_hash[doc_id] = entry["content_hash"]
            logger.info("Loaded %d edit session(s) from %s", len(self._edit_map), self._edit_state_file)
            # Restart background polling for each restored session (requires event loop; deferred until first dispatch)
            self._pending_poll_restart = list(self._edit_map.keys())
        except Exception:
            logger.warning("Failed to load edit state from %s", self._edit_state_file, exc_info=True)

    def _save_edit_state(self) -> None:
        try:
            self._edit_state_file.parent.mkdir(parents=True, exist_ok=True)
            data = {}
            for doc_id, (path, open_id, receive_id, receive_id_type, created_at) in self._edit_map.items():
                data[doc_id] = {
                    "path": path, "open_id": open_id,
                    "receive_id": receive_id, "receive_id_type": receive_id_type,
                    "created_at": created_at,
                    "block_id": self._edit_block_map.get(doc_id, ""),
                    "content_hash": self._edit_content_hash.get(doc_id, ""),
                }
            self._edit_state_file.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        except Exception:
            logger.warning("Failed to save edit state to %s", self._edit_state_file, exc_info=True)

    def _cancel_edit_sessions_for_path(self, file_path: str) -> None:
        """Cancel all stale edit sessions pointing to the same file to prevent multiple polls from concurrently overwriting it."""
        to_remove = [
            doc_id for doc_id, entry in self._edit_map.items()
            if entry[0] == file_path
        ]
        for doc_id in to_remove:
            self._edit_map.pop(doc_id, None)
            self._edit_block_map.pop(doc_id, None)
            self._edit_locks.pop(doc_id, None)
            self._edit_content_hash.pop(doc_id, None)
            logger.info("edit: cancelled stale session doc=%s path=%s", doc_id, file_path)

    async def _restart_pending_polls(self) -> None:
        """Start polling for sessions restored by _load_edit_state once the event loop is ready."""
        for doc_id in getattr(self, "_pending_poll_restart", []):
            if doc_id in self._edit_map:
                task = asyncio.create_task(self._poll_edit_doc(doc_id))
                self._bg_tasks.add(task)
                task.add_done_callback(self._bg_tasks.discard)
                logger.info("edit-poll: restarted after state restore doc=%s", doc_id)
        self._pending_poll_restart = []

    async def _poll_edit_doc(self, doc_id: str, interval: float = 5.0) -> None:
        """Background poll for document content changes; automatically writes to the server file when a change is detected."""
        import hashlib as _hashlib
        import aiofiles
        deadline = time.time() + self._edit_ttl
        logger.info("edit-poll: started doc=%s", doc_id)
        while time.time() < deadline:
            await asyncio.sleep(interval)
            entry = self._edit_map.get(doc_id)
            if not entry:
                logger.info("edit-poll: stopped (session gone) doc=%s", doc_id)
                return
            file_path = entry[0]
            try:
                raw = await self._streamer._api.get_doc_raw_content(doc_id)
                new_content = _extract_fence_content(raw)
                if new_content is None:
                    continue
                new_hash = _hashlib.md5(new_content.encode()).hexdigest()
                if new_hash == self._edit_content_hash.get(doc_id):
                    continue  # no change
                # Content has changed — write to file
                Path(file_path).parent.mkdir(parents=True, exist_ok=True)
                async with aiofiles.open(file_path, "w", encoding="utf-8") as f:
                    await f.write(new_content)
                self._edit_content_hash[doc_id] = new_hash
                logger.info("edit-poll: wrote %s (%d bytes) from doc %s", file_path, len(new_content), doc_id)
                # Notify the user
                _, _oid, receive_id, receive_id_type, _ = entry
                await self._streamer.send_text(receive_id, receive_id_type, f"✅ `{file_path}` 已更新")
            except Exception:
                logger.debug("edit-poll: error doc=%s", doc_id, exc_info=True)
        # TTL expired — clean up
        self._edit_map.pop(doc_id, None)
        self._edit_block_map.pop(doc_id, None)
        self._edit_locks.pop(doc_id, None)
        self._edit_content_hash.pop(doc_id, None)
        self._save_edit_state()
        logger.info("edit-poll: TTL expired, cleaned up doc=%s", doc_id)

    # ------------------------------------------------------------------
    # lark_oapi event callbacks (synchronous entry points; schedule async tasks internally)
    # ------------------------------------------------------------------

    def _schedule(self, coro) -> None:
        """Schedule a coroutine onto the main event loop (compatible with calls from background threads)."""
        if self._main_loop is not None:
            asyncio.run_coroutine_threadsafe(coro, self._main_loop)
        else:
            asyncio.ensure_future(coro)

    def on_message(self, data) -> None:
        """Handle im.message.receive_v1 events."""
        self._schedule(self._handle_message(data))

    def on_card_action(self, data) -> P2CardActionTriggerResponse:
        """Handle card.action.trigger events; must return a response synchronously."""
        logger.info("on_card_action called: data=%r", data)
        self._schedule(self._handle_card_action(data))
        # Feishu requires a synchronous response; the actual business logic is handled in the async task
        resp = P2CardActionTriggerResponse()
        resp.toast = CallBackToast()
        resp.toast.type = "info"
        resp.toast.content = "处理中..."
        return resp

    def on_doc_edit(self, data) -> None:
        """Handle P2DriveFileEditV1 events."""
        self._schedule(self._handle_doc_edit(data))

    async def fetch_bot_open_id(self) -> None:
        """Fetch the bot's own open_id (called once at startup); also restores persisted edit polls."""
        try:
            self._bot_open_id = await self._streamer._api.get_bot_open_id()
            logger.info("Bot open_id: %s", self._bot_open_id)
        except Exception:
            logger.exception("Failed to fetch bot open_id; doc self-edit filter disabled")
        await self._restart_pending_polls()

    # ------------------------------------------------------------------
    # Document edit event handling
    # ------------------------------------------------------------------

    async def _handle_doc_edit(self, data) -> None:
        try:
            event = data.event
            file_token: str = event.file_token or ""
            operator_ids: list = event.operator_id_list or []

            # Filter out writes by the bot itself (prevent feedback loops)
            if self._bot_open_id and operator_ids:
                if all(
                    (getattr(op, "open_id", None) or "") == self._bot_open_id
                    for op in operator_ids
                ):
                    logger.debug("Doc edit by bot itself, skipping: %s", file_token)
                    return

            entry = self._edit_map.get(file_token)
            if not entry:
                logger.debug("drive.file.edit_v1: file_token=%r not in edit_map (known: %s)",
                             file_token, list(self._edit_map.keys()))
                return

            path, _open_id, receive_id, receive_id_type, created_at = entry

            # TTL check: discard and clean up expired edit sessions immediately
            if time.time() - created_at > self._edit_ttl:
                self._edit_map.pop(file_token, None)
                self._edit_block_map.pop(file_token, None)
                self._edit_locks.pop(file_token, None)
                self._save_edit_state()
                logger.info("Edit session expired: doc=%s path=%s", file_token, path)
                return

            # per-document write lock to prevent concurrent writes
            if file_token not in self._edit_locks:
                self._edit_locks[file_token] = asyncio.Lock()
            async with self._edit_locks[file_token]:
                raw = await self._streamer._api.get_doc_raw_content(file_token)
                content = _extract_fence_content(raw)
                if content is None:
                    logger.warning("Failed to extract fence content from doc %s", file_token)
                    return
                import aiofiles
                async with aiofiles.open(path, "w", encoding="utf-8") as f:
                    await f.write(content)
                logger.info("File written via doc relay: %s (doc=%s)", path, file_token)

            await self._streamer.send_text(
                receive_id, receive_id_type, f"✅ `{path}` 已更新"
            )
        except Exception:
            logger.exception("Error handling doc edit event")

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    async def _handle_message(self, data) -> None:
        try:
            msg = data.event.message
            sender = data.event.sender

            open_id: str = sender.sender_id.open_id
            chat_id: str = msg.chat_id
            chat_type: str = msg.chat_type
            receive_id = chat_id
            receive_id_type = "chat_id"

            # Handle file messages sent by the user
            if msg.message_type == "file":
                allowed, reason = self._security.check_user(open_id, chat_id)
                if not allowed:
                    await self._streamer.send_text(receive_id, receive_id_type, f"❌ {reason}")
                    return
                file_content = json.loads(msg.content)
                file_key: str = file_content.get("file_key", "")
                file_name: str = file_content.get("file_name", "")
                # Default path = current shell session's working directory / filename
                from pathlib import Path as _Path
                session_id = self._sm.session_id_for(chat_type, chat_id, open_id)
                session = self._sm.get(session_id)
                if session:
                    try:
                        default_path = str(_Path(f"/proc/{session.pid}/cwd").resolve() / file_name)
                    except Exception:
                        default_path = f"~/{file_name}"
                else:
                    default_path = f"~/{file_name}"
                # Save upload info for the /save command (fallback when card callbacks are unavailable)
                self._last_upload[open_id] = {
                    "file_key": file_key,
                    "file_name": file_name,
                    "message_id": msg.message_id,
                    "receive_id": receive_id,
                    "receive_id_type": receive_id_type,
                }
                logger.info("File upload received: file_key=%r file_name=%r open_id=%s", file_key, file_name, open_id)
                await self._streamer.send_save_prompt(
                    receive_id, receive_id_type, file_key, file_name, default_path,
                    message_id=msg.message_id,
                )
                return

            if msg.message_type != "text":
                return

            content = json.loads(msg.content)
            command: str = content.get("text", "").strip()
            command = _strip_mention(command)
            if not command:
                return

            await self._dispatch(
                open_id=open_id,
                chat_id=chat_id,
                chat_type=chat_type,
                command=command,
                receive_id=receive_id,
                receive_id_type=receive_id_type,
                reply_to_message_id=msg.message_id,
            )
        except Exception:
            logger.exception("Error handling message event")

    async def _handle_card_action(self, data) -> None:
        try:
            event = data.event  # P2CardActionTriggerData
            action = event.action
            operator = event.operator

            open_id: str = operator.open_id
            chat_id: str = (event.context.open_chat_id if event.context else "") or ""
            card_message_id: str = (event.context.open_message_id if event.context else "") or ""
            value: dict = action.value or {}
            action_type: str = value.get("action", "")
            session_id: str = value.get("session_id", "")

            if action_type == "run_cmd":
                form_values: dict = action.form_value or {}
                command = (form_values.get("cmd") or "").strip()
                if not command:
                    return

                chat_type = "p2p" if session_id.startswith("user_") else "group"
                receive_id = chat_id or open_id
                receive_id_type = "chat_id" if chat_id else "open_id"

                await self._dispatch(
                    open_id=open_id,
                    chat_id=chat_id,
                    chat_type=chat_type,
                    command=command,
                    receive_id=receive_id,
                    receive_id_type=receive_id_type,
                    hint_session_id=session_id,
                )

            elif action_type == "save_file":
                file_key: str = value.get("file_key", "")
                file_name: str = value.get("file_name", "")
                src_message_id: str = value.get("message_id", "")
                # CardKit 1.0: path is directly in value (no form_value)
                save_path: str = (value.get("save_path") or "").strip()
                receive_id = chat_id or open_id
                receive_id_type = "chat_id" if chat_id else "open_id"
                logger.info(
                    "save_file: file_key=%r file_name=%r src_message_id=%r "
                    "save_path=%r receive_id=%r receive_id_type=%r",
                    file_key, file_name, src_message_id, save_path, receive_id, receive_id_type,
                )
                allowed, reason = self._security.check_user(open_id, chat_id)
                if not allowed:
                    await self._streamer.send_text(receive_id, receive_id_type, f"❌ {reason}")
                    return
                if not file_key:
                    await self._streamer.send_text(receive_id, receive_id_type, "❌ 文件 key 缺失，无法保存")
                    return
                if not save_path:
                    await self._streamer.send_text(receive_id, receive_id_type, "❌ 请填写保存路径后再点保存")
                    return
                try:
                    import aiofiles
                    from pathlib import Path as _Path
                    session = await self._sm.get_or_create(
                        f"user_{open_id}" if not chat_id else f"chat_{chat_id}"
                    )
                    try:
                        session_cwd = _Path(f"/proc/{session.pid}/cwd").resolve()
                    except Exception:
                        session_cwd = _Path.home()
                    sp = _resolve_save_path(save_path, file_name, session_cwd)
                    path_ok, path_reason = self._security.check_path(sp)
                    self._security.audit_file_access(open_id, chat_id, "/save", str(sp), path_ok)
                    if not path_ok:
                        await self._streamer.send_text(receive_id, receive_id_type, f"❌ {path_reason}")
                        return
                    content = await self._streamer._api.download_file(file_key, message_id=src_message_id)
                    logger.info("save_file: downloaded %d bytes, saving to %r", len(content), str(sp))
                    sp.parent.mkdir(parents=True, exist_ok=True)
                    async with aiofiles.open(sp, "wb") as f:
                        await f.write(content)
                    await self._streamer.send_text(receive_id, receive_id_type, f"✅ 已保存：`{sp}`")
                    logger.info("File saved: %s by open_id=%s", sp, open_id)
                except Exception as e:
                    logger.exception("save_file failed")
                    await self._streamer.send_text(receive_id, receive_id_type, f"❌ 保存失败：{e}")

            elif action_type == "cancel_save":
                if card_message_id:
                    cancelled_card = {
                        "schema": "2.0",
                        "body": {
                            "elements": [
                                {"tag": "div", "text": {"tag": "lark_md", "content": "🚫 已取消保存"}},
                            ]
                        },
                    }
                    import json as _json
                    try:
                        await self._streamer._api.patch(
                            f"https://open.feishu.cn/open-apis/im/v1/messages/{card_message_id}",
                            {"content": _json.dumps(cancelled_card)},
                        )
                    except Exception:
                        logger.debug("cancel_save: failed to update card", exc_info=True)
                logger.info("cancel_save by open_id=%s", open_id)

            elif action_type == "ctrl_c":
                session = self._sm.get(session_id)
                if session:
                    session.send_ctrl_c()
                    logger.info("Ctrl+C sent to session=%s by open_id=%s", session_id, open_id)

            elif action_type == "close_session":
                destroyed = self._sm.destroy(session_id)
                logger.info("Session %s closed by %s (found=%s)", session_id, open_id, destroyed)

        except Exception:
            logger.exception("Error handling card action event")

    # ------------------------------------------------------------------
    # Core dispatch logic
    # ------------------------------------------------------------------

    async def _dispatch(
        self,
        open_id: str,
        chat_id: str,
        chat_type: str,
        command: str,
        receive_id: str,
        receive_id_type: str,
        hint_session_id: str = "",
        reply_to_message_id: str = "",
    ) -> None:
        # 1. User authorization
        allowed, reason = self._security.check_user(open_id, chat_id)
        if not allowed:
            await self._streamer.send_text(receive_id, receive_id_type, f"❌ {reason}")
            return

        # 2. Determine session_id
        session_id = hint_session_id or self._sm.session_id_for(chat_type, chat_id, open_id)

        # 3. Special commands (/help /exit /cd etc.)
        if command.startswith("/"):
            await self._handle_special(
                command, open_id, chat_id, session_id, receive_id, receive_id_type,
                reply_to_message_id=reply_to_message_id,
            )
            return

        # 4. Command blocklist check
        cmd_ok, cmd_reason = self._security.check_command(command)
        self._security.audit(open_id, chat_id, command, cmd_ok, session_id)
        if not cmd_ok:
            await self._streamer.send_text(receive_id, receive_id_type, f"🚫 {cmd_reason}")
            return

        # 5. TUI program detection (full-screen interactive programs cannot be displayed in cards)
        tui_prog = _detect_tui(command)
        if tui_prog:
            await self._streamer.send_text(
                receive_id, receive_id_type,
                f"⚠️ `{tui_prog}` 是全屏交互程序，不支持在飞书卡片中运行。\n"
                f"请改用非交互替代方案，例如：\n"
                f"• `cat`/`head`/`tail` 替代 `vi`/`nano`\n"
                f"• `ps aux` 替代 `top`/`htop`\n"
                f"• `cat file | grep ...` 替代 `less`/`more`"
            )
            return

        # 6. Regular command: first send a "running..." placeholder message
        session = await self._sm.get_or_create(session_id)

        msg_id = await self._streamer.send_running_placeholder(
            receive_id, receive_id_type, command, session_id,
            reply_to_message_id=reply_to_message_id,
        )

        # 7. Collect output (stream_output writes the command internally, ensuring subscribe precedes write)
        raw_output = await self._streamer.stream_output(
            message_id=msg_id,
            session=session,
            command=command,
            timeout=120.0,
            session_id=session_id,
        )

        # 9. Send result card (new message) and update the placeholder message
        await self._streamer.finalize_card(
            message_id=msg_id,
            command=command,
            raw_output=raw_output,
            session_id=session_id,
            receive_id=receive_id,
            receive_id_type=receive_id_type,
        )

    # ------------------------------------------------------------------
    # Special commands
    # ------------------------------------------------------------------

    async def _handle_special(
        self,
        command: str,
        open_id: str,
        chat_id: str,
        session_id: str,
        receive_id: str,
        receive_id_type: str,
        reply_to_message_id: str = "",
    ) -> None:
        parts = command.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "/help":
            await self._streamer.send_text(receive_id, receive_id_type, HELP_TEXT)

        elif cmd == "/exit":
            destroyed = self._sm.destroy(session_id)
            msg = "✅ Shell 会话已关闭。" if destroyed else "ℹ️ 没有活跃的 Shell 会话。"
            await self._streamer.send_text(receive_id, receive_id_type, msg)
            logger.info("Session %s closed via /exit by %s", session_id, open_id)

        elif cmd == "/cd":
            if not arg:
                await self._streamer.send_text(receive_id, receive_id_type, "❌ 用法：`/cd <目录>`")
                return
            session = await self._sm.get_or_create(session_id)
            cd_cmd = f"cd {arg} && pwd"
            msg_id = await self._streamer.send_running_placeholder(
                receive_id, receive_id_type, cd_cmd, session_id,
                reply_to_message_id=reply_to_message_id,
            )
            raw = await self._streamer.stream_output(
                message_id=msg_id, session=session, command=cd_cmd, timeout=10.0,
                session_id=session_id,
            )
            await self._streamer.finalize_card(
                message_id=msg_id, command=cd_cmd, raw_output=raw, session_id=session_id,
                receive_id=receive_id, receive_id_type=receive_id_type,
            )

        elif cmd == "/status":
            sessions = self._sm.list_sessions()
            my = next((s for s in sessions if s.session_id == session_id), None)
            if my:
                idle = int(time.time() - my.last_active)
                text = (
                    f"**当前会话状态**\n"
                    f"• Session ID：`{session_id}`\n"
                    f"• PID：`{my.pid}`\n"
                    f"• 存活：{'✅' if my.is_alive else '❌'}\n"
                    f"• 空闲：{idle} 秒"
                )
            else:
                text = "ℹ️ 当前没有活跃的 Shell 会话，发送任意命令将自动创建。"
            await self._streamer.send_text(receive_id, receive_id_type, text)

        elif cmd == "/get":
            if not arg:
                await self._streamer.send_text(receive_id, receive_id_type, "❌ 用法：`/get <文件或目录路径>`")
                return
            try:
                import aiofiles
                from pathlib import Path as _Path
                path = _Path(arg)
                if not path.is_absolute():
                    # Relative path: resolve relative to the shell session's actual cwd
                    session = await self._sm.get_or_create(session_id)
                    try:
                        session_cwd = _Path(f"/proc/{session.pid}/cwd").resolve()
                    except Exception:
                        session_cwd = _Path.home()
                    path = (session_cwd / path).resolve()
                path_ok, path_reason = self._security.check_path(path)
                self._security.audit_file_access(open_id, chat_id, "/get", str(path), path_ok)
                if not path_ok:
                    await self._streamer.send_text(receive_id, receive_id_type, f"❌ {path_reason}")
                    return
                if not path.exists():
                    await self._streamer.send_text(receive_id, receive_id_type, f"❌ 路径不存在：`{arg}`")
                    return
                if path.is_dir():
                    await self._streamer.send_text(receive_id, receive_id_type, f"📦 正在打包 `{arg}`...")
                    loop = asyncio.get_event_loop()
                    content = await loop.run_in_executor(None, _zip_directory, path)
                    file_name = path.name + ".zip"
                else:
                    async with aiofiles.open(path, "rb") as f:
                        content = await f.read()
                    file_name = path.name
                self._last_get[open_id] = arg
                await self._streamer.send_file(receive_id, receive_id_type, file_name, content)
            except PermissionError:
                await self._streamer.send_text(receive_id, receive_id_type, f"❌ 无权限读取：`{arg}`")
            except FileTooLargeError as e:
                await self._streamer.send_text(receive_id, receive_id_type, str(e))
            except Exception as e:
                await self._streamer.send_text(receive_id, receive_id_type, f"❌ 读取失败：{e}")

        elif cmd == "/edit":
            if not arg:
                await self._streamer.send_text(receive_id, receive_id_type, "❌ 用法：`/edit <文件路径>`")
                return
            try:
                import aiofiles
                # Relative path is resolved relative to the shell session cwd (consistent with /get)
                edit_path = Path(arg).expanduser()
                if not edit_path.is_absolute():
                    session = await self._sm.get_or_create(session_id)
                    try:
                        session_cwd = Path(f"/proc/{session.pid}/cwd").resolve()
                    except Exception:
                        session_cwd = Path.home()
                    edit_path = (session_cwd / edit_path).resolve()
                abs_arg = str(edit_path)

                path_ok, path_reason = self._security.check_path(edit_path)
                self._security.audit_file_access(open_id, chat_id, "/edit", abs_arg, path_ok)
                if not path_ok:
                    await self._streamer.send_text(receive_id, receive_id_type, f"❌ {path_reason}")
                    return

                try:
                    async with aiofiles.open(abs_arg, "r", encoding="utf-8") as f:
                        content = await f.read()
                except FileNotFoundError:
                    content = ""

                logger.info("/edit: resolved path=%r content_len=%d", abs_arg, len(content))
                # Cancel any existing edit sessions for this file to prevent multiple polls writing to it concurrently
                self._cancel_edit_sessions_for_path(abs_arg)
                self._save_edit_state()
                api = self._streamer._api
                doc_id = await api.create_doc(title=edit_path.name)
                logger.info("/edit: created doc=%s", doc_id)
                fence = _build_fence(abs_arg, content)
                block_id = await api.append_doc_text_block(doc_id, fence)
                logger.info("/edit: wrote block=%s fence_len=%d", block_id, len(fence))

                self._edit_map[doc_id] = (abs_arg, open_id, receive_id, receive_id_type, time.time())
                self._edit_block_map[doc_id] = block_id
                import hashlib as _hashlib
                self._edit_content_hash[doc_id] = _hashlib.md5(content.encode()).hexdigest()
                self._save_edit_state()
                task = asyncio.create_task(self._poll_edit_doc(doc_id))
                self._bg_tasks.add(task)
                task.add_done_callback(self._bg_tasks.discard)

                cmd_str = f"larksh-client edit {abs_arg} --doc {doc_id} --block {block_id}"
                await self._streamer.send_edit_prompt(
                    receive_id, receive_id_type,
                    abs_path=abs_arg, doc_id=doc_id, block_id=block_id, cmd=cmd_str,
                )
                logger.info("Doc relay created: doc=%s path=%s open_id=%s", doc_id, abs_arg, open_id)
            except Exception as e:
                await self._streamer.send_text(receive_id, receive_id_type, f"❌ 创建编辑文档失败：{e}")

        elif cmd == "/edit-commit":
            # Sent by the client after editing is complete; bot reads the document and writes the content to the file
            doc_id_arg = arg.strip()
            if not doc_id_arg:
                await self._streamer.send_text(receive_id, receive_id_type, "❌ 用法：`/edit-commit <doc_id>`")
                return
            entry = self._edit_map.get(doc_id_arg)
            if not entry:
                await self._streamer.send_text(
                    receive_id, receive_id_type,
                    f"❌ 未找到文档 `{doc_id_arg}` 的编辑会话（已过期或服务重启？）"
                )
                return
            file_path, stored_oid, _rid, _rtype, _ts = entry
            if open_id != stored_oid:
                logger.warning(
                    "edit-commit: open_id mismatch doc=%s expected=%s got=%s",
                    doc_id_arg, stored_oid, open_id,
                )
                await self._streamer.send_text(
                    receive_id, receive_id_type,
                    f"❌ 文档 `{doc_id_arg}` 不属于你的编辑会话"
                )
                return
            try:
                import aiofiles
                raw = await self._streamer._api.get_doc_raw_content(doc_id_arg)
                logger.info("edit-commit: doc=%s raw_len=%d raw_head=%r", doc_id_arg, len(raw), raw[:120])
                content = _extract_fence_content(raw)
                if content is None:
                    await self._streamer.send_text(
                        receive_id, receive_id_type,
                        f"❌ 无法从文档提取内容，格式有误\n原始内容片段：\n```\n{raw[:200]}\n```"
                    )
                    return
                Path(file_path).parent.mkdir(parents=True, exist_ok=True)
                async with aiofiles.open(file_path, "w", encoding="utf-8") as f:
                    await f.write(content)
                self._edit_map.pop(doc_id_arg, None)
                self._edit_block_map.pop(doc_id_arg, None)
                self._edit_locks.pop(doc_id_arg, None)
                self._save_edit_state()
                await self._streamer.send_text(receive_id, receive_id_type, f"✅ `{file_path}` 已更新")
                logger.info("edit-commit: wrote %s from doc %s by open_id=%s", file_path, doc_id_arg, open_id)
            except Exception as e:
                logger.exception("edit-commit failed: doc=%s path=%s", doc_id_arg, file_path)
                await self._streamer.send_text(receive_id, receive_id_type, f"❌ 写入失败：{e}")

        elif cmd == "/save":
            upload = self._last_upload.get(open_id)
            if not upload:
                await self._streamer.send_text(receive_id, receive_id_type, "❌ 没有待保存的文件，请先上传文件。")
                return
            if not arg:
                await self._streamer.send_text(receive_id, receive_id_type, "❌ 用法：`/save <保存路径>`")
                return
            save_path = arg
            file_key = upload["file_key"]
            file_name = upload["file_name"]
            src_message_id = upload["message_id"]
            logger.info("save_file via /save: file_key=%r file_name=%r save_path=%r open_id=%s",
                        file_key, file_name, save_path, open_id)
            try:
                import aiofiles
                from pathlib import Path as _Path
                session = await self._sm.get_or_create(session_id)
                try:
                    session_cwd = _Path(f"/proc/{session.pid}/cwd").resolve()
                except Exception:
                    session_cwd = _Path.home()
                sp = _resolve_save_path(save_path, file_name, session_cwd)
                path_ok, path_reason = self._security.check_path(sp)
                self._security.audit_file_access(open_id, chat_id, "/save", str(sp), path_ok)
                if not path_ok:
                    await self._streamer.send_text(receive_id, receive_id_type, f"❌ {path_reason}")
                    return
                content = await self._streamer._api.download_file(file_key, message_id=src_message_id)
                logger.info("save_file via /save: downloaded %d bytes, saving to %r", len(content), str(sp))
                sp.parent.mkdir(parents=True, exist_ok=True)
                async with aiofiles.open(sp, "wb") as f:
                    await f.write(content)
                del self._last_upload[open_id]
                await self._streamer.send_text(receive_id, receive_id_type, f"✅ 已保存：`{sp}`")
                logger.info("File saved via /save: %s by open_id=%s", sp, open_id)
            except Exception as e:
                logger.exception("save_file via /save failed")
                await self._streamer.send_text(receive_id, receive_id_type, f"❌ 保存失败：{e}")

        elif cmd == "/kill":
            self._sm.destroy(session_id)
            session = await self._sm.get_or_create(session_id)
            await self._streamer.send_text(
                receive_id, receive_id_type,
                f"✅ 已强制终止并重建 Shell 会话（新 PID：{session.pid}）。"
            )

        else:
            await self._streamer.send_text(
                receive_id, receive_id_type,
                f"❓ 未知特殊命令：`{cmd}`，输入 `/help` 查看帮助。"
            )


def _resolve_save_path(save_arg: str, file_name: str, cwd: "Path") -> "Path":
    """
    Resolve the user-supplied save path to a final file path.

    Rules:
    - Expand ~ (home directory) first
    - Relative paths are resolved relative to cwd
    - If the path ends with '/', or the resolved path is an existing directory → append the original file_name
    - Otherwise treat it as a complete file path (including filename)

    Examples:
      ~/logs/          → /home/user/logs/<file_name>
      /tmp/            → /tmp/<file_name>
      /tmp/myfile.txt  → /tmp/myfile.txt
      logs/            → <cwd>/logs/<file_name>
      logs/backup.log  → <cwd>/logs/backup.log
    """
    from pathlib import Path
    p = Path(save_arg).expanduser()
    if not p.is_absolute():
        p = (cwd / p).resolve()
    else:
        p = p.resolve()
    # Path ends with / or is already a directory → append the original filename
    if save_arg.rstrip().endswith("/") or p.is_dir():
        p = p / file_name
    return p


_TUI_PROGRAMS = {
    "vi", "vim", "nvim", "nano", "emacs", "pico",
    "top", "htop", "btop", "atop", "iotop",
    "less", "more", "man",
    "watch",
    "screen", "tmux",
    "mc", "ranger", "nnn",
    "python", "python3", "ipython", "irb", "node", "lua", "R",
    "mysql", "psql", "sqlite3", "redis-cli", "mongo",
    "ssh", "telnet", "ftp", "sftp",
}


def _detect_tui(command: str) -> str:
    """Detect whether the command launches a full-screen TUI program; returns the program name if so, otherwise an empty string."""
    # Extract the first token (strip any path prefix)
    first = command.strip().split()[0] if command.strip() else ""
    prog = first.split("/")[-1]  # handle absolute paths like /usr/bin/vim
    return prog if prog in _TUI_PROGRAMS else ""


FEISHU_FILE_LIMIT = 30 * 1024 * 1024  # 30 MB Feishu single-transfer limit


class FileTooLargeError(Exception):
    pass


def _zip_directory(path) -> bytes:
    """Pack a directory into a zip archive and return the raw bytes; raises FileTooLargeError if it exceeds 30 MB."""
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for file in sorted(path.rglob("*")):
            if file.is_file():
                zf.write(file, file.relative_to(path.parent))
                if buf.tell() > FEISHU_FILE_LIMIT:
                    raise FileTooLargeError(
                        f"❌ 打包后超过 30 MB，飞书不支持传输此大小的文件。\n"
                        f"建议压缩后用 `split` 分片或只传需要的子目录。"
                    )
    content = buf.getvalue()
    if len(content) > FEISHU_FILE_LIMIT:
        raise FileTooLargeError(
            f"❌ 文件大小 {len(content) / 1024 / 1024:.1f} MB，超过飞书 30 MB 限制。"
        )
    return content


def _strip_mention(text: str) -> str:
    """Remove the @-bot mention markup from a Feishu message."""
    text = re.sub(r"<at[^>]*>.*?</at>", "", text)
    return text.strip()
