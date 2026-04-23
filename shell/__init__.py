"""shell — PTY Shell execution engine

Public interface:
- ShellSession        A single persistent bash PTY session
- ShellSessionManager Multi-user session pool, isolated by chat_id/open_id
- CommandExecutor     Command execution engine (sentinel completion detection + timeout + streaming output)
- CommandResult       Command execution result dataclass
- SessionInfo         Session state snapshot
- make_session_id     Factory function for generating session isolation IDs
- run_once            One-shot command execution (temporary session)
"""

from utils.ansi import strip_ansi, clean_output
from .executor import CommandExecutor, run_once
from .interactive import detect_interactive_prompt, is_interactive_command
from .session import ShellSession
from .session_manager import ShellSessionManager, make_session_id
from .types import CommandResult, SessionInfo

__all__ = [
    "ShellSession",
    "ShellSessionManager",
    "CommandExecutor",
    "CommandResult",
    "SessionInfo",
    "make_session_id",
    "run_once",
    "strip_ansi",
    "clean_output",
    "detect_interactive_prompt",
    "is_interactive_command",
]
