"""Test-only crash-injection hooks (spec §16.8, §18.3).

``JOBHUNTER_FAULT`` has the form ``<point>[:pause|:sleep=<seconds>]``. A hook acts only when
``JOBHUNTER_ENV=test`` and its point name matches; startup assertion ``fake_mode_in_prod``
refuses to start a prod process that has the variable set at all.

* ``<point>``: the process dies at once with exit code 86 (``os._exit``, no cleanup runs).
* ``<point>:pause``: the process stops itself with SIGSTOP until something sends SIGCONT.
* ``<point>:sleep=<s>``: ``afault_point`` awaits ``asyncio.sleep(s)``. The synchronous
  ``fault_point`` never sleeps, because that would block an event loop.

Before it acts, ``afault_point`` logs ``fault_point_reached`` (warning) with ``point`` and ``action``: the
crash tests' readiness acknowledgement that the process is at the point. The handler flushes every line, so
the line is out before a pause or an exit.

A malformed action on a matching point raises ``ValueError`` so a typo cannot pass as a no-op.
"""

import asyncio
import math
import os
import signal
from typing import Final, Literal

from jobhunter.observability.logging import get_logger

FAULT_EXIT_CODE: Final = 86

_ENV_VAR: Final = "JOBHUNTER_FAULT"

type _Action = tuple[Literal["exit", "pause", "sleep"], float]

log = get_logger("jobhunter.faults")


def _armed_action(name: str) -> _Action | None:
    """Return the action armed for point `name`, or None when nothing fires here."""
    if os.environ.get("JOBHUNTER_ENV") != "test":
        return None
    point, _, action = os.environ.get(_ENV_VAR, "").partition(":")
    if not point or point != name:
        return None
    if action == "":
        return ("exit", 0.0)
    if action == "pause":
        return ("pause", 0.0)
    if action.startswith("sleep="):
        try:
            seconds = float(action.removeprefix("sleep="))
        except ValueError:
            seconds = math.nan
        if math.isfinite(seconds) and seconds >= 0:
            return ("sleep", seconds)
    raise ValueError(f"invalid {_ENV_VAR} action {action!r}: expected '', 'pause' or 'sleep=<seconds>'")


def _stop_the_process(kind: Literal["exit", "pause"]) -> None:
    if kind == "pause":
        os.kill(os.getpid(), signal.SIGSTOP)
    else:
        os._exit(FAULT_EXIT_CODE)


def fault_point(name: str) -> None:
    """Crash or pause here when `JOBHUNTER_FAULT` names this point and the env is `test`."""
    action = _armed_action(name)
    if action is None:
        return
    kind, _ = action
    if kind != "sleep":
        _stop_the_process(kind)


async def afault_point(name: str) -> None:
    """Like `fault_point`, and additionally supports `sleep=<s>` through `asyncio.sleep`."""
    action = _armed_action(name)
    if action is None:
        return
    kind, seconds = action
    log.warning("fault_point_reached", point=name, action=kind)
    if kind == "sleep":
        await asyncio.sleep(seconds)
    else:
        _stop_the_process(kind)
