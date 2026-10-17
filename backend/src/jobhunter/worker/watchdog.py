"""The worker watchdog (spec §3.3.1 step 4): heartbeat on the lock session, exit 70 when ownership is lost.

Every `period_s` the watchdog thread runs the §3.3.1 heartbeat on the lock session. When the statement fails
or times out, updates no row, or reports that this session does not hold the lock, the worker is no longer the
singleton and the thread ends the process at once with `exit(70)` (`os._exit`: no cleanup that could still
act as the old owner). Compose restarts the worker, and the new process takes over through the takeover sweep.

`stop()` wins every race with a failing heartbeat: the decision to exit is taken under `_decision`, after the
heartbeat returned, and `stop()` sets the stop event under the same lock. Once `stop()` has returned the
watchdog never calls `exit`; if the thread was already exiting, `stop()` waits for that and the process ends.
"""

import os
import threading
from collections.abc import Callable
from typing import NoReturn
from uuid import UUID

from jobhunter.observability.logging import get_logger
from jobhunter.worker.lock import HEARTBEAT_PERIOD_S, WATCHDOG_EXIT_CODE, LockSession

log = get_logger("jobhunter.worker.watchdog")


class Watchdog(threading.Thread):
    """A daemon thread that keeps the heartbeat of `(instance_id, epoch)`; it exits the process on failure."""

    def __init__(
        self,
        session: LockSession,
        instance_id: UUID,
        epoch: int,
        *,
        period_s: float = HEARTBEAT_PERIOD_S,
        exit: Callable[[int], NoReturn] = os._exit,
    ) -> None:
        super().__init__(name="jobhunter-watchdog", daemon=True)
        self.session = session
        self.instance_id = instance_id
        self.epoch = epoch
        self.period_s = period_s
        self._exit = exit
        self._stop = threading.Event()
        # Held while the thread decides to exit and exits, and while `stop()` sets `_stop`.
        self._decision = threading.Lock()

    def run(self) -> None:
        while not self._stop.wait(self.period_s):
            error_class: str | None = None
            try:
                held = self.session.heartbeat(self.instance_id, self.epoch)
            except Exception as exc:
                # The outcome this catch persists is the process exit below (§17.1): a worker that cannot
                # prove it holds the lock must stop at once, and the replacement worker's takeover sweep
                # records the state transitions of the work this process owned.
                held, error_class = False, type(exc).__name__
            if held:
                continue
            with self._decision:
                if self._stop.is_set():
                    return  # stop() arrived while the heartbeat was in flight: it wins, no exit
                try:
                    if error_class is None:
                        log.critical("watchdog_lock_lost", epoch=self.epoch, exit_code=WATCHDOG_EXIT_CODE)
                    else:
                        log.critical(
                            "watchdog_heartbeat_failed",
                            error_class=error_class,
                            epoch=self.epoch,
                            exit_code=WATCHDOG_EXIT_CODE,
                        )
                finally:
                    self._exit(WATCHDOG_EXIT_CODE)  # even if the log line could not be written
            return  # reached only when an injected `exit` returns

    def stop(self, timeout_s: float = 15.0) -> None:
        """Stop the watchdog and wait up to `timeout_s` for its thread; it never calls `exit` afterwards.

        It can wait longer than `timeout_s`: when the thread has already decided to exit, `stop()` blocks on
        that decision until `exit` ends the process, because "no exit after `stop()` returns" wins over the
        timeout.
        """
        with self._decision:
            self._stop.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout_s)
