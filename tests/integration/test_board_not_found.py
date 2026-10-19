from datetime import timedelta

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.sources import ats, http
from jobhunter.sources.base import CellPlan
from jobhunter.sources.breaker import HALF_OPEN_AFTER, available, record_failure, record_success
from jobhunter.sources.instances import instance_for_company, list_instances

pytestmark = pytest.mark.integration
GONE = (
    "INSERT INTO watchlist_companies "
    "(display_name, company_key, ats_type, board_slug, detection, validation_status) "
    "VALUES ('Gone', 'gone', 'greenhouse', 'gone-to-workday', 'manual', 'ok') RETURNING id"
)  # a slug the fixture routes answer with greenhouse_404
OPEN = sa.text(
    "SELECT count(*) FROM alerts WHERE dedup_key = :k AND severity = 'warn' AND resolved_at IS NULL"
)


async def instance(engine, iid: int):
    async with engine.connect() as c:
        return next(i for i in await list_instances(c) if i.id == iid)


async def test_board_404_marks_not_found_and_skips_until_half_open(
    engine, current_epoch, fake_http
) -> None:  # Review Focus 5 (planning: Task 13; postings: Task 19)
    now = utcnow()
    async with engine.begin() as c:
        iid = await instance_for_company(c, (await c.execute(sa.text(GONE))).scalar_one())
    plan = CellPlan(
        cell_id=0,
        source_id="greenhouse",
        instance_id=iid,
        query_text=None,
        query_lang=None,
        location_key=None,
        location_label=None,
        radius_km=None,
        phase="primary",
        request_class="free",
        bucket=None,
        window=(now, now),
        max_days_old=None,
    )
    async with http.make_client("greenhouse") as client:
        adapter = ats.adapter_for(await instance(engine, iid), client, clock=utcnow)
        outcome = await adapter.fetch(
            adapter.plan(plan)
        )  # one GET of the board's list URL, outside acquire_page
    assert outcome.page is not None and outcome.page.status == 404
    async with engine.begin() as c:
        assert await record_failure(c, iid, "not_found", billing="free", now=now) == "not_found"
    gone, key = await instance(engine, iid), {"k": f"source.not_found:instance:{iid}"}
    assert (
        available(gone, now) == "blocked"
        and available(gone, now + HALF_OPEN_AFTER - timedelta(seconds=1)) == "blocked"
    )
    assert (
        available(gone, now + HALF_OPEN_AFTER) == "half_open"
    )  # one probe after 6 h (its planning: Task 13)
    async with engine.connect() as c:
        assert (await c.execute(OPEN, key)).scalar_one() == 1  # `source.not_found` (warn), raised once
    async with engine.begin() as c:
        assert await record_success(c, iid, now=now + HALF_OPEN_AFTER) == "ok"  # the half-open probe answered
    async with engine.connect() as c:
        assert (await c.execute(OPEN, key)).scalar_one() == 0
    assert (await instance(engine, iid)).health == "ok"
