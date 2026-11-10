"""Child processes of the backup jobs (plan M5 Task 10, R13): `CommandRunner`, the seam the restic wrapper
(and the PostgreSQL client calls of Tasks 24 and 26) run through, and `SubprocessRunner`, the real one.

The child gets exactly the environment it is given, nothing of this process's: a secret reaches it as a file
path or in that environment, never in its argv. Its stdout is returned whole; of its stderr only the last
`STDERR_TAIL_BYTES`, decoded, as `stderr_tail`, which is never logged: a log line names the command (the base
name of `argv[0]`) and its return code only. A child still running at `timeout_s` is stopped (SIGTERM, which
lets restic remove its lock, then SIGKILL after `STOP_GRACE_S`) and `CommandTimeout` is raised; a cancelled
call stops its child the same way.
"""

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePath
from typing import Final, Protocol

from jobhunter.observability.logging import get_logger

STDERR_TAIL_BYTES: Final = 4096
STOP_GRACE_S: Final = 10.0

log = get_logger("jobhunter.backup.runner")


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr_tail: str


class CommandTimeout(Exception):
    """The command named `command` was still running after `timeout_s` and was stopped."""

    def __init__(self, command: str, timeout_s: float) -> None:
        super().__init__(f"{command} still running after {timeout_s:g} s")
        self.command = command
        self.timeout_s = timeout_s


class CommandRunner(Protocol):
    async def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        timeout_s: float,
        stdin: bytes | None = None,
    ) -> CommandResult: ...


def command_name(argv: Sequence[str]) -> str:
    """What a log line may say of a command: the base name of its program."""
    return PurePath(argv[0]).name


async def _stop(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    process.terminate()
    try:
        async with asyncio.timeout(STOP_GRACE_S):
            await process.wait()
    except TimeoutError:
        process.kill()
        await process.wait()


class SubprocessRunner:
    """`CommandRunner` on real child processes (module docstring)."""

    async def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        timeout_s: float,
        stdin: bytes | None = None,
    ) -> CommandResult:
        name = command_name(argv)
        process = await asyncio.create_subprocess_exec(
            *argv,
            env=dict(env),
            stdin=asyncio.subprocess.DEVNULL if stdin is None else asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(timeout_s):
                stdout, stderr = await process.communicate(stdin)
        except TimeoutError:
            await _stop(process)
            log.warning("command_timed_out", command=name, timeout_s=timeout_s)
            raise CommandTimeout(name, timeout_s) from None
        except BaseException:
            await asyncio.shield(_stop(process))
            raise
        returncode = await process.wait()  # it has exited: communicate() returns only then
        log.info("command_finished", command=name, returncode=returncode)
        return CommandResult(
            returncode, stdout, stderr[-STDERR_TAIL_BYTES:].decode("utf-8", errors="replace")
        )
