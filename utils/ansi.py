"""ANSI/VT100 escape sequence stripping and output truncation utilities"""
from __future__ import annotations

import re

# ---- Regex patterns ---------------------------------------------------------------

# CSI:  ESC [ [parameter bytes]* [intermediate bytes]* final byte
_CSI = r'\x1b\[[\x20-\x2f]*[\x30-\x3f]*[\x40-\x7e]'

# OSC:  ESC ] ... BEL  or  ESC ] ... ESC \\
_OSC = r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)'

# Character set switching: ESC ( X  ESC ) X  ESC * X  ESC + X
_CHARSET = r'\x1b[()][AB012]'

# Single-character ESC sequences (SS2 SS3 DCS PM APC ST RI NEL HTS VT/FF DECsave/restore ...)
_SINGLE = r'\x1b[=><NOPQRST78cDE12346789]'

# DEC special: ESC # digit
_DEC = r'\x1b#[0-9]'

_ANSI_RE = re.compile(
    r'(?:' + r'|'.join([_CSI, _OSC, _CHARSET, _SINGLE, _DEC]) + r')',
)

# Bare \r not followed by \n (carriage return to line start in PTY, overwrites printed content)
_BARE_CR_RE = re.compile(r'\r(?!\n)')

# Backspace character sequences: character + \x08 in succession, used for "delete" effect
_BACKSPACE_RE = re.compile(r'.\x08')


# ---- Public interface ----------------------------------------------------------------

def strip_ansi(text: str) -> str:
    """Strip all ANSI/VT100 escape sequences, preserving readable text.
    Processing order: backspace → ANSI → bare CR
    """
    while _BACKSPACE_RE.search(text):
        text = _BACKSPACE_RE.sub('', text)
    text = _ANSI_RE.sub('', text)
    text = _BARE_CR_RE.sub('', text)
    return text


def clean_output(text: str, max_chars: int = 8000) -> str:
    """Full cleaning pipeline: strip ANSI → normalize line endings → truncate oversized output"""
    text = strip_ansi(text)
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    if len(text) > max_chars:
        text = f"...(输出过长，已截断前段)...\n{text[-max_chars:]}"
    return text


def truncate_output(text: str, max_chars: int = 8000, max_lines: int = 100) -> tuple[str, bool]:
    """Truncate oversized output, retaining trailing content. Returns (truncated text, whether truncation occurred)."""
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    lines = text.splitlines(keepends=True)
    truncated = False

    if len(lines) > max_lines:
        dropped = len(lines) - max_lines
        lines = lines[-max_lines:]
        lines.insert(0, f"... [省略了 {dropped} 行] ...\n")
        truncated = True

    result = "".join(lines)
    if len(result) > max_chars:
        result = result[-max_chars:]
        result = "... [输出过长，只显示末尾部分] ...\n" + result
        truncated = True

    return result, truncated
