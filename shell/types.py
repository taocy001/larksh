"""Common data types for the shell module."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CommandResult:
    """Execution result for a single command."""

    command: str
    output: str           # cleaned, human-readable text (ANSI escapes stripped)
    exit_code: int        # process exit code; -1 means timed out / interrupted
    timed_out: bool
    duration_ms: float = 0.0
    session_id: str = ""
    interactive_hint: Optional[str] = None   # non-empty when an interactive prompt was detected


@dataclass
class SessionInfo:
    """Session state snapshot (for external inspection)."""

    session_id: str
    pid: int
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    command_count: int = 0
    is_alive: bool = True
