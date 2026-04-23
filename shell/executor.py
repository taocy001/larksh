"""CommandExecutor — Shell command execution engine

Core mechanisms
---------------
1. **Sentinel-based command completion detection**
   - A unique sentinel UUID is generated for each command execution
   - Writes to the PTY: ``{command}\n`` followed by ``printf '\\n__LARKSH_DONE_{uuid}__:%d\\n' $?\n``
   - Because stty -echo was disabled during session initialization, the PTY will not echo our writes
   - OutputCollector continuously collects output and fires the done event when the sentinel line is detected
   - Sentinel line format: ``__LARKSH_DONE_{uuid}__:{exit_code}``

2. **Streaming output** (optional)
   - Callers may supply a stream_callback
   - Whenever new complete lines accumulate, the callback is awaited immediately
   - An additional time-based batch flush (default 100ms) ensures data is pushed even if the command produces no newlines for a long time

3. **Timeout control**
   - On timeout, sends SIGINT (Ctrl-C) first, then waits up to 2 seconds for the sentinel to arrive
   - If still not done, records timed_out=True and returns the collected output so far

4. **Interactive command detection**
   - Pre-execution: checks the command name (vim/htop/ssh, etc.)
   - During execution: scans output for sudo password prompts, y/n confirmations, etc.

5. **ANSI stripping**
   - OutputCollector calls strip_ansi() on each line, returning plain text

6. **Audit logging**
   - After each command completes, writes an entry to AuditLogger (from the security module)
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Awaitable, Callable, Optional

from utils.ansi import strip_ansi, clean_output
from .interactive import detect_interactive_prompt, is_interactive_command
from .session import ShellSession
from .types import CommandResult

logger = logging.getLogger(__name__)

# Sentinel template
_SENTINEL_PREFIX = "__LARKSH_DONE_"
_SENTINEL_SUFFIX = "__"

# Sentinel print command written to the PTY (printf avoids relying on echo built-in behavior)
# %d receives $?; sentinel line format: \n__LARKSH_DONE_{uuid}__:{exit_code}
_SENTINEL_CMD_TMPL = "printf '\\n{sentinel}:%d\\n' $?\n"

StreamCallback = Callable[[str], Awaitable[None]]


# ---------------------------------------------------------------------------
# Internal: output collector
# ---------------------------------------------------------------------------

class _OutputCollector:
    """Accumulates PTY output, processes it line by line, and detects the sentinel line.

    Thread safety: all methods are called within the same asyncio event loop; no additional locks needed.
    """

    def __init__(
        self,
        sentinel: str,
        stream_callback: Optional[StreamCallback] = None,
        flush_interval: float = 0.1,
    ) -> None:
        self._sentinel = sentinel
        self._stream_cb = stream_callback
        self._flush_interval = flush_interval

        self._raw_buf = ""            # unprocessed raw data (may span chunk boundaries)
        self._output_lines: list[str] = []   # cleaned output lines
        self._pending_stream: list[str] = [] # lines queued for delivery to stream_callback

        self._exit_code: Optional[int] = None
        self.done = asyncio.Event()

        # Streaming flush task (only started when stream_callback is provided)
        self._flush_task: Optional[asyncio.Task] = None
        if stream_callback:
            self._flush_task = asyncio.get_running_loop().create_task(
                self._flush_loop(), name="output-flush"
            )

    # ------------------------------------------------------------------
    # Data ingestion (called by ShellSession callbacks)
    # ------------------------------------------------------------------

    async def feed(self, raw: str) -> None:
        """Receive a raw data chunk from the PTY and process complete lines."""
        if self.done.is_set():
            return  # sentinel already received; ignore subsequent data

        self._raw_buf += raw

        # Process line by line
        while "\n" in self._raw_buf:
            line, self._raw_buf = self._raw_buf.split("\n", 1)
            line_text = line.rstrip("\r")
            await self._process_line(line_text)

            if self.done.is_set():
                return

    async def _process_line(self, line: str) -> None:
        """Process a single line: detect sentinel → strip ANSI → dispatch to stream."""
        # Sentinel detection
        if self._sentinel in line:
            colon = line.rfind(":")
            if colon != -1:
                try:
                    self._exit_code = int(line[colon + 1:])
                except ValueError:
                    self._exit_code = 0
            else:
                self._exit_code = 0
            self.done.set()
            # Cancel the flush task
            if self._flush_task and not self._flush_task.done():
                self._flush_task.cancel()
            return

        # Strip ANSI escape codes
        clean = strip_ansi(line)

        # Append to output list
        self._output_lines.append(clean)

        # If a streaming callback is set, enqueue for delivery
        if self._stream_cb is not None:
            self._pending_stream.append(clean + "\n")

    # ------------------------------------------------------------------
    # Streaming flush loop
    # ------------------------------------------------------------------

    async def _flush_loop(self) -> None:
        """Push accumulated lines to stream_callback every flush_interval seconds."""
        assert self._stream_cb is not None
        try:
            while True:
                await asyncio.sleep(self._flush_interval)
                await self._flush_pending()
        except asyncio.CancelledError:
            # Do one final flush when the task is cancelled
            await self._flush_pending()

    async def _flush_pending(self) -> None:
        if not self._pending_stream or self._stream_cb is None:
            return
        chunk = "".join(self._pending_stream)
        self._pending_stream.clear()
        try:
            await self._stream_cb(chunk)
        except Exception:
            logger.exception("stream_callback raised")

    async def flush_remaining(self) -> None:
        """Manually flush remaining content after the command completes."""
        await self._flush_pending()

    # ------------------------------------------------------------------
    # Result extraction
    # ------------------------------------------------------------------

    def get_output(self) -> str:
        return "\n".join(self._output_lines)

    @property
    def exit_code(self) -> int:
        return self._exit_code if self._exit_code is not None else -1


# ---------------------------------------------------------------------------
# Public: command executor
# ---------------------------------------------------------------------------

class CommandExecutor:
    """Execute a single shell command and return a CommandResult.

    Usage::

        executor = CommandExecutor(config, audit_logger=guard._audit)
        result = await executor.execute(session, "ls -la", timeout=30.0)
        print(result.output)
    """

    def __init__(self, config, audit_logger=None) -> None:
        out_cfg = config.get("output", {})
        self._max_output_chars: int = int(out_cfg.get("max_output_chars", 8000))
        self._push_interval: float = float(out_cfg.get("push_interval_ms", 500)) / 1000
        self._default_timeout: float = 60.0
        self._audit = audit_logger

    # ------------------------------------------------------------------
    # Core execution method
    # ------------------------------------------------------------------

    async def execute(
        self,
        session: ShellSession,
        command: str,
        *,
        timeout: Optional[float] = None,
        stream_callback: Optional[StreamCallback] = None,
        open_id: str = "",
        skip_interactive_check: bool = False,
    ) -> CommandResult:
        """Execute a command and return a CommandResult.

        Parameters
        ----------
        session:
            Target ShellSession (must already be initialized)
        command:
            Shell command to execute (may be multi-line)
        timeout:
            Timeout in seconds; None uses the default (60s)
        stream_callback:
            Streaming output callback, called once per push_interval seconds with new text
        open_id:
            Feishu open_id of the executor (used for audit logging)
        skip_interactive_check:
            Skip the pre-execution interactive command name check (not needed in WebApp mode)
        """
        timeout = timeout if timeout is not None else self._default_timeout
        command = command.strip()

        if not command:
            return CommandResult(
                command=command,
                output="",
                exit_code=0,
                timed_out=False,
                session_id=session.session_id,
            )

        # ---- Pre-execution check: is this a known interactive command? ----
        if not skip_interactive_check:
            is_interactive, hint = is_interactive_command(command)
            if is_interactive:
                return CommandResult(
                    command=command,
                    output="",
                    exit_code=126,
                    timed_out=False,
                    session_id=session.session_id,
                    interactive_hint=hint,
                )

        # ---- Generate sentinel ----
        sentinel_id = uuid.uuid4().hex
        sentinel = f"{_SENTINEL_PREFIX}{sentinel_id}{_SENTINEL_SUFFIX}"
        sentinel_cmd = _SENTINEL_CMD_TMPL.format(sentinel=sentinel)

        # ---- Create output collector ----
        collector = _OutputCollector(
            sentinel=sentinel,
            stream_callback=stream_callback,
            flush_interval=self._push_interval,
        )
        session.subscribe(collector.feed)

        start_ts = time.time()
        timed_out = False

        try:
            # ---- Write command + sentinel print instruction to the PTY ----
            await session.async_write(f"{command}\n{sentinel_cmd}")
            logger.debug(
                "Session %s: wrote command %r (sentinel=%s)",
                session.session_id, command[:80], sentinel_id[:8],
            )

            # ---- Wait for sentinel or timeout ----
            try:
                await asyncio.wait_for(
                    asyncio.shield(collector.done.wait()),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                timed_out = True
                logger.warning(
                    "Session %s: command timed out after %.1fs: %r",
                    session.session_id, timeout, command[:80],
                )
                # Send Ctrl-C to interrupt the command; give the sentinel one last chance to arrive
                await session.async_send_ctrl_c()
                try:
                    await asyncio.wait_for(collector.done.wait(), timeout=2.0)
                    timed_out = True  # keep the timed-out flag
                except asyncio.TimeoutError:
                    pass  # sentinel never arrived; return whatever was collected

        finally:
            session.unsubscribe(collector.feed)

        # ---- Flush remaining streaming data ----
        await collector.flush_remaining()

        duration_ms = (time.time() - start_ts) * 1000

        # ---- Post-process output ----
        raw_output = collector.get_output()
        output = clean_output(raw_output, max_chars=self._max_output_chars)

        # ---- Runtime interactive prompt detection ----
        interactive_hint: Optional[str] = None
        if not timed_out:
            interactive_hint = detect_interactive_prompt(raw_output)

        result = CommandResult(
            command=command,
            output=output,
            exit_code=collector.exit_code,
            timed_out=timed_out,
            duration_ms=round(duration_ms, 1),
            session_id=session.session_id,
            interactive_hint=interactive_hint,
        )

        # ---- Audit log ----
        self._write_audit(open_id, result)

        logger.info(
            "Session %s: cmd done exit=%d timed_out=%s dur=%.0fms: %r",
            session.session_id,
            result.exit_code,
            timed_out,
            duration_ms,
            command[:80],
        )
        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _write_audit(self, open_id: str, result: CommandResult) -> None:
        if self._audit is None:
            return
        try:
            self._audit.write(
                {
                    "open_id": open_id,
                    "session_id": result.session_id,
                    "command": result.command,
                    "exit_code": result.exit_code,
                    "timed_out": result.timed_out,
                    "duration_ms": result.duration_ms,
                    "interactive_hint": result.interactive_hint,
                }
            )
        except Exception:
            logger.exception("Failed to write audit log")


# ---------------------------------------------------------------------------
# Convenience function: one-shot execution (creates a temporary session, discarded after use)
# ---------------------------------------------------------------------------

async def run_once(
    command: str,
    config,
    timeout: float = 30.0,
) -> CommandResult:
    """Create a temporary bash session, execute the command, then immediately destroy it.

    Suitable for single-use scenarios where no persistent session is needed.
    """
    from .session import ShellSession

    session = ShellSession("_oneshot_", config)
    loop = asyncio.get_running_loop()
    session.start_reading(loop)
    await session.initialize()

    executor = CommandExecutor(config)
    try:
        return await executor.execute(session, command, timeout=timeout)
    finally:
        session.terminate()
