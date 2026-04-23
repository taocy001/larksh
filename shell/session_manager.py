"""ShellSessionManager — Shell session pool

Responsibilities
----------------
- Maintains the session_id → ShellSession mapping
- Generates isolated session IDs based on Feishu chat_type/chat_id/open_id
- Lazy creation: creates the bash process and completes initialization on first use
- Background cleanup: periodically reclaims timed-out or dead sessions
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from .session import ShellSession
from .types import SessionInfo

logger = logging.getLogger(__name__)


def make_session_id(chat_type: str, chat_id: str, open_id: str) -> str:
    """Generate an isolated session ID.

    Strategy:
    - p2p (direct message): ``user_{open_id}``  — one persistent bash per user, shared across groups
    - group (group chat):   ``group_{chat_id}_{open_id}``  — each user in a group gets an independent bash
    """
    if chat_type == "p2p":
        return f"user_{open_id}"
    return f"group_{chat_id}_{open_id}"


class ShellSessionManager:
    def __init__(self, config) -> None:
        self._config = config
        self._sessions: dict[str, ShellSession] = {}
        shell_cfg = config.get("shell", {})
        self._timeout: float = float(shell_cfg.get("session_timeout", 3600))

    # ------------------------------------------------------------------
    # Core interface
    # ------------------------------------------------------------------

    def session_id_for(self, chat_type: str, chat_id: str, open_id: str) -> str:
        return make_session_id(chat_type, chat_id, open_id)

    async def get_or_create(self, session_id: str) -> ShellSession:
        """Return an existing session, or create and initialize a new one (async).

        - If a live session already exists, return it directly (refreshing the active timestamp)
        - If a session exists but is dead, clean it up first then create a new one
        - On creation, awaits initialize() to ensure bash is ready
        """
        existing = self._sessions.get(session_id)
        if existing:
            if existing.is_alive():
                existing.last_active = time.time()
                return existing
            # Dead: clean up
            logger.info("Session %s: dead, recreating", session_id)
            existing.terminate()
            del self._sessions[session_id]

        session = ShellSession(session_id, self._config)
        loop = asyncio.get_running_loop()
        session.start_reading(loop)
        self._sessions[session_id] = session

        # Wait for bash initialization to complete (stty -echo / PS1='' etc. are injected here)
        await session.initialize()
        logger.info(
            "Created session %s (pid=%d)", session_id, session.pid
        )
        return session

    def get(self, session_id: str) -> Optional[ShellSession]:
        """Synchronously retrieve an existing live session (does not create one)."""
        s = self._sessions.get(session_id)
        if s and s.is_alive():
            return s
        return None

    def destroy(self, session_id: str) -> bool:
        s = self._sessions.pop(session_id, None)
        if s:
            s.terminate()
            logger.info("Destroyed session %s", session_id)
            return True
        return False

    def list_sessions(self) -> list[SessionInfo]:
        now = time.time()
        result = []
        for sid, s in list(self._sessions.items()):
            result.append(
                SessionInfo(
                    session_id=sid,
                    pid=s.pid,
                    created_at=s.created_at,
                    last_active=s.last_active,
                    is_alive=s.is_alive(),
                )
            )
        return result

    # ------------------------------------------------------------------
    # Background cleanup task
    # ------------------------------------------------------------------

    async def cleanup_loop(self) -> None:
        """Scan every 60 seconds and reclaim timed-out or dead sessions."""
        while True:
            await asyncio.sleep(60)
            now = time.time()
            expired = [
                sid
                for sid, s in list(self._sessions.items())
                if not s.is_alive() or (now - s.last_active > self._timeout)
            ]
            for sid in expired:
                logger.info("Reaping expired session: %s", sid)
                self.destroy(sid)
            if expired:
                logger.info("Reaped %d expired session(s)", len(expired))
