"""`PUT /api/jobs/{job_id}/actions` over HTTP (spec §14.2 L4991, §7.13, §7.1 L1255; plan M4 Task 7, B11).

- `true` sets a timestamp to the request's time unless it already has one, `false` clears it and an absent
  field leaves it; `note: ""` clears the note, which holds at most 2,000 code points and no NUL or lone
  surrogate (422, the text never echoed nor logged).
- `seen_on_open` sets `seen_at` like `seen: true` without bumping the ranking generation; every other write
  bumps it once, changed values or not (L1255). `seen_on_open` with `seen`, and a body that sets nothing, are
  422.
- `apply_to_group` writes `saved`, `applied` and `hidden` on every other member of the job's active duplicate
  group too, never `seen` or the note; `members` is their rows after the write, by job id. Without an active
  group, or without one of those three, only the job is written. A member that leaves the group while the
  PUT waits for its row is not written.
- Every write sets `updated_at` to the request's time (the route's clock), the first one creating the row.
- A PUT that meets a merge holding the job waits for it, then answers 404 with the survivor and writes
  nothing; an unknown, synthetic or merged job is 404, a request without the session 401, without the CSRF
  header 403.

The clock is injected (`jobhunter.api.routes.jobs.utcnow`), far from the database's own. Every datum and note
is invented.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx
import pytest
import sqlalchemy as sa
from jobhunter.api.routes import jobs as jobs_route
from jobhunter.db import ranking
from jobhunter.dedup import merge
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.collect import put_user_actions, user_actions
from tests.support.fixtures import count_rows
from tests.support.m3 import insert_group, insert_job, insert_row
from tests.support.pg import LockWait, PgServer, lock_wait_details
from tests.support.procs import await_until
from tests.support.settings import generation

pytestmark = pytest.mark.integration

T0: Final = datetime(2025, 12, 1, 8, 0, tzinfo=UTC)
T1: Final = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
T2: Final = T1 + timedelta(days=1)
T3: Final = T1 + timedelta(days=2)
T4: Final = T1 + timedelta(days=3)
NOTE_LIMIT: Final = 2_000  # code points (§7.13: one API edit ≤ 2,000)
JSON: Final = {"content-type": "application/json"}
WAIT_S: Final = 30.0


class Clock:
    """The route's `utcnow`, set by the test."""

    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    tick = Clock(T1)
    monkeypatch.setattr(jobs_route, "utcnow", tick)
    return tick


def url(job_id: int) -> str:
    return f"/api/jobs/{job_id}/actions"


def z(moment: datetime | None) -> str | None:
    """A time as the api writes it (UTC, `Z`)."""
    return None if moment is None else moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def out(
    job_id: int,
    *,
    updated: datetime,
    seen: datetime | None = None,
    saved: datetime | None = None,
    applied: datetime | None = None,
    hidden: datetime | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """A `UserActionsOut` as the api answers it."""
    return {
        "job_id": job_id,
        "seen_at": z(seen),
        "saved_at": z(saved),
        "applied_at": z(applied),
        "hidden_at": z(hidden),
        "note": note,
        "updated_at": z(updated),
    }


def stored(row: Any) -> dict[str, Any]:
    """A `user_job_actions` row in the answer's form."""
    return {
        "job_id": row.job_id,
        **{name: z(getattr(row, name)) for name in ("seen_at", "saved_at", "applied_at", "hidden_at")},
        "note": row.note,
        "updated_at": z(row.updated_at),
    }


async def put(client: httpx.AsyncClient, job_id: int, body: dict[str, Any]) -> dict[str, Any]:
    r = await client.put(url(job_id), json=body)
    assert r.status_code == 200, (r.status_code, r.text)
    answer: dict[str, Any] = r.json()
    return answer


async def new_job(engine: AsyncEngine) -> int:
    async with engine.begin() as c:
        return (await insert_job(c)).job_id


async def grouped(engine: AsyncEngine, n: int, *, status: str = "active") -> list[int]:
    """`n` jobs in one duplicate group (the first its representative) with their membership rows."""
    async with engine.begin() as c:
        jobs = [await insert_job(c) for _ in range(n)]
        group = await insert_group(c, jobs[0], jobs[1:])
        for job in jobs:
            await insert_row(
                c, "duplicate_group_members", {"group_id": group, "job_id": job.job_id, "evidence": {}}
            )
        if status != "active":
            await c.execute(
                sa.text("UPDATE duplicate_groups SET status = :s WHERE id = :g"), {"s": status, "g": group}
            )
    return [job.job_id for job in jobs]


# --- the values -------------------------------------------------------------------------------------------


async def test_true_keeps_an_existing_timestamp_and_false_clears(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, clock: Clock
) -> None:
    job = await new_job(engine)
    first = await put(logged_in_client, job, {"seen": True, "saved": True, "applied": True, "hidden": True})
    assert first == {
        "job": out(job, seen=T1, saved=T1, applied=T1, hidden=T1, updated=T1),
        "members": [],
    }
    clock.at = T2
    second = await put(logged_in_client, job, {"seen": True, "saved": True, "applied": False})
    # true keeps T1, false clears, the absent `hidden` is left
    assert second["job"] == out(job, seen=T1, saved=T1, hidden=T1, updated=T2)
    assert stored(await user_actions(engine, job)) == second["job"]
    clock.at = T3
    third = await put(logged_in_client, job, {"seen": False, "saved": False, "applied": True, "hidden": None})
    assert third["job"] == out(job, applied=T3, hidden=T1, updated=T3)  # an explicit null leaves too
    assert stored(await user_actions(engine, job)) == third["job"]


async def test_an_empty_note_clears_and_a_note_over_2000_is_422(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, clock: Clock, log_events: Any
) -> None:
    job = await new_job(engine)
    noted = await put(logged_in_client, job, {"note": "Rückruf am Montag."})
    assert noted["job"] == out(job, note="Rückruf am Montag.", updated=T1)
    longest = "🙂" * NOTE_LIMIT  # 2,000 code points, 4,000 UTF-16 units
    assert (await put(logged_in_client, job, {"note": longest}))["job"]["note"] == longest
    for refused in ("x" * (NOTE_LIMIT + 1), "geheim\u0000notiz", "geheim\ud800notiz"):
        r = await logged_in_client.put(url(job), content=json.dumps({"note": refused}), headers=JSON)
        assert (r.status_code, r.json()["code"]) == (422, "validation_failed"), r.text
        assert "geheim" not in r.text and "xxxx" not in r.text  # the text is never echoed
    assert (await user_actions(engine, job)).note == longest  # nothing written
    cleared = await put(logged_in_client, job, {"note": ""})
    assert cleared["job"] == out(job, updated=T1)
    assert (await user_actions(engine, job)).note is None
    events = log_events()
    written = [e for e in events if e.get("event") == "job_actions_written"]
    assert [e["fields"] for e in written] == [
        ["note"],
        ["note"],
        ["note"],
    ]  # the field's name, never its text
    logged = json.dumps(events)
    assert "Rückruf" not in logged and "🙂" not in logged and "geheim" not in logged


async def test_seen_on_open_sets_seen_without_a_bump_and_a_save_bumps_once(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, clock: Clock
) -> None:
    (job, member) = await grouped(engine, 2)
    before = await generation(engine)
    opened = await put(logged_in_client, job, {"seen_on_open": True})
    assert opened == {"job": out(job, seen=T1, updated=T1), "members": []}
    clock.at = T2
    again = await put(logged_in_client, job, {"seen_on_open": True, "apply_to_group": True})
    assert again == {"job": out(job, seen=T1, updated=T2), "members": []}  # kept; no member written
    assert await user_actions(engine, member) is None
    assert await generation(engine) == before  # opening the drawer never bumps
    await put(logged_in_client, job, {"saved": True})
    assert await generation(engine) == before + 1
    await put(logged_in_client, job, {"saved": True})  # unchanged, still a user action
    assert await generation(engine) == before + 2
    await put(logged_in_client, job, {"seen": True})  # an explicit Mark seen bumps
    assert await generation(engine) == before + 3
    await put(logged_in_client, job, {"seen_on_open": True, "hidden": True})
    assert await generation(engine) == before + 4


@pytest.mark.parametrize("seen", [True, False])
async def test_seen_on_open_with_seen_is_422(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, clock: Clock, seen: bool
) -> None:
    job = await new_job(engine)
    before = await generation(engine)
    r = await logged_in_client.put(url(job), json={"seen_on_open": True, "seen": seen})
    assert (r.status_code, r.json()["code"]) == (422, "validation_failed"), r.text
    assert await count_rows(engine, "user_job_actions", job_id=job) == 0
    assert await generation(engine) == before


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"apply_to_group": True},
        {"seen_on_open": False},
        {"saved": None, "note": None, "apply_to_group": True},
        {"saved": True, "starred": True},
        {"saved": "maybe"},
    ],
)
async def test_an_all_empty_or_unknown_body_is_422_and_writes_nothing(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, clock: Clock, body: dict[str, Any]
) -> None:
    job = await new_job(engine)
    before = await generation(engine)
    r = await logged_in_client.put(url(job), json=body)
    assert (r.status_code, r.json()["code"]) == (422, "validation_failed"), r.text
    assert await count_rows(engine, "user_job_actions", job_id=job) == 0
    assert await generation(engine) == before


# --- the group --------------------------------------------------------------------------------------------


async def test_apply_to_group_sets_saved_applied_hidden_on_every_member_not_seen_or_note(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, clock: Clock
) -> None:
    a, b, c = await grouped(engine, 3)
    outsider = await new_job(engine)
    async with engine.begin() as conn:
        await put_user_actions(conn, c, seen_at=T0, saved_at=T0, hidden_at=T0, note="Eigene Notiz.")
    body = {
        "seen": True,
        "saved": True,
        "applied": True,
        "hidden": False,
        "note": "Für die ganze Gruppe?",
        "apply_to_group": True,
    }
    answer = await put(logged_in_client, b, body)
    assert answer == {
        "job": out(b, seen=T1, saved=T1, applied=T1, note="Für die ganze Gruppe?", updated=T1),
        "members": [
            out(a, saved=T1, applied=T1, updated=T1),  # created: no seen, no note
            out(c, seen=T0, saved=T0, applied=T1, note="Eigene Notiz.", updated=T1),  # its own seen and note
        ],
    }
    assert [stored(await user_actions(engine, job)) for job in (a, c)] == answer["members"]
    assert stored(await user_actions(engine, b)) == answer["job"]
    assert await user_actions(engine, outsider) is None
    clock.at = T2
    alone = await put(logged_in_client, b, {"saved": False})  # without apply_to_group: the job alone
    assert alone == {
        "job": out(b, seen=T1, applied=T1, note="Für die ganze Gruppe?", updated=T2),
        "members": [],
    }
    unchanged = stored(await user_actions(engine, a))
    assert unchanged == out(a, saved=T1, applied=T1, updated=T1)
    only_own = await put(logged_in_client, b, {"seen": False, "note": "", "apply_to_group": True})
    assert only_own == {"job": out(b, applied=T1, updated=T2), "members": []}  # seen and note stay per job
    assert stored(await user_actions(engine, a)) == unchanged


@pytest.mark.parametrize("status", ["none", "dissolved"])
async def test_apply_to_group_without_an_active_group_writes_the_job_alone(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, clock: Clock, status: str
) -> None:
    if status == "none":
        job, others = await new_job(engine), [await new_job(engine)]
    else:
        job, *others = await grouped(engine, 2, status=status)
    answer = await put(logged_in_client, job, {"hidden": True, "apply_to_group": True})
    assert answer == {"job": out(job, hidden=T1, updated=T1), "members": []}
    assert [await user_actions(engine, other) for other in others] == [None]


async def test_every_write_sets_updated_at(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, clock: Clock
) -> None:
    a, b = await grouped(engine, 2)
    created = await put(logged_in_client, a, {"hidden": False})  # the first write creates the row
    assert created["job"] == out(a, updated=T1)
    assert (await user_actions(engine, a)).updated_at == T1  # the request's time, not the database's
    clock.at = T2
    assert (await put(logged_in_client, a, {"hidden": False}))["job"] == out(a, updated=T2)  # nothing changed
    clock.at = T3
    opened = await put(logged_in_client, a, {"seen_on_open": True})
    assert opened["job"] == out(a, seen=T3, updated=T3)
    clock.at = T4
    before = await generation(engine)
    both = await put(logged_in_client, a, {"saved": False, "apply_to_group": True})
    assert both == {"job": out(a, seen=T3, updated=T4), "members": [out(b, updated=T4)]}
    assert await generation(engine) == before + 1  # one bump for the write, not one per row
    assert [(await user_actions(engine, job)).updated_at for job in (a, b)] == [T4, T4]


# --- gone jobs and the guards -----------------------------------------------------------------------------


async def wait_at_jobs(server: PgServer, task: "asyncio.Task[Any]") -> LockWait:
    """The one lock wait on a `jobs` row, once the task's statement waits there."""

    def at_jobs() -> list[LockWait]:
        return [w for w in lock_wait_details(server) if w.relation == "jobs"]

    async def waiting() -> bool:
        return task.done() or bool(at_jobs())

    await await_until(waiting, WAIT_S, 0.02)
    assert not task.done(), "the PUT did not wait for the merge"
    [wait] = at_jobs()
    return wait


async def test_actions_racing_a_merge_write_no_row_for_the_merged_job(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, migrated: PgServer, clock: Clock
) -> None:
    async with engine.begin() as c:
        into, merged = (await insert_job(c)).job_id, (await insert_job(c)).job_id
    async with engine.connect() as merging:
        await merging.begin()
        await merge.merge_jobs(  # both job rows FOR UPDATE, held until the commit below
            merging,
            from_job_id=merged,
            into_job_id=into,
            kind="user_confirmed_merge",
            evidence={},
            recompute=False,
        )
        putting = asyncio.create_task(
            logged_in_client.put(url(merged), json={"saved": True, "note": "Zu spät."})
        )
        wait = await wait_at_jobs(migrated, putting)
        assert wait.key == str(merged)  # the PUT waits at the job row, holding nothing the merge needs
        assert "jobs_actions.lock" in wait.query  # in its explicit lock, not the upsert's key check
        await ranking.bump(merging)
        await merging.commit()
    r = await asyncio.wait_for(putting, WAIT_S)
    assert (r.status_code, r.json()["code"], r.json()["merged_into_job_id"]) == (404, "not_found", into)
    assert await count_rows(engine, "user_job_actions", job_id=merged) == 0
    assert await count_rows(engine, "user_job_actions", job_id=into) == 0


async def test_a_member_that_leaves_the_group_while_the_put_waits_is_not_written(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, migrated: PgServer, clock: Clock
) -> None:
    a, b, c = await grouped(engine, 3)
    async with engine.connect() as leaving:
        await leaving.begin()
        await leaving.execute(sa.text("SELECT 1 FROM jobs WHERE id = :j FOR UPDATE"), {"j": b})
        await leaving.execute(sa.text("DELETE FROM duplicate_group_members WHERE job_id = :j"), {"j": b})
        await leaving.execute(sa.text("UPDATE jobs SET duplicate_group_id = NULL WHERE id = :j"), {"j": b})
        # the PUT's first read still sees b as a member: it locks a, then waits at b
        putting = asyncio.create_task(
            logged_in_client.put(url(a), json={"saved": True, "apply_to_group": True})
        )
        wait = await wait_at_jobs(migrated, putting)
        assert wait.key == str(b) and "jobs_actions.lock" in wait.query
        await leaving.commit()
    r = await asyncio.wait_for(putting, WAIT_S)
    assert r.status_code == 200, r.text
    # re-read under the lock: b left, so only c is written
    assert r.json() == {"job": out(a, saved=T1, updated=T1), "members": [out(c, saved=T1, updated=T1)]}
    assert await user_actions(engine, b) is None


async def test_unknown_job_404_and_csrf_403(
    logged_in_client: httpx.AsyncClient, api_client: httpx.AsyncClient, engine: AsyncEngine, clock: Clock
) -> None:
    async with engine.begin() as c:
        survivor = (await insert_job(c)).job_id
        merged = (await insert_job(c, merged_into_job_id=survivor)).job_id
        synthetic = (await insert_job(c, synthetic_eval=True)).job_id
        job = (await insert_job(c)).job_id
    before = await generation(engine)
    for gone, into in ((10**12, None), (synthetic, None), (merged, survivor)):
        r = await logged_in_client.put(url(gone), json={"saved": True})
        problem = r.json()
        assert (r.status_code, problem["code"], problem["merged_into_job_id"]) == (404, "not_found", into), (
            gone
        )
    r = await logged_in_client.put(url(0), json={"saved": True})
    assert (r.status_code, r.json()["code"]) == (422, "validation_failed")
    r = await api_client.put(url(job), json={"saved": True}, headers={"X-JH-Request": "1"})
    assert (r.status_code, r.json()["code"]) == (401, "unauthenticated")
    r = await logged_in_client.put(url(job), json={"saved": True}, headers={"X-JH-Request": "0"})
    assert (r.status_code, r.json()["code"]) == (403, "csrf_failed")
    assert await count_rows(engine, "user_job_actions") == 0
    assert await generation(engine) == before
