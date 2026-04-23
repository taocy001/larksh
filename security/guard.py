"""Security control — user allowlist validation + command blacklist filtering + path access control + audit log"""
from __future__ import annotations

import fnmatch
import logging
from pathlib import Path

from .audit import AuditLogger

logger = logging.getLogger(__name__)


class SecurityGuard:
    def __init__(self, config):
        sec = config.get("security", {})
        self._allowed_users: set[str] = set(sec.get("allowed_users", []))
        self._allowed_groups: set[str] = set(sec.get("allowed_groups", []))
        self._cmd_blacklist: list[str] = sec.get("command_blacklist", [])
        # File access path allowlist (empty list = no restriction)
        raw_allowlist: list[str] = sec.get("path_allowlist", [])
        self._path_allowlist: list[Path] = [Path(p).resolve() for p in raw_allowlist]
        audit_path = sec.get("audit_log", "/var/log/larksh/audit.jsonl")
        self._audit = AuditLogger(audit_path)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def check_user(self, open_id: str, chat_id: str) -> tuple[bool, str]:
        """Returns (allowed, reason)"""
        if open_id in self._allowed_users:
            return True, ""
        if chat_id and chat_id in self._allowed_groups:
            return True, ""
        reason = f"用户 {open_id} 未在白名单中，拒绝执行"
        logger.warning("Auth denied: open_id=%s chat_id=%s", open_id, chat_id)
        return False, reason

    def check_command(self, command: str) -> tuple[bool, str]:
        """Returns (allowed, reason)"""
        stripped = command.strip()
        for pattern in self._cmd_blacklist:
            if fnmatch.fnmatch(stripped, pattern):
                reason = f"命令被安全策略拒绝（匹配规则：{pattern}）"
                logger.warning("Command blocked: %r matches pattern %r", stripped, pattern)
                return False, reason
        return True, ""

    def check_path(self, path: Path) -> tuple[bool, str]:
        """Check whether the path is within the allowed scope. Always permits if path_allowlist is not configured."""
        if not self._path_allowlist:
            return True, ""
        resolved = path.resolve()
        for allowed_base in self._path_allowlist:
            try:
                resolved.relative_to(allowed_base)
                return True, ""
            except ValueError:
                continue
        allowed_str = ", ".join(str(p) for p in self._path_allowlist)
        reason = f"路径 `{path}` 不在允许的目录范围内（{allowed_str}）"
        logger.warning("Path access denied: %s", resolved)
        return False, reason

    def audit(
        self,
        open_id: str,
        chat_id: str,
        command: str,
        allowed: bool,
        session_id: str = "",
    ) -> None:
        self._audit.write(
            {
                "open_id": open_id,
                "chat_id": chat_id,
                "session_id": session_id,
                "command": command,
                "allowed": allowed,
            }
        )

    def audit_file_access(
        self,
        open_id: str,
        chat_id: str,
        action: str,
        path: str,
        allowed: bool,
    ) -> None:
        """Record file access (/get, /save, /edit) to the audit log."""
        self._audit.write(
            {
                "open_id": open_id,
                "chat_id": chat_id,
                "action": action,
                "path": path,
                "allowed": allowed,
            }
        )
