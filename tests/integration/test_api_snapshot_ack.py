"""`POST /api/ats-snapshots/{snapshot_id}/acknowledge` (spec §14.2 L5044, §8.11; plan M4 Task 12): the user
confirms a board's mass disappearance at once, and the event's postings that are still `unknown` close
`mass_disappearance_confirmed`, as a confirmation by two later snapshots would close them.

200 `{snapshot_id, closed}`; an unknown snapshot 404 `not_found`; one that is no mass disappearance 409
`conflict_state`, writing nothing. The board is the fixtures' Greenhouse board of 20 postings (`board20`);
every datum is invented.
"""

from datetime import datetime, timedelta
from typing import Any, Final

import httpx
import pytest
import sqlalchemy as sa
from jobhunter.availability import snapshots
from jobhunter.availability.snapshots import apply_snapshot
from jobhunter.clock import utcnow
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.collect import Board
from tests.support.settings import generation

pytestmark = pytest.mark.integration

H: Final = timedelta(hours=1)
SNAPSHOT: Final = sa.text(
    "INSERT INTO ats_snapshots (source_instance_id, status, member_count, taken_at) "
    "VALUES (:i, 'complete', :n, :t) RETURNING id"
)
MEMBERS: Final = sa.text(
    "INSERT INTO ats_snapshot_members (snapshot_id, native_id) SELECT :s, unnest(CAST(:members AS text[]))"
)
POSTINGS: Final = sa.text(
    "SELECT p.availability, p.availability_reason, j.availability AS job_availability FROM postings p "
    "JOIN jobs j ON j.id = p.job_id WHERE p.source_instance_id = :i ORDER BY p.native_id"
)
EVENT: Final = sa.text("SELECT mass_event_snapshot_id FROM source_instances WHERE id = :i")
ACKNOWLEDGED: Final = sa.text("SELECT acknowledged_at FROM ats_snapshots WHERE id = :s")
ALERT_OPEN: Final = sa.text(
    "SELECT resolved_at IS NULL FROM alerts WHERE dedup_key = :k ORDER BY id DESC LIMIT 1"
)


def url(snapshot_id: int) -> str:
    return f"/api/ats-snapshots/{snapshot_id}/acknowledge"


async def no_guard(conn: AsyncConnection) -> None:
    return None


async def taken(engine: AsyncEngine, board: Board, members: set[str], at: datetime) -> int:
    """A recorded complete snapshot of the board with `members`, applied; its id."""
    async with engine.begin() as c:
        snapshot_id = int(
            (await c.execute(SNAPSHOT, {"i": board.instance_id, "n": len(members), "t": at})).scalar_one()
        )
        await c.execute(MEMBERS, {"s": snapshot_id, "members": sorted(members)})
    async with engine.begin() as c:
        await apply_snapshot(c, snapshot_id, now=utcnow(), guard=no_guard)
    return snapshot_id


async def rows(engine: AsyncEngine, statement: sa.TextClause, **params: Any) -> list[Any]:
    async with engine.connect() as c:
        return list((await c.execute(statement, params)).all())


async def scalar(engine: AsyncEngine, statement: sa.TextClause, **params: Any) -> Any:
    async with engine.connect() as c:
        return (await c.execute(statement, params)).scalar_one()


async def test_acknowledge_closes_the_absent_postings(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, board20: Board
) -> None:
    t = utcnow() - 3 * H
    opening = await taken(engine, board20, set(), t)  # none of the 20 present: the event opens
    followup = await taken(engine, board20, set(), t + H)
    assert await scalar(engine, EVENT, i=board20.instance_id) == opening
    alert = {"k": f"{snapshots.MASS_ALERT}:instance:{board20.instance_id}"}
    assert await scalar(engine, ALERT_OPEN, **alert) is True
    assert {tuple(r) for r in await rows(engine, POSTINGS, i=board20.instance_id)} == {
        ("unknown", None, "unknown")
    }
    before = await generation(engine)

    r = await logged_in_client.post(url(followup))  # a follow-up names its event too
    assert (r.status_code, r.json()) == (200, {"snapshot_id": followup, "closed": 20})
    assert {tuple(r) for r in await rows(engine, POSTINGS, i=board20.instance_id)} == {
        ("closed", "mass_disappearance_confirmed", "closed")
    }
    assert await scalar(engine, EVENT, i=board20.instance_id) is None  # the event ended
    assert await scalar(engine, ALERT_OPEN, **alert) is False
    for snapshot_id in (opening, followup):
        assert await scalar(engine, ACKNOWLEDGED, s=snapshot_id) is not None
    assert await generation(engine) == before + 1  # once, for the jobs' availability

    # the event is no longer open: 200, nothing closed, nothing bumped
    r = await logged_in_client.post(url(opening))
    assert (r.status_code, r.json()) == (200, {"snapshot_id": opening, "closed": 0})
    assert await generation(engine) == before + 1


async def test_unknown_404_and_not_a_mass_disappearance_409(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, board20: Board
) -> None:
    present = {f"p{n:02d}" for n in range(1, 21)}
    complete = await taken(engine, board20, present, utcnow() - H)  # every posting present: no event
    before = await generation(engine)

    r = await logged_in_client.post(url(10**12))
    assert (r.status_code, r.json()["code"]) == (404, "not_found")
    r = await logged_in_client.post(url(complete))
    assert (r.status_code, r.json()["code"]) == (409, "conflict_state")
    assert "retryable" not in r.json()
    assert await scalar(engine, ACKNOWLEDGED, s=complete) is None
    assert {tuple(r) for r in await rows(engine, POSTINGS, i=board20.instance_id)} == {
        ("active", None, "active")
    }
    assert await generation(engine) == before
    r = await logged_in_client.post(url(0))  # no snapshot id below 1
    assert (r.status_code, r.json()["code"]) == (422, "validation_failed")
