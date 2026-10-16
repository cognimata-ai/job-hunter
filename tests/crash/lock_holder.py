"""A worker reduced to its singleton start-up, for the crash tests (`python -m tests.crash.lock_holder`).

It starts like the worker (`startup_or_exit("worker")`: startup assertions, JSON logs on stdout), opens the
lock session, waits for the lock (logging `standby`), claims the next epoch, starts the watchdog and logs
`holder_ready` with `instance_id`, `epoch` and `lock_pid`. Then it does nothing until the watchdog ends the
process (exit 70) or the test kills it.
"""

import asyncio
import os
import socket
import uuid

from jobhunter import __version__
from jobhunter.config import AppConfig, startup_or_exit
from jobhunter.db.session import make_engine
from jobhunter.observability.logging import get_logger
from jobhunter.worker.epoch import WorkerIdentity, claim_epoch_and_sweep
from jobhunter.worker.lock import LockSession, acquire_lock
from jobhunter.worker.watchdog import Watchdog

log = get_logger("tests.crash.lock_holder")


async def _claim(cfg: AppConfig, ident: WorkerIdentity) -> int:
    engine = make_engine(cfg, pool_size=1)
    try:
        epoch, _ = await claim_epoch_and_sweep(engine, ident)
    finally:
        await engine.dispose()
    return epoch


def main() -> int:
    cfg = startup_or_exit("worker")
    session = LockSession(cfg.dsn("psycopg"))
    lock_pid = session.connect()
    acquire_lock(session)
    ident = WorkerIdentity(uuid.uuid4(), socket.gethostname(), os.getpid(), lock_pid, __version__)
    epoch = asyncio.run(_claim(cfg, ident))
    watchdog = Watchdog(session, ident.instance_id, epoch)
    watchdog.start()
    log.info("holder_ready", instance_id=str(ident.instance_id), epoch=epoch, lock_pid=lock_pid)
    watchdog.join()  # the watchdog only ends by ending the process
    log.error("holder_watchdog_ended")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
