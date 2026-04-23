"""CardKit streaming output pusher — sends a placeholder first, then streams actual output"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING

import httpx

from utils.ansi import strip_ansi, truncate_output

if TYPE_CHECKING:
    from shell.session import ShellSession

logger = logging.getLogger(__name__)

# Message send APIs
MSG_SEND   = "https://open.feishu.cn/open-apis/im/v1/messages"
MSG_REPLY  = "https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply"
MSG_UPDATE = "https://open.feishu.cn/open-apis/im/v1/messages/{message_id}"

# Feishu document API
DOCX_BASE = "https://open.feishu.cn/open-apis/docx/v1/documents"



class FeishuApiClient:
    """Wraps Feishu HTTP API calls (with automatic token refresh)."""

    TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"

    def __init__(self, app_id: str, app_secret: str):
        self._app_id = app_id
        self._app_secret = app_secret
        self._token: str = ""
        self._token_expire: float = 0.0
        self._http = httpx.AsyncClient(timeout=30.0)

    async def _get_token(self) -> str:
        if time.time() < self._token_expire - 60:
            return self._token
        resp = await self._http.post(
            self.TOKEN_URL,
            json={"app_id": self._app_id, "app_secret": self._app_secret},
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["tenant_access_token"]
        self._token_expire = time.time() + data.get("expire", 7200)
        return self._token

    async def _headers(self) -> dict:
        token = await self._get_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }

    async def post(self, url: str, payload: dict) -> dict:
        headers = await self._headers()
        resp = await self._http.post(url, headers=headers, json=payload)
        if resp.status_code == 401:
            self._token_expire = 0
            headers = await self._headers()
            resp = await self._http.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        return resp.json()

    async def patch(self, url: str, payload: dict) -> dict:
        headers = await self._headers()
        resp = await self._http.patch(url, headers=headers, json=payload)
        if resp.status_code == 401:
            self._token_expire = 0
            headers = await self._headers()
            resp = await self._http.patch(url, headers=headers, json=payload)
        resp.raise_for_status()
        return resp.json()

    async def delete(self, url: str) -> None:
        headers = await self._headers()
        resp = await self._http.delete(url, headers=headers)
        resp.raise_for_status()

    async def put(self, url: str, payload: dict) -> dict:
        headers = await self._headers()
        resp = await self._http.put(url, headers=headers, json=payload)
        resp.raise_for_status()
        return resp.json()

    async def upload_file(self, file_name: str, content: bytes) -> str:
        """Upload a file to Feishu and return its file_key; raises ValueError for empty or >30 MB files."""
        if len(content) == 0:
            raise ValueError("文件为空，无法上传")
        limit = 30 * 1024 * 1024
        if len(content) > limit:
            raise ValueError(
                f"文件大小 {len(content) / 1024 / 1024:.1f} MB，超过飞书 30 MB 限制"
            )
        token = await self._get_token()
        headers = {"Authorization": f"Bearer {token}"}
        resp = await self._http.post(
            "https://open.feishu.cn/open-apis/im/v1/files",
            headers=headers,
            data={"file_type": "stream", "file_name": file_name},
            files={"file": (file_name, content)},
        )
        if not resp.is_success:
            logger.error("upload_file failed %s: %s", resp.status_code, resp.text)
            body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            if body.get("code") == 99991672:
                raise PermissionError("飞书应用缺少 im:resource 权限，请在开放平台控制台开通")
        resp.raise_for_status()
        return resp.json()["data"]["file_key"]

    async def download_file(self, file_key: str, message_id: str = "") -> bytes:
        """Download a Feishu file and return its raw bytes.
        Files sent by users in chat must be downloaded via the messages/{id}/resources endpoint.
        """
        token = await self._get_token()
        headers = {"Authorization": f"Bearer {token}"}
        if message_id:
            url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{file_key}?type=file"
        else:
            url = f"https://open.feishu.cn/open-apis/im/v1/files/{file_key}"
        resp = await self._http.get(url, headers=headers)
        if not resp.is_success:
            logger.error("download_file failed %s: %s", resp.status_code, resp.text)
            body = {}
            try:
                body = resp.json()
            except Exception:
                pass
            if body.get("code") == 99991672:
                raise PermissionError("飞书应用缺少 im:resource 权限，请在开放平台控制台开通")
        resp.raise_for_status()
        return resp.content

    async def get_bot_open_id(self) -> str:
        """Return the current bot's open_id (used to filter document events triggered by the bot's own writes)."""
        headers = await self._headers()
        resp = await self._http.get(
            "https://open.feishu.cn/open-apis/bot/v3/info",
            headers=headers,
        )
        resp.raise_for_status()
        body = resp.json()
        # v3 API: bot field is at the top level (no data wrapper)
        if "bot" in body:
            return body["bot"]["open_id"]
        return body["data"]["bot"]["open_id"]

    async def create_doc(self, title: str) -> str:
        """Create a Feishu document and return its document_id (i.e. file_token)."""
        resp = await self.post(DOCX_BASE, {"title": title})
        return resp["data"]["document"]["document_id"]

    async def append_doc_text_block(self, doc_id: str, content: str) -> str:
        """Append a text block at the end of the document and return its block_id.

        Feishu caps a single text_run at ~10000 UTF-16 code units; oversized content is
        automatically split into multiple runs.
        """
        # Split into 8000-character chunks, conservatively below the Feishu limit
        CHUNK = 8000
        chunks = [content[i: i + CHUNK] for i in range(0, max(len(content), 1), CHUNK)]
        elements = [{"text_run": {"content": chunk}} for chunk in chunks]

        payload = {
            "children": [
                {
                    "block_type": 2,
                    "text": {"elements": elements, "style": {}},
                }
            ],
            "index": -1,
        }
        resp = await self.post(
            f"{DOCX_BASE}/{doc_id}/blocks/{doc_id}/children",
            payload,
        )
        return resp["data"]["children"][0]["block_id"]

    async def get_doc_raw_content(self, doc_id: str) -> str:
        """Fetch the plain-text content of a document."""
        headers = await self._headers()
        resp = await self._http.get(
            f"{DOCX_BASE}/{doc_id}/raw_content",
            headers=headers,
            params={"lang": 0},
        )
        resp.raise_for_status()
        return resp.json()["data"]["content"]

    async def update_doc_text_block(self, doc_id: str, block_id: str, content: str) -> None:
        """Replace the entire content of a text block; oversized content is auto-split into multiple text_runs."""
        CHUNK = 8000
        chunks = [content[i: i + CHUNK] for i in range(0, max(len(content), 1), CHUNK)]
        elements = [{"text_run": {"content": chunk}} for chunk in chunks]
        await self.patch(
            f"{DOCX_BASE}/{doc_id}/blocks/{block_id}",
            {"update_text_elements": {"elements": elements}},
        )

    async def close(self) -> None:
        await self._http.aclose()


class CardStreamer:
    """
    Feishu card streaming output pusher.

    Flow:
    1. Send a placeholder message (plain text, obtain message_id)
    2. Once the command finishes, edit the message into a card with a code block (full output)
    3. The card includes a next-command input field
    """

    def __init__(self, api_client: FeishuApiClient, config):
        self._api = api_client
        out_cfg = config.get("output", {})
        self._max_chars: int = out_cfg.get("max_output_chars", 8000)
        self._max_lines: int = out_cfg.get("max_lines", 100)
        # Streaming push interval: 100ms triggers Feishu rate-limiting, default is 2000ms; set 0 to disable streaming
        self._push_interval: float = out_cfg.get("push_interval_ms", 2000) / 1000.0
        self._idle_timeout: float = out_cfg.get("idle_timeout_ms", 500) / 1000.0

    # ------------------------------------------------------------------
    # Send placeholder "running..." message
    # ------------------------------------------------------------------

    async def send_running_placeholder(
        self,
        receive_id: str,
        receive_id_type: str,
        command: str,
        session_id: str = "",
        reply_to_message_id: str = "",
    ) -> str:
        """Send an interactive "running" card (with Ctrl+C button) and return the message_id.
        If reply_to_message_id is provided, the card is sent as a reply to that message.
        Once the command completes, finalize_card updates it in-place with the result card.
        """
        # CardKit 1.0 (no schema:2.0); button callbacks are delivered via MessageType.EVENT
        card = {
            "config": {"update_multi": True, "wide_screen_mode": True},
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": f"**$ {command}**\n⏳ 执行中..."},
                },
                {"tag": "hr"},
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "Ctrl+C"},
                            "type": "danger",
                            "value": {"action": "ctrl_c", "session_id": session_id},
                        },
                    ],
                },
            ],
        }
        if reply_to_message_id:
            resp = await self._api.post(
                MSG_REPLY.format(message_id=reply_to_message_id),
                {"msg_type": "interactive", "content": json.dumps(card)},
            )
        else:
            resp = await self._api.post(
                f"{MSG_SEND}?receive_id_type={receive_id_type}",
                {"receive_id": receive_id, "msg_type": "interactive", "content": json.dumps(card)},
            )
        msg_id = resp.get("data", {}).get("message_id", "")
        logger.debug("Sent running placeholder: message_id=%s", msg_id)
        return msg_id

    # ------------------------------------------------------------------
    # Streaming push (collect output + periodic PATCH card)
    # ------------------------------------------------------------------

    async def stream_output(
        self,
        message_id: str,
        session: "ShellSession",
        command: str,
        timeout: float = 60.0,
        session_id: str = "",
    ) -> str:
        """
        Subscribe to session output, use a sentinel to detect command completion, and return
        the full output text (excluding the sentinel line).
        If push_interval > 0, PATCH the card at that interval to show live output during execution.
        """
        import uuid as _uuid
        sentinel = f"__LARKSH_DONE_{_uuid.uuid4().hex}__"
        output_chunks: list[str] = []
        done_event = asyncio.Event()

        async def on_output(text: str) -> None:
            output_chunks.append(strip_ansi(text))
            if len(output_chunks) > 10000:
                output_chunks[:] = output_chunks[-5000:]
            if sentinel in text:
                done_event.set()

        async def _push_loop() -> None:
            """Periodically push the current output to the Feishu card (intermediate state) for real-time feedback."""
            last_raw: str = ""
            while not done_event.is_set():
                await asyncio.sleep(self._push_interval)
                if done_event.is_set():
                    break
                current_raw = "".join(output_chunks)
                if not current_raw or current_raw == last_raw:
                    continue
                last_raw = current_raw
                preview, _ = truncate_output(current_raw, self._max_chars, self._max_lines)
                card = self._build_streaming_card(command, preview, session_id)
                try:
                    await self._api.patch(
                        MSG_UPDATE.format(message_id=message_id),
                        {"content": json.dumps(card)},
                    )
                except Exception:
                    logger.debug("stream push patch failed", exc_info=True)

        session.subscribe(on_output)
        session.write(command + "\n")       # write command after subscribing to ensure no output is lost
        session.write(f"echo {sentinel}\n")

        push_task = None
        if message_id and self._push_interval > 0:
            push_task = asyncio.create_task(_push_loop())

        try:
            try:
                await asyncio.wait_for(done_event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning("stream_output timeout for command: %r", command)
                session.send_ctrl_c()  # terminate the still-running command to avoid polluting the next one
        finally:
            session.unsubscribe(on_output)
            if push_task and not push_task.done():
                push_task.cancel()
                try:
                    await push_task
                except asyncio.CancelledError:
                    pass

        raw = "".join(output_chunks)
        sentinel_idx = raw.find(sentinel)
        if sentinel_idx != -1:
            raw = raw[:sentinel_idx]
        return raw

    # ------------------------------------------------------------------
    # Finalize card (full output + input field)
    # ------------------------------------------------------------------

    async def finalize_card(
        self,
        message_id: str,
        command: str,
        raw_output: str,
        receive_id: str = "",
        receive_id_type: str = "chat_id",
        session_id: str = "",
    ) -> None:
        """PATCH the running card into a result card (in-place update, no flicker); falls back to a new message on failure."""
        cleaned = strip_ansi(raw_output)
        cleaned = _strip_command_echo(cleaned, command)
        truncated_output, was_truncated = truncate_output(cleaned, self._max_chars, self._max_lines)

        card = self._build_result_card(
            command=command,
            output=truncated_output,
            truncated=was_truncated,
        )

        if message_id:
            try:
                await self._api.patch(
                    MSG_UPDATE.format(message_id=message_id),
                    {"content": json.dumps(card)},
                )
                return
            except Exception:
                logger.warning(
                    "PATCH card failed, falling back to new message", exc_info=True
                )

        # Fallback: send a new message
        if receive_id:
            await self._api.post(
                f"{MSG_SEND}?receive_id_type={receive_id_type}",
                {"receive_id": receive_id, "msg_type": "interactive", "content": json.dumps(card)},
            )

    # ------------------------------------------------------------------
    # Send a simple text message
    # ------------------------------------------------------------------

    async def send_text(self, receive_id: str, receive_id_type: str, text: str) -> str:
        """Send a message (lark_md card, supports Markdown rendering) and return the message_id."""
        card = {
            "schema": "2.0",
            "body": {
                "elements": [
                    {"tag": "div", "text": {"tag": "lark_md", "content": text}},
                ]
            },
        }
        resp = await self._api.post(
            f"{MSG_SEND}?receive_id_type={receive_id_type}",
            {"receive_id": receive_id, "msg_type": "interactive", "content": json.dumps(card)},
        )
        return resp.get("data", {}).get("message_id", "")

    # ------------------------------------------------------------------
    # Card construction
    # ------------------------------------------------------------------

    async def send_file(
        self,
        receive_id: str,
        receive_id_type: str,
        file_name: str,
        content: bytes,
    ) -> None:
        """Upload a file and send it as an attachment."""
        file_key = await self._api.upload_file(file_name, content)
        await self._api.post(
            f"{MSG_SEND}?receive_id_type={receive_id_type}",
            {
                "receive_id": receive_id,
                "msg_type": "file",
                "content": json.dumps({"file_key": file_key}),
            },
        )

    async def send_save_prompt(
        self,
        receive_id: str,
        receive_id_type: str,
        file_key: str,
        file_name: str,
        default_path: str,
        message_id: str = "",
    ) -> None:
        """After receiving a user-uploaded file, show a card for the user to confirm the save path.

        Uses CardKit 1.0 format (no schema:2.0); button callbacks are delivered via MessageType.EVENT,
        fully compatible with the lark_oapi WS client — no patches required.
        """
        # Fall back to ~/filename when no default path is provided
        save_path = default_path or f"~/{file_name}"
        body_text = f"收到文件 `{file_name}`\n保存到 `{save_path}`？\n其他路径请发送 `/save <路径>`"
        actions = [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "保存"},
                "type": "primary",
                "value": {
                    "action": "save_file",
                    "file_key": file_key,
                    "file_name": file_name,
                    "save_path": save_path,
                    "message_id": message_id,
                },
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "取消"},
                "type": "default",
                "value": {"action": "cancel_save"},
            },
        ]

        card = {
            "config": {"wide_screen_mode": True},
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": body_text}},
                {"tag": "hr"},
                {"tag": "action", "actions": actions},
            ],
        }
        await self._api.post(
            f"{MSG_SEND}?receive_id_type={receive_id_type}",
            {"receive_id": receive_id, "msg_type": "interactive", "content": json.dumps(card)},
        )

    async def send_edit_prompt(
        self,
        receive_id: str,
        receive_id_type: str,
        abs_path: str,
        doc_id: str,
        block_id: str,
        cmd: str,
    ) -> None:
        """Send the /edit ready message; the code block includes Feishu's native copy icon in the top-right corner."""
        text = (
            f"📝 **文档已就绪**\n"
            f"服务器路径：`{abs_path}`\n"
            f"编辑保存后约 5 秒自动写入服务器。\n\n"
            f"```shell\n{cmd}\n```"
        )
        await self.send_text(receive_id, receive_id_type, text)

    def _build_streaming_card(self, command: str, output: str, session_id: str = "") -> dict:
        """Build the intermediate-state card shown while a command is running (with live output + Ctrl+C button)."""
        stripped = output.strip()
        output_block = f"```\n{stripped}\n```" if stripped else "_（等待输出...）_"
        elements = [
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**$ {command}**\n⏳ 执行中..."}},
            {"tag": "div", "text": {"tag": "lark_md", "content": output_block}},
        ]
        if session_id:
            elements.append({
                "tag": "action",
                "actions": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "Ctrl+C"},
                    "type": "danger",
                    "value": {"action": "ctrl_c", "session_id": session_id},
                }],
            })
        return {"schema": "2.0", "body": {"elements": elements}}

    def _build_result_card(
        self,
        command: str,
        output: str,
        truncated: bool,
    ) -> dict:
        stripped = output.strip()
        output_block = f"```\n{stripped}\n```" if stripped else "_（无输出）_"
        if truncated:
            output_block += "\n\n> ⚠️ 输出过长，已截断。可将输出重定向到文件后用 `/get` 取回。"

        return {
            "schema": "2.0",
            "body": {
                "elements": [
                    {
                        "tag": "div",
                        "text": {"tag": "lark_md", "content": f"**$ {command}**"},
                    },
                    {
                        "tag": "div",
                        "text": {"tag": "lark_md", "content": output_block},
                    },
                ]
            },
        }



def _strip_command_echo(output: str, command: str) -> str:
    """Remove the first PTY-echoed input line (rarely appears when PTY echo is disabled; this is a safety measure)."""
    cmd_stripped = command.rstrip()
    lines = output.splitlines(keepends=True)
    result = []
    removed = False
    for line in lines:
        if not removed and line.rstrip("\r\n").endswith(cmd_stripped):
            removed = True  # only remove the first occurrence to avoid accidentally deleting real output
            continue
        result.append(line)
    return "".join(result)


