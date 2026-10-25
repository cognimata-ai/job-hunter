"""`schedule.stale_data` while the schedule is disabled (spec §17.5, §17.6; M0 Carry-forward 2, plan Task 17).

A user who switches the schedule off runs no collection on purpose, so the reconciler raises nothing and
resolves an alert opened before; switched on again, it judges the run age under the old rule.
"""

from datetime import datetime, timedelta

import pytest
import sqlalchemy as sa
from jobhunter.observability.alerts import raise_alert
from jobhunter.worker import runs
from jobhunter.worker.reconcilers import ReconcileContext
from jobhunter.worker.tasks import defer_in_tx
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.settings import save, settings_body

pytestmark = pytest.mark.integration

OPEN = sa.text("SELECT severity FROM alerts WHERE code = 'schedule.stale_data' AND resolved_at IS NULL")


async def seeded_at(engine: AsyncEngine) -> datetime:
    async with engine.connect() as conn:
        first: datetime = (
            await conn.execute(sa.text("SELECT min(created_at) FROM settings_versions"))
        ).scalar_one()
    return first


async def open_severity(engine: AsyncEngine) -> str | None:
    async with engine.connect() as conn:
        severity: str | None = (await conn.execute(OPEN)).scalar_one_or_none()
    return severity


async def set_schedule(engine: AsyncEngine, *, enabled: bool) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            sa.text("UPDATE schedule_state SET interval_s = 3600, enabled = :e"), {"e": enabled}
        )


async def test_a_disabled_schedule_raises_nothing_and_resolves_an_open_alert(
    engine, queue_app, current_epoch
) -> None:
    seeded = await seeded_at(engine)
    three_intervals = ReconcileContext(
        engine=engine, queue=queue_app, defer=defer_in_tx, now=seeded + timedelta(hours=3, minutes=1),
        epoch=current_epoch,
    )  # fmt: skip
    await set_schedule(engine, enabled=True)  # no run for three 1 h intervals: the old rule raises
    assert await runs.reconcile_stale_data(three_intervals) == 1
    assert await open_severity(engine) == "critical"
    await set_schedule(engine, enabled=False)
    assert await runs.reconcile_stale_data(three_intervals) == 1  # resolved
    assert await open_severity(engine) is None
    assert await runs.reconcile_stale_data(three_intervals) == 0  # and never raised while disabled
    async with engine.connect() as conn:
        count = await conn.execute(sa.text("SELECT count(*) FROM alerts WHERE code = 'schedule.stale_data'"))
        assert count.scalar_one() == 1
    await set_schedule(engine, enabled=True)
    assert await runs.reconcile_stale_data(three_intervals) == 1  # switched on again: the old rule
    assert await open_severity(engine) == "critical"


async def test_an_alert_opened_before_the_disable_is_resolved(engine, queue_app, current_epoch) -> None:
    seeded = await seeded_at(engine)
    async with engine.begin() as conn:
        await raise_alert(
            conn,
            code=runs.STALE_DATA_ALERT,
            severity="critical",
            subject=runs.STALE_DATA_SUBJECT,
            message="stale",
        )
    await set_schedule(engine, enabled=False)
    ctx = ReconcileContext(
        engine=engine, queue=queue_app, defer=defer_in_tx, now=seeded + timedelta(days=2), epoch=current_epoch
    )
    assert await runs.reconcile_stale_data(ctx) == 1 and await open_severity(engine) is None


async def test_a_settings_save_that_disables_the_schedule_exempts_it(
    logged_in_client, engine, queue_app, current_epoch
) -> None:
    payload = (await settings_body(logged_in_client))["payload"]
    payload["schedule"]["enabled"] = False
    await save(logged_in_client, payload)
    ctx = ReconcileContext(
        engine=engine, queue=queue_app, defer=defer_in_tx, now=await seeded_at(engine) + timedelta(days=3),
        epoch=current_epoch,
    )  # fmt: skip
    assert await runs.reconcile_stale_data(ctx) == 0 and await open_severity(engine) is None
    payload["schedule"]["enabled"] = True
    await save(logged_in_client, payload)
    assert await runs.reconcile_stale_data(ctx) == 1 and await open_severity(engine) == "critical"
