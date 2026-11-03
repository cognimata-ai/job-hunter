"""The merge family over HTTP (spec §14.2 L4997, L5043, §8.6.2, §8.6 step 3; plan M4 Task 8, B12, T22-a, M1
Carry-forward 10).

- `POST /api/jobs/{job_id}/unmerge {merge_event_id}` → 202 `{unmerge_event_id}`: the merge reversed. An
  event that does not exist or merged into another job is 404 (no `merged_into_job_id`: the event is what is
  missing); an unmerge event, or a merge already reversed, reaches the unmerge and is 409.
- `POST /api/identity-conflicts/{conflict_id}/resolve {decision}` → 202 `{conflict_id, status}`: the
  conflict's status after its resolution; a conflict resolved already is 409, an unknown one 404.
- A deadlock (40P01) is tried again in a fresh transaction, three tries in all, each retry logged
  `api_deadlock_retried` with its attempt and SQLSTATE only; the third failure is 409 `conflict_state` with
  `retryable: true`, and nothing is written.
- A job set that changed under the lock (a member that joined a group while the request waited for its
  rows) is 409 `conflict_state` with `retryable: true`, tried once, and nothing is written.

Every datum is invented.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any, Final

import httpx
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.dedup import merge
from jobhunter.normalize.keys import AdKey
from jobhunter.worker.runs import RunOwner
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.collect import (
    STEPSTONE_1,
    STEPSTONE_2,
    KeyPair,
    key_pair,
    pending_item,
    queued_run,
    resolved,
)
from tests.support.m3 import insert_id, insert_job, insert_row
from tests.support.pg import PgServer, lock_wait_details
from tests.support.procs import await_until
from tests.support.settings import generation

pytestmark = pytest.mark.integration

WAIT_S: Final = 30.0
EVENTS: Final = sa.text(
    "SELECT id, kind, from_job_id, into_job_id, posting_ids, reversed_by_event_id FROM merge_events "
    "ORDER BY id"
)
CONFLICTS: Final = sa.text(
    "SELECT id, status, posting_job_id, candidate_job_ids FROM identity_conflicts ORDER BY id"
)
IN_GROUP: Final = sa.text("UPDATE jobs SET duplicate_group_id = :g WHERE id = :j")
_DEADLOCK: Final = sa.text("DO $$ BEGIN RAISE EXCEPTION 'injected' USING ERRCODE = '40P01'; END $$")


async def rows(engine: AsyncEngine, statement: sa.TextClause, **params: Any) -> list[Any]:
    async with engine.connect() as c:
        return list((await c.execute(statement, params)).all())


async def scalar(engine: AsyncEngine, statement: str, **params: Any) -> Any:
    async with engine.connect() as c:
        return (await c.execute(sa.text(statement), params)).scalar_one()


def unmerge_url(job_id: int) -> str:
    return f"/api/jobs/{job_id}/unmerge"


def resolve_url(conflict_id: int) -> str:
    return f"/api/identity-conflicts/{conflict_id}/resolve"


def status_code(r: httpx.Response) -> tuple[int, str]:
    return r.status_code, r.json()["code"]


async def merged_pair(engine: AsyncEngine) -> tuple[KeyPair, int]:
    """Jobs A and B (`key_pair`) and the event of B's strong-key merge into A (a sighting with both keys)."""
    owner = await queued_run(engine)
    at = utcnow() - timedelta(days=1)
    pair = await key_pair(engine, owner, at=at)
    merging = await pending_item(
        engine,
        owner,
        "adzuna",
        {"native_id": "9", "primary_link": STEPSTONE_1},
        fetched_at=at + timedelta(hours=2),
    )
    assert await resolved(engine, owner, merging) == "linked"
    [event] = await rows(engine, EVENTS)
    assert (event.kind, event.from_job_id, event.into_job_id) == ("strong_key_merge", pair.b, pair.a)
    return pair, int(event.id)


async def merged_into(engine: AsyncEngine, job_id: int) -> int | None:
    value = await scalar(engine, "SELECT merged_into_job_id FROM jobs WHERE id = :j", j=job_id)
    return None if value is None else int(value)


# --- unmerge ----------------------------------------------------------------------------------------------


async def test_unmerge_of_an_event_into_another_job_is_404(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, current_epoch: int
) -> None:
    pair, event = await merged_pair(engine)
    before = await generation(engine)
    for job_id, event_id in ((pair.b, event), (pair.a, 10**12)):  # into A, not B; no such event
        r = await logged_in_client.post(unmerge_url(job_id), json={"merge_event_id": event_id})
        assert status_code(r) == (404, "not_found"), r.text
        assert r.json()["title"] == "Merge event not found"  # the route's own check, not the router's
        assert "merged_into_job_id" not in r.json()  # the event is what is missing, not the job
    [unchanged] = await rows(engine, EVENTS)
    assert unchanged.reversed_by_event_id is None and await merged_into(engine, pair.b) == pair.a
    assert await generation(engine) == before


async def test_unmerge_returns_the_unmerge_event_and_restores_the_job(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, current_epoch: int
) -> None:
    pair, event = await merged_pair(engine)
    before = await generation(engine)
    r = await logged_in_client.post(unmerge_url(pair.a), json={"merge_event_id": event})
    assert r.status_code == 202, r.text
    original, reversal = await rows(engine, EVENTS)
    assert r.json() == {"unmerge_event_id": reversal.id}
    assert original.reversed_by_event_id == reversal.id
    assert (reversal.kind, reversal.from_job_id, reversal.into_job_id, reversal.posting_ids) == (
        "unmerge",
        pair.b,
        pair.a,
        [pair.posting_b],
    )
    assert await merged_into(engine, pair.b) is None
    assert await scalar(engine, "SELECT job_id FROM postings WHERE id = :p", p=pair.posting_b) == pair.b
    decisions = sa.text("SELECT job_a, job_b, decision, source FROM dedup_decisions")
    assert [tuple(d) for d in await rows(engine, decisions)] == [(pair.a, pair.b, "different", "unmerge")]
    assert await generation(engine) == before + 1


async def test_unmerging_an_unmerge_event_is_409(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, current_epoch: int
) -> None:
    pair, event = await merged_pair(engine)
    async with engine.begin() as c:
        reversal = await merge.unmerge(c, event)
    assert await scalar(engine, "SELECT into_job_id FROM merge_events WHERE id = :e", e=reversal) == pair.a
    before = await generation(engine)
    # the event names the job (the route's check passes); the unmerge refuses an unmerge event
    r = await logged_in_client.post(unmerge_url(pair.a), json={"merge_event_id": reversal})
    assert status_code(r) == (409, "conflict_state") and "retryable" not in r.json()
    assert len(await rows(engine, EVENTS)) == 2 and await generation(engine) == before


async def test_unmerging_a_reversed_merge_is_409(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, current_epoch: int
) -> None:
    pair, event = await merged_pair(engine)
    r = await logged_in_client.post(unmerge_url(pair.a), json={"merge_event_id": event})
    assert r.status_code == 202, r.text
    before = await generation(engine)
    r = await logged_in_client.post(unmerge_url(pair.a), json={"merge_event_id": event})
    assert status_code(r) == (409, "conflict_state") and "retryable" not in r.json()
    assert len(await rows(engine, EVENTS)) == 2 and await merged_into(engine, pair.b) is None
    assert await generation(engine) == before


# --- conflict resolution ----------------------------------------------------------------------------------


async def first_conflict(engine: AsyncEngine) -> tuple[RunOwner, datetime, KeyPair, int, int]:
    """A conflict of a new SerpApi posting (htidocid H of A, stepstone 2 against A's stepstone 1): the run's
    owner, the time of A's sighting, A and B, the conflict and the posting's job X, its candidate being A."""
    owner = await queued_run(engine)
    at = utcnow() - timedelta(days=1)
    pair = await key_pair(engine, owner, at=at)
    serp = await pending_item(
        engine,
        owner,
        "serpapi",
        {"native_id": "H", "primary_link": STEPSTONE_2},
        fetched_at=at + timedelta(hours=2),
    )
    assert await resolved(engine, owner, serp) == "linked"
    [conflict] = await rows(engine, CONFLICTS)
    assert (conflict.status, conflict.candidate_job_ids) == ("open", [pair.a])
    return owner, at, pair, int(conflict.id), int(conflict.posting_job_id)


async def test_resolve_separate_and_merge_and_a_second_resolve_is_409(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, current_epoch: int
) -> None:
    owner, at, pair, first, x = await first_conflict(engine)
    before = await generation(engine)
    r = await logged_in_client.post(resolve_url(10**12), json={"decision": "separate"})
    assert status_code(r) == (404, "not_found")
    r = await logged_in_client.post(resolve_url(first), json={"decision": "both"})
    assert status_code(r) == (422, "validation_failed")

    r = await logged_in_client.post(resolve_url(first), json={"decision": "separate"})
    assert (r.status_code, r.json()) == (202, {"conflict_id": first, "status": "resolved_separate"})
    assert (await rows(engine, CONFLICTS))[0].status == "resolved_separate"
    assert await generation(engine) == before  # the list did not change

    # a second conflict (htidocid H with stepstone 3), resolved as a merge of A into the posting's job Y
    third = await pending_item(
        engine,
        owner,
        "arbeitnow",
        {"raw_keys": (AdKey("htidocid", "H"), AdKey("pub:stepstone.de", "3"))},
        fetched_at=at + timedelta(hours=4),
    )
    assert await resolved(engine, owner, third) == "linked"
    second = (await rows(engine, CONFLICTS))[1]
    y = int(second.posting_job_id)
    assert y != x and second.candidate_job_ids == [pair.a]
    before = await generation(engine)
    r = await logged_in_client.post(resolve_url(second.id), json={"decision": "merge"})
    assert (r.status_code, r.json()) == (202, {"conflict_id": second.id, "status": "resolved_merged"})
    assert await merged_into(engine, pair.a) == y
    assert await generation(engine) == before + 1

    for conflict in (first, second.id):  # a resolved conflict stays resolved
        r = await logged_in_client.post(resolve_url(conflict), json={"decision": "merge"})
        assert status_code(r) == (409, "conflict_state") and "retryable" not in r.json()
    assert [c.status for c in await rows(engine, CONFLICTS)] == ["resolved_separate", "resolved_merged"]
    assert await generation(engine) == before + 1


# --- deadlocks and changed job sets -----------------------------------------------------------------------


async def test_a_deadlock_is_retried_and_the_third_failure_is_a_retryable_409(
    logged_in_client: httpx.AsyncClient,
    engine: AsyncEngine,
    current_epoch: int,
    monkeypatch: pytest.MonkeyPatch,
    log_events: Callable[[], list[dict[str, Any]]],
) -> None:
    pair, event = await merged_pair(engine)
    real_unmerge = merge.unmerge
    calls: list[int] = []
    failing = {"times": 0}

    async def deadlocking(conn: AsyncConnection, merge_event_id: int) -> int:
        """The unmerge, whose first `failing["times"]` calls the server aborts as a deadlock (a real 40P01,
        which rolls the transaction back)."""
        calls.append(merge_event_id)
        if len(calls) <= failing["times"]:
            await conn.execute(_DEADLOCK)
        return await real_unmerge(conn, merge_event_id)

    monkeypatch.setattr(merge, "unmerge", deadlocking)
    before = await generation(engine)

    failing["times"] = 3
    r = await logged_in_client.post(unmerge_url(pair.a), json={"merge_event_id": event})
    assert status_code(r) == (409, "conflict_state") and r.json()["retryable"] is True
    assert calls == [event] * 3  # three tries in all
    [unchanged] = await rows(engine, EVENTS)
    assert unchanged.reversed_by_event_id is None and await generation(engine) == before

    calls.clear()
    failing["times"] = 2
    r = await logged_in_client.post(unmerge_url(pair.a), json={"merge_event_id": event})
    assert r.status_code == 202, r.text
    assert calls == [event] * 3  # the third try, in a fresh transaction, unmerged once
    original, reversal = await rows(engine, EVENTS)
    assert r.json() == {"unmerge_event_id": reversal.id} and original.reversed_by_event_id == reversal.id
    assert await generation(engine) == before + 1

    retried = [e for e in log_events() if e.get("event") == "api_deadlock_retried"]
    assert [(e["attempt"], e["sqlstate"]) for e in retried] == [(1, "40P01"), (2, "40P01")] * 2
    standard = {"event", "level", "logger", "ts", "request_id"}  # the chain's own and the edge's
    assert all(set(e) - standard == {"attempt", "sqlstate"} for e in retried), retried


async def wait_at_jobs(server: PgServer, task: "asyncio.Task[Any]") -> None:
    """Until the task's statement waits for a `jobs` row."""

    async def waiting() -> bool:
        return task.done() or any(w.relation == "jobs" for w in lock_wait_details(server))

    await await_until(waiting, WAIT_S, 0.02)
    assert not task.done(), task.result()


GroupSetup = Callable[[AsyncEngine], Awaitable[tuple[str, int, int, dict[str, Any] | None]]]


async def a_group(c: AsyncConnection, members: list[int]) -> int:
    """An active group of `members` (the first represents it), with their membership rows."""
    values = {"representative_job_id": members[0], "status": "active", "election_reason": "initial"}
    group = await insert_id(c, "duplicate_groups", values)
    for job_id in members:
        await join(c, group, job_id)
    return group


async def join(c: AsyncConnection, group: int, job_id: int) -> None:
    """The job's membership row and `duplicate_group_id`, as a regroup writes them."""
    await insert_row(c, "duplicate_group_members", {"group_id": group, "job_id": job_id, "evidence": {}})
    await c.execute(IN_GROUP, {"g": group, "j": job_id})


async def confirm_setup(engine: AsyncEngine) -> tuple[str, int, int, dict[str, Any] | None]:
    """A group of two to confirm: its url, the group, the member whose row the joining transaction holds, and
    no body."""
    async with engine.begin() as c:
        rep, other = (await insert_job(c)).job_id, (await insert_job(c)).job_id
        group = await a_group(c, [rep, other])
    return f"/api/duplicate-groups/{group}/confirm", group, rep, None


async def resolve_setup(engine: AsyncEngine) -> tuple[str, int, int, dict[str, Any] | None]:
    """A conflict whose candidate A is grouped with another job: its url, A's group, that other job (whose row
    the joining transaction holds) and the merge's body."""
    _, _, pair, conflict, _ = await first_conflict(engine)
    async with engine.begin() as c:
        other = (await insert_job(c)).job_id
        group = await a_group(c, [pair.a, other])
    return resolve_url(conflict), group, other, {"decision": "merge"}


@pytest.mark.parametrize("setup", [confirm_setup, resolve_setup], ids=["confirm", "resolve"])
async def test_a_co_member_change_is_a_retryable_409(
    logged_in_client: httpx.AsyncClient,
    engine: AsyncEngine,
    migrated: PgServer,
    current_epoch: int,
    log_events: Callable[[], list[dict[str, Any]]],
    setup: GroupSetup,
) -> None:
    url, group, held, body = await setup(engine)
    async with engine.begin() as c:
        joining = (await insert_job(c)).job_id
    before = await generation(engine)
    events = len(await rows(engine, EVENTS))
    async with engine.connect() as joiner:
        # a regroup's transaction: it holds a member's row and adds a member to the group
        transaction = await joiner.begin()
        await joiner.execute(sa.text("SELECT id FROM jobs WHERE id = :j FOR UPDATE"), {"j": held})
        await join(joiner, group, joining)
        request = asyncio.create_task(logged_in_client.post(url, json=body))
        await wait_at_jobs(migrated, request)  # the request locks the members it read, and waits
        await transaction.commit()
    r = await asyncio.wait_for(request, WAIT_S)
    # the member that joined meanwhile is not among the rows the request holds: it gives up, writing nothing
    assert status_code(r) == (409, "conflict_state") and r.json()["retryable"] is True, r.text
    assert not [e for e in log_events() if str(e.get("event")).startswith("api_deadlock")]  # tried once
    assert len(await rows(engine, EVENTS)) == events and await generation(engine) == before
    assert await scalar(engine, "SELECT status FROM duplicate_groups WHERE id = :g", g=group) == "active"
    assert await scalar(engine, "SELECT count(*) FROM jobs WHERE merged_into_job_id IS NOT NULL") == 0
