"""`jobhunter migrate` (spec §19.3): the steps in their fixed order, run as the migrator role.

M0 has no deletion-manifest stream, so steps 1, 3 and 7 only log that they did nothing. The stream, with the
identity advancement and the replay, arrives in M5.
"""

import asyncio
from datetime import datetime
from typing import Final, Literal, assert_never

import procrastinate
from procrastinate import PsycopgConnector
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jobhunter.clock import utcnow
from jobhunter.config import AppConfig
from jobhunter.db.alembic_runner import upgrade_head
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
            await asyncio.to_thread(upgrade_head, cfg.dsn("sqlalchemy"))
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


async def run_migrate_async(cfg: AppConfig, *, now: datetime | None = None) -> list[MigrateStep]:
    """Run every step of `MIGRATE_ORDER` as the migrator; returns the steps run, in order.

    `now` (default: the current time) only matters to the first seeding. A step that fails raises and the
    steps after it do not run; every step is idempotent, so the next `migrate` starts over.
    """
    moment = utcnow() if now is None else now
    engine = make_null_engine(cfg.dsn("sqlalchemy"))
    try:
        for step in MIGRATE_ORDER:
            log.info("migrate_step", step=step)
            await _run_step(step, cfg, engine, moment)
    finally:
        await engine.dispose()
    return list(MIGRATE_ORDER)


def run_migrate(cfg: AppConfig, *, now: datetime | None = None) -> list[MigrateStep]:
    """`run_migrate_async` for synchronous callers (the CLI, sync fixtures, sync test bodies).

    Raises `RuntimeError` before touching the database when called inside a running event loop: `asyncio.run`
    would fail there anyway, and `migrate` never nests event loops.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(run_migrate_async(cfg, now=now))
    raise RuntimeError("run_migrate cannot run inside a running event loop; await run_migrate_async instead")
