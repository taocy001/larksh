"""Interactive command / prompt detection

Two categories of detection:
1. Command name detection: before execution, determines whether the command is an interactive program (vim/htop/ssh, etc.)
2. Output content detection: during execution, detects sudo password prompts, [y/N] confirmations, etc., and advises the user to switch to a terminal
"""
from __future__ import annotations

import re
import shlex
from typing import Optional


# ---- Interactive command list -----------------------------------------------

#: Known commands that require a full terminal; WebApp is recommended before execution
INTERACTIVE_CMDS: frozenset[str] = frozenset({
    # Editors
    "vim", "vi", "nvim", "nano", "emacs", "micro", "ne", "joe", "mcedit",
    # Pagers
    "less", "more", "most",
    # System monitors
    "top", "htop", "btop", "glances", "iotop", "iftop", "atop", "nmon",
    # Terminal multiplexers
    "tmux", "screen", "byobu", "zellij",
    # Remote access
    "ssh", "telnet", "mosh", "nc", "ncat",
    # File transfer (interactive mode)
    "ftp", "sftp",
    # REPLs / debuggers
    "python", "python2", "python3", "ipython", "bpython", "ptpython",
    "node", "nodejs", "deno", "irb", "pry", "iex", "ghci", "lua",
    "gdb", "lldb", "pdb", "rdb", "jdb",
    # Database clients
    "mysql", "mysqladmin", "psql", "sqlite3", "redis-cli",
    "mongo", "mongosh", "cqlsh", "clickhouse-client",
    # Miscellaneous
    "man", "info", "dialog", "whiptail", "mc", "ranger", "nnn", "vifm",
    "watch",  # not a full-screen editor but continuously refreshes
})

# ---- Runtime interactive prompt patterns ------------------------------------

_INTERACTIVE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (
        re.compile(r'\[sudo\]\s+password\s+for\s+\w+\s*:', re.I),
        "检测到 sudo 密码提示，请点击「打开终端」按钮在 WebApp 中输入密码",
    ),
    (
        re.compile(r'(?<!\w)Password\s*:', re.I),
        "检测到密码输入提示，请点击「打开终端」按钮在 WebApp 中输入",
    ),
    (
        re.compile(r'\(yes/no(?:/\[fingerprint\])?\)', re.I),
        "SSH 需要确认主机指纹，请点击「打开终端」按钮操作",
    ),
    (
        re.compile(r'\[y(?:es)?/[nN](?:o)?\]', re.I),
        "需要交互确认（y/n），请点击「打开终端」按钮操作",
    ),
    (
        re.compile(r'(?:Enter|Input|Type)\s+(?:passphrase|pin|otp|token)', re.I),
        "检测到认证信息输入提示，请点击「打开终端」按钮操作",
    ),
    (
        re.compile(r'--\s*More\s*--|\(END\)', re.I),
        "分页器已暂停，请点击「打开终端」按钮继续查看",
    ),
    (
        re.compile(r'^\s*>>>\s*$', re.M),
        "进入 Python REPL 交互模式，请点击「打开终端」按钮",
    ),
    (
        re.compile(r'^\s*>\s*$', re.M),
        "进入交互式 Shell/REPL，请点击「打开终端」按钮",
    ),
]


# ---- Public interface -------------------------------------------------------

def is_interactive_command(command: str) -> tuple[bool, str]:
    """Check whether the command belongs to a known interactive program.

    Returns:
        (True, hint_message) — interactive command
        (False, "")          — ordinary command
    """
    try:
        parts = shlex.split(command.strip())
    except ValueError:
        parts = command.strip().split()

    if not parts:
        return False, ""

    # Use the last path component as the command name (handles forms like /usr/bin/vim)
    cmd_name = parts[0].rsplit("/", 1)[-1]

    if cmd_name in INTERACTIVE_CMDS:
        return True, (
            f"命令 `{cmd_name}` 是交互式程序，无法在消息卡片中运行。\n"
            "请点击下方「打开终端」按钮，在 WebApp 终端中执行。"
        )
    return False, ""


def detect_interactive_prompt(output: str) -> Optional[str]:
    """Detect whether any interactive prompt appears in the command output.

    Returns:
        Non-empty hint string — an interactive prompt was detected
        None                  — no interactive prompt found
    """
    for pattern, message in _INTERACTIVE_PATTERNS:
        if pattern.search(output):
            return message
    return None
