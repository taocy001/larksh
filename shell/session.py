"""ShellSession — Wraps a single persistent bash PTY process

Design notes
------------
1. Uses ptyprocess.PtyProcess to hold the master fd; bash runs on the slave side
2. Background asyncio read loop: blocks reading the PTY in a thread pool, then awaits all subscribed callbacks with received data
3. Startup initialization (initialize()):
   - Disables PTY echo (stty -echo) to prevent input echo from polluting output
   - Clears PS1/PS2 to prevent prompt strings from polluting output
   - Disables HISTFILE to avoid writing to .bash_history
   - Waits for the READY sentinel to confirm bash is ready
4. write() is synchronous (writes directly to fd, very fast); async_write() provides a thread-safe async version
5. All concurrent writes are serialized through _write_lock
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Awaitable, Callable, Optional

import ptyprocess

logger = logging.getLogger(__name__)

OutputCallback = Callable[[str], Awaitable[None]]

# Startup ready sentinel — ensures bash has finished initializing before accepting commands
_STARTUP_SENTINEL = "__LARKSH_READY__"

# Initialization script injected at bash startup:
# - stty -echo: disables terminal echo so commands we write don't appear in the output stream
# - PS1/PS2/PS3/PS4: clears all prompt strings
# - HISTFILE: disables history persistence to disk
_INIT_SCRIPT = (
    "stty -echo 2>/dev/null; "
    "export PS1='' PS2='' PS3='' PS4=''; "
    "unset HISTFILE; export HISTSIZE=0 HISTFILESIZE=0; "
    f'echo "{_STARTUP_SENTINEL}"\n'
)


class ShellSession:
    """Wraps a single persistent bash PTY process."""

    def __init__(self, session_id: str, config) -> None:
        self.session_id = session_id
        self.created_at = time.time()
        self.last_active = time.time()

        shell_cfg = config.get("shell", {})
        bash_path: str = shell_cfg.get("bash_path", "/bin/bash")
        cols: int = int(shell_cfg.get("pty_cols", 220))
        rows: int = int(shell_cfg.get("pty_rows", 40))
        extra_env: dict = dict(shell_cfg.get("env", {}) or {})

        env = {
            "HOME": os.environ.get("HOME", "/root"),
            "PATH": os.environ.get(
                "PATH",
                "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            ),
            "USER": os.environ.get("USER", "root"),
            "SHELL": bash_path,
            **extra_env,
            "_FEISHU_SESSION": session_id,
        }

        start_dir: str = shell_cfg.get("start_dir", os.environ.get("HOME", "/root"))

        self._proc = ptyprocess.PtyProcess.spawn(
            [bash_path, "--norc", "--noprofile"],
            env=env,
            dimensions=(rows, cols),
            cwd=start_dir,
        )

        self._callbacks: list[OutputCallback] = []
        self._read_task: Optional[asyncio.Task] = None
        self._write_lock: Optional[asyncio.Lock] = None  # deferred until inside the asyncio loop

        # Initialization state: initialize() must be called before the first execute()
        self._initialized = False
        self._init_event: Optional[asyncio.Event] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start_reading(self, loop: asyncio.AbstractEventLoop) -> None:
        """Start the background output-reading task (synchronous, returns immediately)."""
        self._write_lock = asyncio.Lock()
        self._init_event = asyncio.Event()
        self._read_task = loop.create_task(
            self._read_loop(), name=f"pty-read-{self.session_id}"
        )

    async def initialize(self, timeout: float = 10.0) -> None:
        """Send the initialization script to bash and wait for the READY sentinel (idempotent).

        Must be awaited once after start_reading() and before the first user command.
        """
        if self._initialized:
            return

        assert self._init_event is not None, "call start_reading() first"

        ready_event = asyncio.Event()

        async def _watch_ready(data: str) -> None:
            if _STARTUP_SENTINEL in data:
                ready_event.set()

        self._callbacks.append(_watch_ready)
        try:
            self.write(_INIT_SCRIPT)
            try:
                await asyncio.wait_for(ready_event.wait(), timeout=timeout)
                logger.debug("Session %s: bash ready", self.session_id)
            except asyncio.TimeoutError:
                logger.warning(
                    "Session %s: init timeout (%.1fs), proceeding anyway",
                    self.session_id,
                    timeout,
                )
        finally:
            self._callbacks.remove(_watch_ready)
            self._initialized = True
            self._init_event.set()

    async def wait_ready(self) -> None:
        """Wait for initialization to complete (for external callers)."""
        if self._initialized:
            return
        assert self._init_event is not None
        await self._init_event.wait()

    def is_alive(self) -> bool:
        try:
            return self._proc.isalive()
        except Exception:
            return False

    def terminate(self) -> None:
        try:
            if self._read_task and not self._read_task.done():
                self._read_task.cancel()
            self._proc.terminate(force=True)
        except Exception:
            pass
        logger.info("Session %s: terminated", self.session_id)

    # ------------------------------------------------------------------
    # Background read loop
    # ------------------------------------------------------------------

    async def _read_loop(self) -> None:
        """Continuously read data from the PTY master fd and dispatch to all callbacks."""
        loop = asyncio.get_running_loop()
        while self._proc.isalive():
            try:
                data: bytes = await loop.run_in_executor(
                    None, self._proc.read, 4096
                )
                if not data:
                    continue
                text = data.decode("utf-8", errors="replace")
                for cb in list(self._callbacks):
                    try:
                        await cb(text)
                    except Exception:
                        logger.exception(
                            "Session %s: output callback error", self.session_id
                        )
            except EOFError:
                logger.info("Session %s: PTY EOF", self.session_id)
                break
            except Exception:
                logger.exception("Session %s: PTY read error", self.session_id)
                break
        logger.info("Session %s: read loop ended", self.session_id)

    # ------------------------------------------------------------------
    # Input interface
    # ------------------------------------------------------------------

    def write(self, text: str) -> None:
        """Synchronously write to bash stdin (writes directly to fd, very fast, will not noticeably block the event loop)."""
        self.last_active = time.time()
        self._proc.write(text.encode("utf-8"))

    async def async_write(self, text: str) -> None:
        """Asynchronous write, serialized via a lock for concurrent safety (used by executor)."""
        assert self._write_lock is not None, "call start_reading() first"
        async with self._write_lock:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, self._proc.write, text.encode("utf-8")
            )
        self.last_active = time.time()

    def send_ctrl_c(self) -> None:
        """Send SIGINT (interrupt the current command)."""
        try:
            self._proc.sendintr()
        except Exception:
            pass

    async def async_send_ctrl_c(self) -> None:
        """Async version of SIGINT, thread-safe."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._proc.sendintr)

    def send_ctrl_z(self) -> None:
        self._proc.sendcontrol("z")

    def send_eof(self) -> None:
        """Send Ctrl-D (EOF)."""
        self._proc.write(b"\x04")

    def resize(self, rows: int, cols: int) -> None:
        """Resize the PTY terminal (called on xterm.js resize events)."""
        if self.is_alive():
            self._proc.setwinsize(rows, cols)

    # ------------------------------------------------------------------
    # Output subscriptions
    # ------------------------------------------------------------------

    def subscribe(self, callback: OutputCallback) -> None:
        if callback not in self._callbacks:
            self._callbacks.append(callback)

    def unsubscribe(self, callback: OutputCallback) -> None:
        try:
            self._callbacks.remove(callback)
        except ValueError:
            pass

    # ------------------------------------------------------------------
    # State queries
    # ------------------------------------------------------------------

    @property
    def pid(self) -> int:
        return self._proc.pid

    def is_shell_waiting(self) -> bool:
        """Check via /proc/<pid>/wchan whether bash is idle (waiting for user input).

        Used as a supplementary signal to determine whether a command has finished executing
        (complements the sentinel mechanism).
        """
        try:
            wchan = open(f"/proc/{self.pid}/wchan").read().strip()
            # Kernel wait channels observed when bash is waiting for input
            return wchan in (
                "n_tty_read",
                "wait4",
                "poll_schedule_timeout",
                "do_wait",
                "hrtime",
                "ep_poll",
            )
        except Exception:
            return False

    def __repr__(self) -> str:
        return (
            f"<ShellSession id={self.session_id!r} "
            f"pid={self.pid} alive={self.is_alive()}>"
        )
