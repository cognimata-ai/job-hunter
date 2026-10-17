import time
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from jobhunter.worker.tasks import defer_in_tx
from jobhunter.worker.tick import run_tick

pytestmark = [pytest.mark.perf, pytest.mark.integration]


async def test_measure_long_downtime_tick(engine, queue_app, record_property) -> None:
    a = datetime(2026, 1, 1, tzinfo=UTC)
    async with engine.begin() as c:
        await c.execute(
            sa.text("UPDATE schedule_state SET anchor_utc=:a, interval_s=3600, last_evaluated_slot=:a"),
            {"a": a},
        )
    t0 = time.perf_counter()
    async with engine.begin() as c:
        await run_tick(c, now=a + timedelta(days=30), defer=defer_in_tx)
    record_property("tick_720_slots_seconds", time.perf_counter() - t0)
