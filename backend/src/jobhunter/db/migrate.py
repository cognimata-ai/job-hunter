"""`jobhunter migrate` (spec §19.3): the steps in their fixed order, run as the migrator role (the restore
test: the restore role, on its scratch database).

Steps 1 and 3 advance the identities past every bound of the deletion-manifest stream and of the manifests
the database records, step 1 after writing the files of recorded manifests the stream lacks
(`privacy.identities`). Step 5 ends with the same advancement over every table, once procrastinate's and the
checkpointer's schemas exist: on a first installation the queue's `bigserial` sequences are created only
there, after steps 1 and 3, and a queue job must never take an id an earlier delete-everything lists. Nothing
allocates an id between their creation and that pass: both setups create schema only, and the checkpointer's
`checkpoint_migrations` has no sequence. The replay (step 7) still only logs that it did nothing; it arrives
with the two-phase replay (M5 Task 25).

The flags (M5 Task 23, Deviation 38) serve the restore code and the restore test (§19.5): `steps` runs a
subset of step groups (`STEP_GROUPS`), always in `MIGRATE_ORDER`; `database` and `db_role` select the database
and the login of every step (`AppConfig.dsn`); `manifest_dir`, `cv_dir` and `exports_dir` replace the volumes
of the configuration (`manifests_dir`, `cv_store_dir`, `exports_dir`), so a scratch run never touches the live
ones.
"""

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal, assert_never

import procrastinate
import psycopg
from procrastinate import PsycopgConnector
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jobhunter.clock import utcnow
from jobhunter.config import AppConfig, DbRole
from jobhunter.db.alembic_runner import upgrade_head
from jobhunter.db.compat import chain_compat, record_compat
from jobhunter.db.seed import seed_defaults
from jobhunter.db.session import make_null_engine
from jobhunter.db.sql import q
from jobhunter.graphs.checkpointer import setup_checkpointer
from jobhunter.observability.logging import get_logger
from jobhunter.privacy.identities import advance_identities, manifest_bounds, rewrite_missing_files
from jobhunter.privacy.manifests import FileRoots

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
# `--steps`: each group's steps (§19.4, §19.5's restore test runs `sequences,alembic,replay`).
StepGroup = Literal["sequences", "alembic", "replay"]
STEP_GROUPS: Final[Mapping[StepGroup, tuple[MigrateStep, ...]]] = MappingProxyType(
    {
        "sequences": ("reconstruct_and_advance", "advance_again"),
        "alembic": ("alembic",),
        "replay": ("replay",),
    }
)
# Tables of later milestones that the backup role writes (the backup role has no default privileges).
BACKUP_GRANT_TABLES: Final = ("backup_sets", "backup_runs", "manifest_shipments", "manifest_shipment_items")
_REPLAY_NOOP_REASON: Final = "replay_arrives_later_in_M5"
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


def selected_steps(steps: frozenset[StepGroup] | None) -> tuple[MigrateStep, ...]:
    """The steps a selection of step groups runs, in `MIGRATE_ORDER`; every step without one."""
    if steps is None:
        return MIGRATE_ORDER
    chosen = {step for group in steps for step in STEP_GROUPS[group]}
    return tuple(step for step in MIGRATE_ORDER if step in chosen)


@dataclass(frozen=True)
class _Target:
    """What a run works on: the database as one login (`dsn` for psycopg, `sqlalchemy_dsn`), the manifest
    stream, and the volumes whose files the replay removes (step 7)."""

    dsn: str
    sqlalchemy_dsn: str
    manifest_dir: Path
    files: FileRoots


async def _setup_queue(dsn: str) -> None:
    """Apply the procrastinate schema when it is absent. Upgrades of that schema are owned by M5."""
    app = procrastinate.App(connector=PsycopgConnector(conninfo=dsn, min_size=1, max_size=1))
    async with app.open_async():
        absent = await app.connector.execute_query_one_async(
            "SELECT to_regclass('public.procrastinate_jobs') IS NULL AS absent"
        )
        if absent["absent"]:
            await app.schema_manager.apply_schema_async()
        log.info("procrastinate_schema", applied=bool(absent["absent"]))


async def _advance(conn: AsyncConnection, step: MigrateStep, manifest_dir: Path) -> None:
    """Advance every identity of the tables that exist past the bounds of the stream and the recorded
    manifests (`privacy.identities`)."""
    advanced = await advance_identities(conn, await manifest_bounds(conn, manifest_dir))
    log.info("migrate_identities_advanced", step=step, advanced=advanced)


async def _run_step(step: MigrateStep, target: _Target, engine: AsyncEngine, now: datetime) -> None:
    match step:
        case "reconstruct_and_advance":
            async with engine.begin() as conn:
                rewritten = await rewrite_missing_files(conn, target.manifest_dir)
                if rewritten:
                    log.info(
                        "migrate_manifest_files_rewritten", step=step, manifests=[str(u) for u in rewritten]
                    )
                await _advance(conn, step, target.manifest_dir)
        case "advance_again":
            async with engine.begin() as conn:
                await _advance(conn, step, target.manifest_dir)
        case "replay":
            log.info("migrate_step_noop", step=step, reason=_REPLAY_NOOP_REASON)
        case "alembic":
            # The rollback compatibility floors of the chain (M5): validated before the schema moves, so a
            # mis-declared floor fails here, and recorded once the schema is at the head
            chain = chain_compat()
            await asyncio.to_thread(upgrade_head, target.sqlalchemy_dsn)
            async with engine.begin() as conn:
                log.info("migrate_compat_recorded", written=await record_compat(conn, chain=chain))
        case "grants":
            async with engine.begin() as conn:
                log.info("migrate_grants", granted=await apply_explicit_grants(conn))
        case "queue_and_checkpointer":
            await _setup_queue(target.dsn)
            await setup_checkpointer(target.dsn)
            # The queue's sequences exist from here on (module docstring)
            async with engine.begin() as conn:
                await _advance(conn, step, target.manifest_dir)
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


async def run_migrate_async(
    cfg: AppConfig,
    *,
    now: datetime | None = None,
    steps: frozenset[StepGroup] | None = None,
    database: str | None = None,
    manifest_dir: Path | None = None,
    cv_dir: Path | None = None,
    exports_dir: Path | None = None,
    db_role: DbRole | None = None,
) -> list[MigrateStep]:
    """Run the steps of `MIGRATE_ORDER` that `steps` selects (every one without it) as `db_role` (default: the
    configuration role's) on `database` (default: `JOBHUNTER_DB_NAME`); returns the steps run, in order.

    `manifest_dir`, `cv_dir` and `exports_dir` default to the configuration's volumes (module docstring).
    Concurrent runs on a database are serialised on `MIGRATE_LOCK_KEY`. `now` (default: the current time) only
    matters to the first seeding. A step that fails raises (after one `migrate_failed` event with the step and
    the exception class, never its message) and the steps after it do not run; every step is idempotent, so
    the next `migrate` starts over.
    """
    moment = utcnow() if now is None else now
    order = selected_steps(steps)
    phase = "lock"  # what `migrate_failed` names when the failure comes before the first step
    try:
        target = _Target(
            dsn=cfg.dsn("psycopg", db_role=db_role, database=database),
            sqlalchemy_dsn=cfg.dsn("sqlalchemy", db_role=db_role, database=database),
            manifest_dir=cfg.manifests_dir if manifest_dir is None else manifest_dir,
            files=FileRoots(
                cv_store=cfg.cv_store_dir if cv_dir is None else cv_dir,
                exports=cfg.exports_dir if exports_dir is None else exports_dir,
            ),
        )
        async with _migrate_lock(target.dsn):
            engine = make_null_engine(target.sqlalchemy_dsn)
            try:
                for step in order:
                    phase = step
                    log.info("migrate_step", step=step)
                    await _run_step(step, target, engine, moment)
            finally:
                await engine.dispose()
    except Exception as exc:
        log.error("migrate_failed", step=phase, error_type=type(exc).__name__)
        raise
    return list(order)


def run_migrate(
    cfg: AppConfig,
    *,
    now: datetime | None = None,
    steps: frozenset[StepGroup] | None = None,
    database: str | None = None,
    manifest_dir: Path | None = None,
    cv_dir: Path | None = None,
    exports_dir: Path | None = None,
    db_role: DbRole | None = None,
) -> list[MigrateStep]:
    """`run_migrate_async` for synchronous callers (the CLI, sync fixtures, sync test bodies), with its flags.

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
    return asyncio.run(
        run_migrate_async(
            cfg,
            now=now,
            steps=steps,
            database=database,
            manifest_dir=manifest_dir,
            cv_dir=cv_dir,
            exports_dir=exports_dir,
            db_role=db_role,
        )
    )
