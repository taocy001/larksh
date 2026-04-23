#!/usr/bin/env python3
"""
larksh — Feishu intranet Shell control service
Entry point

Usage:
  python main.py [--config config.yaml]

Environment variable overrides:
  FEISHU_APP_ID         Feishu application App ID
  FEISHU_APP_SECRET     Feishu application App Secret
  LARKSH_CONFIG         Path to config file
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import threading
from pathlib import Path

# Ensure the project root directory is in sys.path
sys.path.insert(0, str(Path(__file__).parent))

from utils.config import load_config
from security.guard import SecurityGuard
from shell.session_manager import ShellSessionManager
from messaging.streamer import CardStreamer, FeishuApiClient
from bot.dispatcher import CommandDispatcher
from bot.listener import BotListener


def setup_logging(level_str: str = "INFO") -> None:
    level = getattr(logging, level_str.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> None:
    parser = argparse.ArgumentParser(description="Feishu intranet Shell control service")
    parser.add_argument("--config", "-c", help="Path to config file", default=None)
    args = parser.parse_args()

    config = load_config(args.config)
    log_cfg = config.get("logging", {})
    setup_logging(log_cfg.get("level", "INFO"))
    logger = logging.getLogger("larksh")

    logger.info("=" * 60)
    logger.info("larksh starting")
    logger.info("=" * 60)

    feishu_cfg = config.feishu
    api_client = FeishuApiClient(feishu_cfg.app_id, feishu_cfg.app_secret)
    security = SecurityGuard(config)
    session_manager = ShellSessionManager(config)
    streamer = CardStreamer(api_client, config)

    # Event loop for the async worker thread (used for API calls, shell sessions, etc.)
    async_loop = asyncio.new_event_loop()

    dispatcher = CommandDispatcher(
        session_manager=session_manager,
        streamer=streamer,
        security=security,
        config=config,
        main_loop=async_loop,
    )
    listener = BotListener(config, dispatcher)

    # ----------------------------------------------------------------
    # Async worker thread
    # ----------------------------------------------------------------

    def _run_async():
        asyncio.set_event_loop(async_loop)
        async_loop.create_task(session_manager.cleanup_loop())
        async_loop.create_task(dispatcher.fetch_bot_open_id())
        async_loop.run_forever()

    async_thread = threading.Thread(
        target=_run_async,
        name="async-worker",
        daemon=True,
    )
    async_thread.start()

    logger.info("Feishu WebSocket long connection started")

    # ----------------------------------------------------------------
    # Graceful shutdown
    # ----------------------------------------------------------------

    _shutdown_event = threading.Event()

    def _do_shutdown():
        """Call from any thread: triggers async cleanup and waits for completion (up to 10s)."""
        if _shutdown_event.is_set():
            return
        _shutdown_event.set()
        logger.info("Starting graceful shutdown...")
        done = threading.Event()

        async def _async_shutdown():
            try:
                logger.info(
                    "async shutdown: cancelling %d background task(s)",
                    len(dispatcher._bg_tasks),
                )
                for task in list(dispatcher._bg_tasks):
                    task.cancel()
                if dispatcher._bg_tasks:
                    await asyncio.wait(dispatcher._bg_tasks, timeout=8.0)
                dispatcher._save_edit_state()
                await api_client.close()
                logger.info("async shutdown: done")
            finally:
                done.set()
                async_loop.stop()

        asyncio.run_coroutine_threadsafe(_async_shutdown(), async_loop)
        done.wait(timeout=10)
        logger.info("larksh stopped")

    def _signal_handler(signum, frame):
        logger.info("Received signal %s", signal.Signals(signum).name)
        _do_shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    # WS runs in a daemon thread (lark_oapi ws.Client has no stop() method, exits with process)
    # Main thread blocks on _shutdown_event to ensure signals are caught by the main thread
    listener.start_websocket_in_thread()
    _shutdown_event.wait()  # Wait for signal or other trigger
    async_thread.join(timeout=12)


if __name__ == "__main__":
    main()
