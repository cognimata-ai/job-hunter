"""`jobhunter migrate` (spec §19.3): the steps in their fixed order, run as the migrator role.

M0 has no deletion-manifest stream, so steps 1, 3 and 7 only log that they did nothing. The stream, with the
identity advancement and the replay, arrives in M5.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from typing import Final, Literal, assert_never

import procrastinate
import psycopg
from procrastinate import PsycopgConnector
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jobhunter.clock import utcnow
from jobhunter.config import AppConfig
from jobhunter.db.alembic_runner import upgrade_head
from jobhunter.db.compat import chain_compat, record_compat
from jobhunter.db.seed import seed_defaults
from jobhunter.db.session import make_null_engine
from jobhunter.db.sql import q
from jobhunter.graphs.checkpointer import setup_checkpointer
from jobhunter.observability.logging import get_logger

log = get_logger("jobhunter.migrate")

MigrateStep = Literal[
    "reconstruct_and_advance",
    "alembic",
    "advance_again",
    "grants",
    "queue_and_checkpointer",
    "seed",
    "replay",
]
MIGRATE_ORDER: Final[tuple[MigrateStep, ...]] = (
    "reconstruct_and_advance",
    "alembic",
    "advance_again",
    "grants",
    "queue_and_checkpointer",
    "seed",
    "replay",
)
# Tables of later milestones that the backup role writes (the backup role has no default privileges).
BACKUP_GRANT_TABLES: Final = ("backup_sets", "backup_runs", "manifest_shipments", "manifest_shipment_items")
_MANIFEST_NOOP_REASON: Final = "manifest_stream_arrives_in_M5"
# Session-level advisory lock that serialises `migrate` runs. The project's advisory keys: a session lock and
# a transaction lock on the same key conflict, so each use has a key of its own. PostgreSQL keeps the one-key
# form (one bigint; `objsubid = 1` in `pg_locks`) and the two-key form (two integers; `objsubid = 2`) apart,
# so these are two key spaces, and a two-key lock never conflicts with a one-key lock of the same number:
#   7261001  worker singleton (one key, session lock)
#   7261002  login throttle (one key, transaction lock)
#   7261003  migrate (one key, session lock)
#   7261004  duplicate grouping's company lock: the two-key form `(7261004, hashtext(company_key))`, a
#            transaction lock (`dedup.groups.COMPANY_LOCK_NAMESPACE`, plan Deviation 46)
#   7261005  dead-man switch (one key, session lock; `observability.deadman.DEADMAN_LOCK_KEY`, M5 Task 5)
MIGRATE_LOCK_KEY: Final = 7261003
_LOCK_POLL_S: Final = 0.5


async def apply_explicit_grants(conn: AsyncConnection) -> list[str]:
    """Grant `INSERT, UPDATE` to the backup role on each table of `BACKUP_GRANT_TABLES` that exists.

    Idempotent. Returns the tables it granted on, in the order of `BACKUP_GRANT_TABLES`; a table that does
    not exist yet (the M0 schema has none of them) is skipped, and a later `migrate` grants on it.
    """
    quote = conn.dialect.identifier_preparer.quote
    granted: list[str] = []
    for table in BACKUP_GRANT_TABLES:
        exists = (
            await conn.execute(q("SELECT to_regclass(:name) IS NOT NULL"), {"name": f"public.{table}"})
        ).scalar_one()
        if exists:
            await conn.execute(q(f"GRANT INSERT, UPDATE ON public.{quote(table)} TO jobhunter_backup"))
            granted.append(table)
    return granted


async def _setup_queue(cfg: AppConfig) -> None:
    """Apply the procrastinate schema when it is absent. Upgrades of that schema are owned by M5."""
    app = procrastinate.App(connector=PsycopgConnector(conninfo=cfg.dsn("psycopg"), min_size=1, max_size=1))
    async with app.open_async():
        absent = await app.connector.execute_query_one_async(
            "SELECT to_regclass('public.procrastinate_jobs') IS NULL AS absent"
        )
        if absent["absent"]:
            await app.schema_manager.apply_schema_async()
        log.info("procrastinate_schema", applied=bool(absent["absent"]))


async def _run_step(step: MigrateStep, cfg: AppConfig, engine: AsyncEngine, now: datetime) -> None:
    match step:
        case "reconstruct_and_advance" | "advance_again" | "replay":
            log.info("migrate_step_noop", step=step, reason=_MANIFEST_NOOP_REASON)
        case "alembic":
            # The rollback compatibility floors of the chain (M5): validated before the schema moves, so a
            # mis-declared floor fails here, and recorded once the schema is at the head
            chain = chain_compat()
            await asyncio.to_thread(upgrade_head, cfg.dsn("sqlalchemy"))
            async with engine.begin() as conn:
                log.info("migrate_compat_recorded", written=await record_compat(conn, chain=chain))
        case "grants":
            async with engine.begin() as conn:
                log.info("migrate_grants", granted=await apply_explicit_grants(conn))
        case "queue_and_checkpointer":
            await _setup_queue(cfg)
            await setup_checkpointer(cfg.dsn("psycopg"))
        case "seed":
            async with engine.begin() as conn:
                await seed_defaults(conn, now=now)
        case _:
            assert_never(step)


async def _try_lock(conn: psycopg.AsyncConnection) -> bool:
    cursor = await conn.execute("SELECT pg_try_advisory_lock(%s::bigint)", (MIGRATE_LOCK_KEY,))
    row = await cursor.fetchone()
    return row is not None and bool(row[0])


@asynccontextmanager
async def _migrate_lock(dsn: str) -> AsyncIterator[None]:
    """Hold `MIGRATE_LOCK_KEY` on a dedicated autocommit connection for the length of the block.

    A second `migrate` waits here (and says so), then finds every step already done. The lock is released
    explicitly and the connection is closed on every path; closing it would also drop the lock.

    The wait polls `pg_try_advisory_lock` instead of blocking in `pg_advisory_lock`: a statement that is
    blocked holds a snapshot, and the checkpointer setup of the run that has the lock runs
    `CREATE INDEX CONCURRENTLY`, which waits for every older snapshot of the database. The two would wait for
    each other, through this process, and neither would ever finish.
    """
    conn = await psycopg.AsyncConnection.connect(
        dsn, autocommit=True, application_name="jobhunter-migrate-lock"
    )
    try:
        acquired = await _try_lock(conn)
        if not acquired:
            log.info("migrate_lock_wait", lock_key=MIGRATE_LOCK_KEY)
        while not acquired:  # polls the database, so there is no event to wait for
            await asyncio.sleep(_LOCK_POLL_S)
            acquired = await _try_lock(conn)
        try:
            yield
        finally:
            # A lost connection took the lock with it; do not let that hide the error that ended the run.
            with suppress(psycopg.OperationalError):
                await conn.execute("SELECT pg_advisory_unlock(%s::bigint)", (MIGRATE_LOCK_KEY,))
    finally:
        await conn.close()


async def run_migrate_async(cfg: AppConfig, *, now: datetime | None = None) -> list[MigrateStep]:
    """Run every step of `MIGRATE_ORDER` as the migrator; returns the steps run, in order.

    Concurrent runs are serialised on `MIGRATE_LOCK_KEY`. `now` (default: the current time) only matters to
    the first seeding. A step that fails raises (after one `migrate_failed` event with the step and the
    exception class, never its message) and the steps after it do not run; every step is idempotent, so the
    next `migrate` starts over.
    """
    moment = utcnow() if now is None else now
    phase = "lock"  # what `migrate_failed` names when the failure comes before the first step
    try:
        async with _migrate_lock(cfg.dsn("psycopg")):
            engine = make_null_engine(cfg.dsn("sqlalchemy"))
            try:
                for step in MIGRATE_ORDER:
                    phase = step
                    log.info("migrate_step", step=step)
                    await _run_step(step, cfg, engine, moment)
            finally:
                await engine.dispose()
    except Exception as exc:
        log.error("migrate_failed", step=phase, error_type=type(exc).__name__)
        raise
    return list(MIGRATE_ORDER)


def run_migrate(cfg: AppConfig, *, now: datetime | None = None) -> list[MigrateStep]:
    """`run_migrate_async` for synchronous callers (the CLI, sync fixtures, sync test bodies).

    Raises `RuntimeError` before touching the database when called inside a running event loop: `asyncio.run`
    would fail there anyway, and `migrate` never nests event loops.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass  # no loop is running: the normal case
    else:
        raise RuntimeError(
            "run_migrate cannot run inside a running event loop; await run_migrate_async instead"
        )
    # Outside the `except` block, so a failure of the run is not chained to "no running event loop".
    return asyncio.run(run_migrate_async(cfg, now=now))
