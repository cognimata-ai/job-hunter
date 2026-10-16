"""Worker epochs and the takeover sweep (spec §3.3.1 step 3).

Right after it acquires the singleton lock, the worker claims the next epoch in `worker_heartbeat` and, in
the same transaction, runs every registered takeover sweep. A sweep invalidates the work of older epochs
(runs, score targets, score cycles, operations) and returns how many rows it took over; the modules that own
that work register their sweeps with `register_takeover_sweep`. Old owners are thus fenced out atomically at
takeover.

The claim first proves, in the same transaction, that the claimant's lock session holds the singleton lock;
a claimant that does not is `FencedOut` before anything is written or swept (ruling T8-h).
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jobhunter.db.sql import q
from jobhunter.errors import FencedOut
from jobhunter.observability.logging import get_logger

TakeoverSweep = Callable[[AsyncConnection, int], Awaitable[int]]

# Whether `lock_pid` holds the granted singleton lock (advisory key 7261001) in this database.
_HOLDS_LOCK: Final = """\
SELECT EXISTS (SELECT 1 FROM pg_locks
                WHERE locktype = 'advisory' AND objid = 7261001 AND granted AND pid = :lock_pid
                  AND database = (SELECT oid FROM pg_database WHERE datname = current_database()))"""

# The first worker creates the row with epoch 1; every later one increments it and records itself.
_CLAIM_EPOCH: Final = """\
INSERT INTO worker_heartbeat (id, worker_instance_id, worker_epoch, hostname, pid, lock_backend_pid,
                              app_version, started_at, heartbeat_at)
VALUES (1, :instance_id, 1, :hostname, :pid, :lock_backend_pid, :app_version, now(), now())
ON CONFLICT (id) DO UPDATE SET worker_epoch = worker_heartbeat.worker_epoch + 1,
  worker_instance_id = EXCLUDED.worker_instance_id, hostname = EXCLUDED.hostname, pid = EXCLUDED.pid,
  lock_backend_pid = EXCLUDED.lock_backend_pid, app_version = EXCLUDED.app_version,
  started_at = now(), heartbeat_at = now()
RETURNING worker_epoch"""

# Insertion ordered: the sweeps run in registration order.
_SWEEPS: dict[str, TakeoverSweep] = {}

log = get_logger("jobhunter.worker.epoch")


@dataclass(frozen=True)
class WorkerIdentity:
    instance_id: UUID
    hostname: str
    pid: int
    lock_backend_pid: int
    app_version: str


def register_takeover_sweep(name: str) -> Callable[[TakeoverSweep], TakeoverSweep]:
    """Decorator: run the sweep in every takeover transaction; its count is reported under `name`."""

    def register(sweep: TakeoverSweep) -> TakeoverSweep:
        registered = _SWEEPS.get(name)
        if registered is not None and registered is not sweep:
            raise ValueError(f"another takeover sweep is already registered as {name!r}")
        _SWEEPS[name] = sweep
        return sweep

    return register


async def claim_epoch_and_sweep(engine: AsyncEngine, ident: WorkerIdentity) -> tuple[int, dict[str, int]]:
    """Claim the next worker epoch for `ident` and run every takeover sweep, all in one transaction.

    Returns the new epoch and the row count of each sweep by name. A failing sweep rolls the whole transaction
    back, the epoch claim included. When `ident.lock_backend_pid` does not hold the singleton lock, it raises
    `FencedOut(owner="worker_lock")` having written nothing and run no sweep.
    """
    async with engine.begin() as conn:
        holds_lock = (await conn.execute(q(_HOLDS_LOCK), {"lock_pid": ident.lock_backend_pid})).scalar_one()
        if holds_lock is not True:
            # Nothing has been written: ending the transaction by raising loses nothing.
            log.warning("worker_epoch_claim_fenced", lock_backend_pid=ident.lock_backend_pid)
            raise FencedOut(owner="worker_lock")
        claimed = (
            await conn.execute(
                q(_CLAIM_EPOCH),
                {
                    "instance_id": ident.instance_id,
                    "hostname": ident.hostname,
                    "pid": ident.pid,
                    "lock_backend_pid": ident.lock_backend_pid,
                    "app_version": ident.app_version,
                },
            )
        ).scalar_one()
        epoch = int(claimed)
        counts = {name: await sweep(conn, epoch) for name, sweep in list(_SWEEPS.items())}
    log.info("worker_epoch_claimed", epoch=epoch, instance_id=str(ident.instance_id), swept=counts)
    return epoch, counts
