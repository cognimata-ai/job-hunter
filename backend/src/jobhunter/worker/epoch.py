"""Worker epochs and the takeover sweep (spec §3.3.1 step 3).

Right after it acquires the singleton lock, the worker claims the next epoch in `worker_heartbeat` and, in
the same transaction, runs every registered takeover sweep. A sweep invalidates the work of older epochs
(runs, score targets, score cycles, operations) and returns how many rows it took over; the modules that own
that work register their sweeps with `register_takeover_sweep`. Old owners are thus fenced out atomically at
takeover.

The claim first proves, in the same transaction, that the claimant's lock session holds the singleton lock;
a claimant that does not is `FencedOut` before anything is written or swept (ruling T8-h).

A sweep returns its row count, or a `Swept` that adds an action to run once the takeover transaction has
committed (for example observing the metrics of the runs it ended, Task 12): nothing the sweep did is real
before that commit. Each action runs on its own: one that raises is logged `takeover_after_commit_failed`
(its sweep and error class only) and the others still run, because the takeover it follows is already final
(ruling T13-d).

The transaction runs under `lock_timeout` (`TAKEOVER_LOCK_TIMEOUT_MS`, 10 s, its first statement; ruling
T13-b). The sweeps wait for the row locks of older owners (they never skip a row), and a frozen holder's
pooled connection can keep such a lock for as long as it stays frozen, so the claim gives up instead of
waiting forever: the lock wait fails with SQLSTATE 55P03 and the whole transaction rolls back, the epoch
claim included. The worker then exits and its restart tries again (`worker.main`).
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jobhunter.db.sql import q
from jobhunter.errors import FencedOut
from jobhunter.observability.logging import get_logger


@dataclass(frozen=True)
class Swept:
    """A sweep's result: `count` rows taken over, and `after_commit`, run once the takeover has committed.

    `after_commit` should not raise (for example: observing metrics): the takeover it follows is already
    final. One that does is logged `takeover_after_commit_failed` and does not stop the others.
    """

    count: int
    after_commit: Callable[[], None] | None = None


TakeoverSweep = Callable[[AsyncConnection, int], Awaitable[int | Swept]]

# How long one lock wait of the takeover transaction may last before it fails with SQLSTATE 55P03.
TAKEOVER_LOCK_TIMEOUT_MS: Final = 10_000

# `SET LOCAL lock_timeout`, as `set_config(..., is_local => true)` so the value is a bind parameter: it holds
# until the transaction ends and never outlives it on the pooled connection.
_SET_LOCK_TIMEOUT: Final = "SELECT set_config('lock_timeout', :timeout, true)"

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


async def claim_epoch_and_sweep(
    engine: AsyncEngine, ident: WorkerIdentity, *, lock_timeout_ms: int = TAKEOVER_LOCK_TIMEOUT_MS
) -> tuple[int, dict[str, int]]:
    """Claim the next worker epoch for `ident` and run every takeover sweep, all in one transaction.

    Returns the new epoch and the row count of each sweep by name. A failing sweep rolls the whole transaction
    back, the epoch claim included, and no sweep's `after_commit` runs; after the commit, each runs in sweep
    order, and one that raises is logged without stopping the others. When `ident.lock_backend_pid` does not
    hold the singleton lock, it raises `FencedOut(owner="worker_lock")` having written nothing and run no
    sweep. A lock wait longer than `lock_timeout_ms` (tests pass a shorter one) raises the `OperationalError`
    of SQLSTATE 55P03 (`db.sql.LOCK_NOT_AVAILABLE`), and nothing is committed.
    """
    async with engine.begin() as conn:
        await conn.execute(q(_SET_LOCK_TIMEOUT), {"timeout": f"{lock_timeout_ms}ms"})
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
        swept = {name: await sweep(conn, epoch) for name, sweep in list(_SWEEPS.items())}
    counts: dict[str, int] = {}
    for name, result in swept.items():
        if isinstance(result, Swept):
            counts[name] = result.count
            if result.after_commit is not None:
                _after_commit(name, epoch, result.after_commit)
        else:
            counts[name] = result
    log.info("worker_epoch_claimed", epoch=epoch, instance_id=str(ident.instance_id), swept=counts)
    return epoch, counts


def _after_commit(name: str, epoch: int, action: Callable[[], None]) -> None:
    """Run the after-commit action of sweep `name`; a failure is logged and goes no further (ruling T13-d).

    The takeover has committed, so nothing is left to roll back and the caller must still learn its epoch.
    The log line is the outcome this catch persists (§17.1): an action only observes (metrics), it records no
    state. Not the exception's message: it may carry content.
    """
    try:
        action()
    except Exception as exc:  # noqa: BLE001 - a committed takeover must not be masked; the log line is the outcome
        log.error("takeover_after_commit_failed", sweep=name, epoch=epoch, error_class=type(exc).__name__)
