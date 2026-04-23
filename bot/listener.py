"""Feishu event listener — supports both WebSocket long-connection and Webhook modes"""
from __future__ import annotations

import asyncio
import json
import logging

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from lark_oapi.api.drive.v1 import P2DriveFileEditV1  # noqa: F401 — triggers event type registration

logger = logging.getLogger(__name__)


class BotListener:
    """
    Establishes Feishu event ingestion, supporting two modes:
    - websocket (default): lark_oapi WS client, no public internet required, direct intranet connection
    - webhook: HTTP callback, requires public internet or intranet tunneling, called by FastAPI routes
    """

    def __init__(self, config, dispatcher):
        self._config = config
        self._dispatcher = dispatcher
        self._mode = config.feishu.get("event_mode", "websocket")

        feishu = config.feishu
        self._app_id: str = feishu.app_id
        self._app_secret: str = feishu.app_secret

        # Build lark_oapi client (for active API calls; the listener is built separately)
        log_level = _parse_log_level(config.get("logging", {}).get("level", "INFO"))
        self.client = (
            lark.Client.builder()
            .app_id(self._app_id)
            .app_secret(self._app_secret)
            .log_level(log_level)
            .build()
        )

        self._ws_client = None
        self._webhook_handler = None

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def start_websocket(self) -> None:
        """Start WebSocket long-connection mode (blocking, with automatic reconnection)."""
        handler = self._build_event_handler()
        self._ws_client = lark.ws.Client(
            self._app_id,
            self._app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.INFO,
        )
        _patch_ws_card_handling(self._ws_client, handler)
        logger.info("Starting WebSocket event listener...")
        self._ws_client.start()  # blocking

    def start_websocket_in_thread(self) -> None:
        """Start WebSocket in a background daemon thread, freeing the main thread for signal handling."""
        import threading
        t = threading.Thread(target=self.start_websocket, name="ws-listener", daemon=True)
        t.start()

    def build_webhook_handler(self):
        """Return the event handler for Webhook mode (to be called by FastAPI routes)."""
        webhook_cfg = self._config.feishu.get("webhook", {})
        verification_token = webhook_cfg.get("verification_token", "")
        encrypt_key = webhook_cfg.get("encrypt_key", "")
        return self._build_event_handler(verification_token, encrypt_key)

    # ------------------------------------------------------------------
    # Event handler registration
    # ------------------------------------------------------------------

    def _build_event_handler(
        self,
        verification_token: str = "",
        encrypt_key: str = "",
    ) -> lark.EventDispatcherHandler:
        d = self._dispatcher
        handler = (
            lark.EventDispatcherHandler.builder(verification_token, encrypt_key)
            .register_p2_im_message_receive_v1(d.on_message)
            .register_p2_card_action_trigger(d.on_card_action)
            .register_p2_drive_file_edit_v1(d.on_doc_edit)
            .build()
        )
        return handler


def _patch_ws_card_handling(ws_client, handler) -> None:
    """
    The lark_oapi WS client silently drops MessageType.CARD messages by returning early,
    which prevents card action callbacks from ever firing. This patch routes CARD messages
    through the same handling path as EVENT messages.
    """
    import base64
    import http
    import json as _json
    import types

    from lark_oapi.ws.client import _get_by_key
    from lark_oapi.ws.const import (
        HEADER_TYPE, HEADER_MESSAGE_ID, HEADER_TRACE_ID, HEADER_SUM, HEADER_SEQ, HEADER_BIZ_RT,
    )
    from lark_oapi.ws.enum import MessageType
    from lark_oapi.ws.model import Response
    from lark_oapi.core.json import JSON

    async def patched_handle_data_frame(self, frame):
        hs = frame.headers
        msg_id = _get_by_key(hs, HEADER_MESSAGE_ID)
        trace_id = _get_by_key(hs, HEADER_TRACE_ID)
        sum_ = _get_by_key(hs, HEADER_SUM)
        seq = _get_by_key(hs, HEADER_SEQ)
        type_ = _get_by_key(hs, HEADER_TYPE)

        pl = frame.payload
        if int(sum_) > 1:
            pl = self._combine(msg_id, int(sum_), int(seq), pl)
            if pl is None:
                return

        message_type = MessageType(type_)
        try:
            _body = _json.loads(pl)
            _event_type = (_body.get("header") or {}).get("event_type", "")
        except Exception:
            _event_type = ""
        logger.info("ws_patch: type=%s event_type=%s msg_id=%s", type_, _event_type, msg_id)

        if message_type not in (MessageType.EVENT, MessageType.CARD):
            return

        resp = Response(code=http.HTTPStatus.OK)
        try:
            result = handler.do_without_validation(pl)
            if result is not None:
                header = hs.add()
                header.key = HEADER_BIZ_RT
                header.value = str(0)
                resp.data = base64.b64encode(JSON.marshal(result).encode("utf-8"))
        except Exception:
            logger.exception("ws_patch: handle failed type=%s event_type=%s msg_id=%s",
                             type_, _event_type, msg_id)
            resp = Response(code=http.HTTPStatus.INTERNAL_SERVER_ERROR)

        frame.payload = JSON.marshal(resp).encode("utf-8")
        await self._write_message(frame.SerializeToString())

    ws_client._handle_data_frame = types.MethodType(patched_handle_data_frame, ws_client)


def _parse_log_level(level_str: str) -> lark.LogLevel:
    mapping = {
        "DEBUG": lark.LogLevel.DEBUG,
        "INFO": lark.LogLevel.INFO,
        "WARNING": lark.LogLevel.WARNING,
        "ERROR": lark.LogLevel.ERROR,
    }
    return mapping.get(level_str.upper(), lark.LogLevel.INFO)
