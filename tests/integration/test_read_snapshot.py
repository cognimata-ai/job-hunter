"""The read snapshot of the list routes (spec §14.3 L5079, §7.1; plan B13): one read-only `REPEATABLE READ`
transaction, so the ranking generation a page carries describes exactly the rows on it; and the generation
reader."""

from typing import Final

import pytest
import sqlalchemy as sa
from jobhunter.db import ranking
from jobhunter.db.session import make_null_engine
from jobhunter.db.snapshot import read_snapshot
from jobhunter.db.sql import pg_error
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.m3 import insert_job
from tests.support.pg import PgServer

pytestmark = pytest.mark.integration
READ_ONLY_SQL_TRANSACTION: Final = "25006"  # psycopg's `ReadOnlySqlTransaction`
_JOB_IDS: Final = sa.text("SELECT id FROM jobs ORDER BY id")


async def test_generation_and_rows_come_from_one_snapshot(engine: AsyncEngine) -> None:
    async with read_snapshot(engine) as snapshot:
        before = await ranking.generation(snapshot)
        jobs_before = list((await snapshot.execute(_JOB_IDS)).scalars())
        async with engine.begin() as other:  # commits while the snapshot is open
            job = await insert_job(other)
            await ranking.bump(other)
        assert await ranking.generation(snapshot) == before
        assert list((await snapshot.execute(_JOB_IDS)).scalars()) == jobs_before
        assert job.job_id not in jobs_before
    async with engine.connect() as later:
        assert await ranking.generation(later) == before + 1
        assert job.job_id in list((await later.execute(_JOB_IDS)).scalars())


async def test_the_snapshot_is_read_only(engine: AsyncEngine) -> None:
    async with read_snapshot(engine) as snapshot:
        isolation = (await snapshot.execute(sa.text("SHOW transaction_isolation"))).scalar_one()
        read_only = (await snapshot.execute(sa.text("SHOW transaction_read_only"))).scalar_one()
        assert (isolation, read_only) == ("repeatable read", "on")
        with pytest.raises(sa.exc.DBAPIError) as refusal:
            await insert_job(snapshot)
    assert type(refusal.value.orig).__name__ == "ReadOnlySqlTransaction"
    assert pg_error(refusal.value).sqlstate == READ_ONLY_SQL_TRANSACTION
    async with engine.connect() as later:
        assert list((await later.execute(_JOB_IDS)).scalars()) == []


async def test_the_snapshot_is_rolled_back_when_its_block_raises(engine: AsyncEngine) -> None:
    """The block's error passes through, and the connection goes back to the pool without a transaction."""
    with pytest.raises(LookupError, match="the caller's"):
        async with read_snapshot(engine) as snapshot:
            await ranking.generation(snapshot)
            raise LookupError("the caller's")
    assert snapshot.closed and not snapshot.in_transaction()
    async with read_snapshot(engine) as again:  # the pooled connection takes a new snapshot
        assert await ranking.generation(again) == 0


async def test_generation_without_the_row_is_a_lookup_error(migrated: PgServer) -> None:
    owner = make_null_engine(migrated.dsn("migrator", driver="sqlalchemy"))
    try:
        async with owner.connect() as c:
            assert await ranking.generation(c) == 0  # the reset sets it to 0
            await c.execute(sa.text("DELETE FROM ranking_state"))
            with pytest.raises(LookupError, match="ranking_state"):
                await ranking.generation(c)
            await c.rollback()
    finally:
        await owner.dispose()
