"""The api's watch of the worker heartbeat (spec §17.6 L5642, §16.5 L5442; plan M5 Task 3, O-ops O11, Q9).

`worker.heartbeat_stale` is "raised in-app by `api`" (L5642), and a GET handler never changes state (§16.5
L5442), so readiness, which sees the same heartbeat, does not raise it: the api's lifespan runs
`watch_heartbeat`, a task that runs `heartbeat_watch_pass` at its start and then every
`HEARTBEAT_WATCH_INTERVAL_S` until its `stop` is set. The external path for a dead worker is the dead-man
switch (§17.6).

**A pass** is one transaction. It reads the heartbeat's age on the database clock, which wrote it, with the
readiness rule (`heartbeats.HEARTBEAT_STALE_S`): a heartbeat at most 60 s old is fresh, and the open alert is
resolved (`fresh`); an older one, or none at all, raises the critical alert, subject `worker`, its age in
seconds (`null` without a heartbeat) in its context (`stale`), but only once the api has been up for
`START_GRACE_S` (`now - started_at`, on the caller's clock): an api that starts with the worker gives it time
to take its lock and beat first, and writes nothing meanwhile (`grace`). A fresh heartbeat resolves the alert
within the grace too, so an api restarted while it was open resolves it at its first pass.

**The watch** is a task boundary: a pass that raises is logged `heartbeat_watch_failed` with the error's class
only (a database error can carry statement parameters), and the next pass runs on time.
"""

import asyncio
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Final, Literal

from sqlalchemy.ext.asyncio import AsyncEngine

from jobhunter.db.sql import q
from jobhunter.heartbeats import HEARTBEAT_STALE_S
from jobhunter.observability.alerts import raise_alert, resolve_alert
from jobhunter.observability.logging import get_logger

HEARTBEAT_WATCH_INTERVAL_S: Final = 30.0
START_GRACE_S: Final = 120.0
HEARTBEAT_STALE_ALERT: Final = "worker.heartbeat_stale"
HEARTBEAT_STALE_SUBJECT: Final = "worker"

_STALE_AFTER: Final = timedelta(seconds=HEARTBEAT_STALE_S)
_GRACE: Final = timedelta(seconds=START_GRACE_S)
# The database clock, which also wrote `heartbeat_at`: readiness reads the same age (`observability.health`).
_HEARTBEAT_AGE: Final = q("SELECT now() - heartbeat_at FROM worker_heartbeat WHERE id = 1")

WatchVerdict = Literal["stale", "fresh", "grace"]

log = get_logger("jobhunter.observability.heartbeat_watch")


async def heartbeat_watch_pass(engine: AsyncEngine, *, now: datetime, started_at: datetime) -> WatchVerdict:
    """One pass of the watch (module docstring), in one transaction: `fresh` (the alert resolved), `stale`
    (raised, once `now - started_at` reaches `START_GRACE_S`) or `grace` (a stale heartbeat within the grace;
    nothing written)."""
    async with engine.begin() as conn:
        age: timedelta | None = (await conn.execute(_HEARTBEAT_AGE)).scalar_one_or_none()
        if age is not None and age <= _STALE_AFTER:
            await resolve_alert(conn, code=HEARTBEAT_STALE_ALERT, subject=HEARTBEAT_STALE_SUBJECT)
            return "fresh"
        if now - started_at < _GRACE:
            return "grace"
        age_s = None if age is None else int(age.total_seconds())
        await raise_alert(
            conn,
            code=HEARTBEAT_STALE_ALERT,
            severity="critical",
            subject=HEARTBEAT_STALE_SUBJECT,
            message="the worker heartbeat is missing or older than 60 s",
            context={"age_s": age_s},
        )
    log.warning("worker_heartbeat_stale", age_s=age_s)
    return "stale"


async def watch_heartbeat(engine: AsyncEngine, *, clock: Callable[[], datetime], stop: asyncio.Event) -> None:
    """Run a pass at the start and every `HEARTBEAT_WATCH_INTERVAL_S` until `stop` is set; the grace counts
    from the start, on `clock`. A failing pass is logged and the watch goes on (module docstring)."""
    started_at = clock()
    while not stop.is_set():
        try:
            await heartbeat_watch_pass(engine, now=clock(), started_at=started_at)
        except Exception as exc:  # noqa: BLE001 - a task boundary: the next pass runs; its class only is logged
            log.warning("heartbeat_watch_failed", error_class=type(exc).__name__)
        with suppress(TimeoutError):
            async with asyncio.timeout(HEARTBEAT_WATCH_INTERVAL_S):
                await stop.wait()
