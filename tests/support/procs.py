"""Tracked child processes for the crash tests, and polling helpers.

`ProcessGroup.spawn` starts a child in its own process group with both output streams on pipes. A daemon
reader thread per stream collects every line; stdout lines that parse as a JSON object are the child's log
events, which `Proc.wait_event` waits for. The `procs` fixture (`tests/support/fixtures.py`) closes the group
after the test: every child's process group gets `SIGCONT` (a stopped process must not survive the test) and
then `SIGKILL`, the child is waited for, and its pipes are closed once their readers have seen end of file.
"""

import asyncio
import json
import os
import signal as signals
import subprocess
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import IO, Any, Final

_REPO_ROOT: Final = Path(__file__).resolve().parents[2]
_TAIL_LINES: Final = 20
_KILL_WAIT_S: Final = 30.0
_READER_JOIN_S: Final = 5.0


class Proc:
    """One child process: its log events, raw output, state and exit."""

    def __init__(self, popen: subprocess.Popen[str]) -> None:
        self._popen = popen
        self.pid: int = popen.pid
        # `spawn` starts the child with `start_new_session`, so it leads a process group with its own pid. Its
        # descendants inherit the group, and with it the pipes.
        self.pgid: int = popen.pid
        self._cond = threading.Condition()
        self._lines: list[str] = []  # both streams, in arrival order
        self._events: list[dict[str, Any]] = []  # the JSON objects among the stdout lines
        self._open_streams = 0
        self._readers: list[threading.Thread] = []
        for name, stream, parse in (("stdout", popen.stdout, True), ("stderr", popen.stderr, False)):
            if stream is None:
                continue
            self._open_streams += 1
            reader = threading.Thread(
                target=self._read, args=(stream, parse), name=f"proc-{self.pid}-{name}", daemon=True
            )
            self._readers.append(reader)
            reader.start()

    def _read(self, stream: IO[str], parse: bool) -> None:
        try:
            for line in stream:
                event = _json_object(line) if parse else None
                with self._cond:
                    self._lines.append(line)
                    if event is not None:
                        self._events.append(event)
                    self._cond.notify_all()
        except OSError:
            pass  # a failed read ends the stream as end of file does
        finally:
            with self._cond:
                self._open_streams -= 1
                self._cond.notify_all()

    def wait_event(self, event: str, timeout_s: float, **match: object) -> dict[str, Any]:
        """The first log event named `event` whose fields equal `match`, waiting up to `timeout_s` for it.

        Raises `TimeoutError`, with the last output lines in its message, when no such event arrived in time
        or the child closed its output first.
        """
        deadline = time.monotonic() + timeout_s
        with self._cond:
            while True:
                for candidate in self._events:
                    if candidate.get("event") == event and _matches(candidate, match):
                        return candidate
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._open_streams == 0:
                    break
                self._cond.wait(remaining)
            reason = "its output ended" if self._open_streams == 0 else f"{timeout_s} s passed"
        raise TimeoutError(
            f"pid {self.pid}: no {event!r} event matching {match!r} before {reason} "
            f"(exit code {self._popen.poll()}); last output lines:\n{self._tail()}"
        )

    def is_stopped(self) -> bool:
        """Whether the child is stopped (state `T` in `/proc/<pid>/stat`), e.g. after `SIGSTOP`."""
        try:
            stat = Path(f"/proc/{self.pid}/stat").read_text(encoding="ascii", errors="replace")
        except FileNotFoundError:
            return False
        # The command name in parentheses may contain spaces; the state is the first field after it.
        return stat.rpartition(")")[2].split()[0] == "T"

    def signal(self, sig: int) -> None:
        """Send `sig` to the child (nothing happens when it has already been reaped)."""
        self._popen.send_signal(sig)

    def exit_within(self, timeout_s: float) -> int | None:
        """The exit code (negative for a signal), or `None` if the child is still alive after `timeout_s`."""
        try:
            return self._popen.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return None

    def output(self) -> str:
        """Everything the child wrote to stdout and stderr so far, in arrival order."""
        with self._cond:
            return "".join(self._lines)

    def _tail(self) -> str:
        return "".join(self._lines[-_TAIL_LINES:])

    def _kill(self) -> int | None:
        """`SIGCONT`, then `SIGKILL`, to the child's whole process group; then wait for the child.

        The group is signalled even when the child itself was already reaped: a descendant that survived it
        would keep the pipes open, and their readers would never end. Linux gives no new process a pid that
        is still a live group's id, so the saved id names this group for as long as any member is alive.
        Returns the exit code, or `None` if the child did not end within `_KILL_WAIT_S`.
        """
        for sig in (signals.SIGCONT, signals.SIGKILL):
            try:
                os.killpg(self.pgid, sig)
            except ProcessLookupError:
                break  # no member of the group is left
        try:
            return self._popen.wait(timeout=_KILL_WAIT_S)
        except subprocess.TimeoutExpired:
            return None

    def _close(self) -> bool:
        """Let the readers drain what is left, then close the pipes once every reader saw end of file.

        Returns `False`, leaving the pipes open, when a reader is still blocked: closing a pipe under a
        blocked reader waits for that read to return.
        """
        for reader in self._readers:
            reader.join(_READER_JOIN_S)
        if any(reader.is_alive() for reader in self._readers):
            return False
        for stream in (self._popen.stdout, self._popen.stderr):
            if stream is not None:
                stream.close()
        return True


def _json_object(line: str) -> dict[str, Any] | None:
    try:
        value = json.loads(line)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _matches(event: Mapping[str, Any], match: Mapping[str, object]) -> bool:
    return all(key in event and event[key] == value for key, value in match.items())


class ProcessGroup:
    """The children of one test; `close()` (the `procs` fixture's finalizer) ends and reaps all of them."""

    def __init__(self) -> None:
        self._procs: list[Proc] = []

    def spawn(self, args: list[str], env: Mapping[str, str]) -> Proc:
        """Start `args` from the repository root with exactly `env` (plus unbuffered Python output)."""
        popen = subprocess.Popen(
            args,
            env={"PYTHONUNBUFFERED": "1", **env},
            cwd=_REPO_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,  # its own process group, so `close` reaches its descendants too
        )
        proc = Proc(popen)
        self._procs.append(proc)
        return proc

    def close(self) -> None:
        """Kill (`SIGCONT`, then `SIGKILL`) every child's process group, reap the children, close all pipes.

        Raises `RuntimeError` after doing all of that if a child could not be reaped or a pipe stayed open.
        """
        survivors = [proc.pid for proc in self._procs if proc._kill() is None]
        held_open = [proc.pid for proc in self._procs if not proc._close()]
        if survivors or held_open:
            raise RuntimeError(f"children alive after SIGKILL: {survivors}; pipes still open: {held_open}")


def wait_until(pred: Callable[[], bool], timeout_s: float, step_s: float = 0.2) -> None:
    """Poll `pred` every `step_s` until it is true; `TimeoutError` if it is still false after `timeout_s`."""
    deadline = time.monotonic() + timeout_s
    while not pred():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"condition {pred!r} still false after {timeout_s} s")
        time.sleep(min(step_s, remaining))


async def await_until(pred: Callable[[], Awaitable[bool]], timeout_s: float, step_s: float = 0.2) -> None:
    """`wait_until` for an async predicate, sleeping on the event loop between polls."""
    deadline = time.monotonic() + timeout_s
    while not await pred():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"condition {pred!r} still false after {timeout_s} s")
        await asyncio.sleep(min(step_s, remaining))
