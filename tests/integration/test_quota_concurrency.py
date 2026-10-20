import asyncio

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.quota import ledger
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.collect import planned_request, queued_run
from tests.support.pg import PgServer, lock_waits
from tests.support.procs import await_until
from tests.support.quota import SETTINGS, insert_window, jsearch_window

pytestmark = pytest.mark.integration
WORKERS = 8
PERIOD = (
    2_675_242  # a schedule as long as the window: its bucket holds every unit, so the limit is what binds
)
LOCK_WINDOW = sa.text("SELECT 1 FROM quota_windows WHERE provider = 'jsearch' AND is_current FOR UPDATE")
COUNTED = sa.text(
    "SELECT COALESCE(SUM(units), 0) FROM quota_ledger "
    "WHERE provider = 'jsearch' AND state IN ('reserved', 'dispatched', 'billed', 'ambiguous')"
)


async def test_eight_concurrent_reservers_never_exceed_the_limit(
    engine: AsyncEngine, current_epoch: int, migrated: PgServer
) -> None:
    # a validated jsearch window with limit 5, reserve 0, tokens 5
    now = utcnow()
    await insert_window(engine, jsearch_window(now, limit=5, reserve=0, tokens=5, interval_s=PERIOD))
    owner = await queued_run(engine)
    keys = [n.to_bytes(32, "big") for n in range(WORKERS)]
    requests = [await planned_request(engine, owner, provider="jsearch", key=k, attempt_seq=1) for k in keys]

    async def reserve(k: bytes, fr: int) -> ledger.Reservation | ledger.Denial:
        async with engine.begin() as c:
            return await ledger.reserve(
                c,
                "jsearch",
                owner=owner,
                request_key=k,
                attempt_seq=1,
                request_class="normal",
                fetch_request_id=fr,
                settings=SETTINGS,
                interval_s=PERIOD,
                now=now,
            )

    # The barrier: the window row is held while the 8 reservers start, so all wait for the quota lock at once.
    async with engine.connect() as blocker:
        await blocker.begin()
        await blocker.execute(LOCK_WINDOW)
        tasks = [asyncio.create_task(reserve(k, fr)) for k, fr in zip(keys, requests, strict=True)]

        async def all_waiting() -> bool:
            return lock_waits(migrated) >= WORKERS or any(t.done() for t in tasks)

        await await_until(all_waiting, 30)
        assert not any(t.done() for t in tasks)  # nobody got past the window lock
        await blocker.rollback()
    results = await asyncio.gather(*tasks)

    admitted = [r for r in results if isinstance(r, ledger.Reservation)]
    denied = [r for r in results if isinstance(r, ledger.Denial)]
    assert (len(admitted), len(denied)) == (5, 3)
    assert {d.code for d in denied} == {"exhausted"}
    async with engine.connect() as c:
        assert (await c.execute(COUNTED)).scalar_one() == 5
        assert await ledger.used_units(c, "jsearch", now) == 5
