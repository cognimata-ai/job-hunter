"""The duplicate-group routes over HTTP (spec §14.2 L4998-5001, §8.7, §7.1 L1255; plan M4 Task 8, B12).

- `POST /api/duplicate-groups/{group_id}/split {job_id}` → 200 `Group` after the split: the member leaves,
  `dedup_decisions(different, user_split)` against every other member, its target `deferred_duplicate →
  pending`; a group left with one member is `dissolved` and still answered. An unknown group is 404, a group
  that is not active or a job that is not its member 409, and nothing is written.
- `POST /api/duplicate-groups/{group_id}/score-individually {job_id}` → 200 `Group`: the membership's flag,
  the member's deferred target `pending` with `override = 'individual'` at the request's time; the flag of a
  member whose target is not deferred too. A refusal is the score-now problem of its code.
- `POST /api/duplicate-groups/{group_id}/confirm` → 202 `{job_id}`: every other member merged into the
  representative, the group dissolved by the last merge.

Each write bumps the ranking generation exactly once. The clock is injected
(`jobhunter.api.routes.groups.utcnow`), far from the database's own. Every datum is invented.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Final, get_args

import httpx
import pytest
import sqlalchemy as sa
from jobhunter.api.routes import groups as groups_route
from jobhunter.clock import utcnow
from jobhunter.dedup import groups
from jobhunter.dedup.groups import GroupEffect
from jobhunter.scoring import targets
from jobhunter.scoring.targets import ScoreNowCode, ScoreNowRefused
from jobhunter.worker.epoch import fence_epoch
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.collect import JobWithPostings
from tests.support.groups import FULL_TEXT, canonical_job
from tests.support.jobs_list import Card, seed_cards
from tests.support.settings import generation

pytestmark = pytest.mark.integration

AT: Final = datetime(2026, 2, 3, 10, 30, tzinfo=UTC)  # the route's clock

TARGET: Final = sa.text(
    "SELECT state, override, override_at, deferred_group_id FROM score_targets WHERE job_id = :j"
)
DECISIONS: Final = sa.text("SELECT job_a, job_b, decision, source FROM dedup_decisions ORDER BY job_a, job_b")
EVENTS: Final = sa.text("SELECT kind, from_job_id, into_job_id FROM merge_events ORDER BY id")
FLAG: Final = sa.text("SELECT score_individually FROM duplicate_group_members WHERE job_id = :j")


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(groups_route, "utcnow", lambda: AT)


async def rows(engine: AsyncEngine, statement: sa.TextClause, **params: Any) -> list[Any]:
    async with engine.connect() as c:
        return list((await c.execute(statement, params)).all())


async def scalar(engine: AsyncEngine, statement: str, **params: Any) -> Any:
    async with engine.connect() as c:
        return (await c.execute(sa.text(statement), params)).scalar_one()


async def grouped(engine: AsyncEngine, deferred: int) -> tuple[int, list[int], int]:
    """A representative card (its target `pending`) and `deferred` members of its active group (their
    targets `deferred_duplicate`): the representative, the members and the group."""
    rep, *members = await seed_cards(
        engine, [Card(state="pending"), *[Card(deferred_to=0) for _ in range(deferred)]]
    )
    group = int(await scalar(engine, "SELECT duplicate_group_id FROM jobs WHERE id = :j", j=rep))
    return rep, members, group


def member(job_id: int, state: str | None, *, rep: int, individually: bool = False) -> dict[str, Any]:
    """A `GroupMemberOut` of a seeded card as the api answers it."""
    return {
        "job_id": job_id,
        "title": "Testrolle",
        "company": "musterfirma",
        "source_ids": ["jsearch"],
        "locations": [],
        "completeness": "full",
        "availability": "active",
        "target_state": state,
        "score_individually": individually,
        "is_representative": job_id == rep,
    }


def group_out(
    group: int, *, status: str, rep: int | None, election: str, members: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "id": group,
        "status": status,
        "representative_job_id": rep,
        "unscorable_reason": None,
        "election_reason": election,
        "members": members,
    }


def split_url(group: int) -> str:
    return f"/api/duplicate-groups/{group}/split"


def individually_url(group: int) -> str:
    return f"/api/duplicate-groups/{group}/score-individually"


def status_code(r: httpx.Response) -> tuple[int, str]:
    return r.status_code, r.json()["code"]


# --- split ------------------------------------------------------------------------------------------------


async def test_split_returns_the_group_and_the_member_becomes_pending(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, active_tuple: int
) -> None:
    rep, (a, b), group = await grouped(engine, 2)
    (outsider,) = await seed_cards(engine, [Card(state="pending")])
    before = await generation(engine)
    # refused, writing nothing: no such group, and a job that is no member of this one
    r = await logged_in_client.post(split_url(10**12), json={"job_id": a})
    assert status_code(r) == (404, "not_found")
    r = await logged_in_client.post(split_url(group), json={"job_id": outsider})
    assert status_code(r) == (409, "conflict_state") and "retryable" not in r.json()
    assert await generation(engine) == before

    r = await logged_in_client.post(split_url(group), json={"job_id": a})
    assert r.status_code == 200, r.text
    assert r.json() == group_out(
        group,
        status="active",
        rep=rep,
        election="initial",
        members=[member(rep, "pending", rep=rep), member(b, "deferred_duplicate", rep=rep)],
    )
    [split] = await rows(engine, TARGET, j=a)
    assert (split.state, split.override, split.deferred_group_id) == ("pending", "none", None)
    assert (await rows(engine, TARGET, j=b))[0].state == "deferred_duplicate"  # still deferred to rep
    assert await scalar(engine, "SELECT duplicate_group_id FROM jobs WHERE id = :j", j=a) is None
    assert await scalar(engine, "SELECT count(*) FROM duplicate_group_members WHERE job_id = :j", j=a) == 0
    assert [tuple(d) for d in await rows(engine, DECISIONS)] == sorted(
        [(min(a, other), max(a, other), "different", "user_split") for other in (rep, b)]
    )
    assert await generation(engine) == before + 1


async def test_split_of_the_second_last_member_dissolves_the_group(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, active_tuple: int
) -> None:
    rep, (a,), group = await grouped(engine, 1)
    before = await generation(engine)
    r = await logged_in_client.post(split_url(group), json={"job_id": a})
    assert r.status_code == 200, r.text
    # the dissolved group is answered: no representative, no member left
    assert r.json() == group_out(group, status="dissolved", rep=None, election="split", members=[])
    assert [(await rows(engine, TARGET, j=job))[0].state for job in (rep, a)] == ["pending", "pending"]
    assert await scalar(engine, "SELECT count(*) FROM duplicate_group_members") == 0
    assert await generation(engine) == before + 1
    # a dissolved group is no longer split
    r = await logged_in_client.post(split_url(group), json={"job_id": rep})
    assert status_code(r) == (409, "conflict_state")
    assert await generation(engine) == before + 1


# --- score individually -----------------------------------------------------------------------------------


async def test_score_individually_sets_the_flag_and_override(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, active_tuple: int, clock: None
) -> None:
    rep, (a, b), group = await grouped(engine, 2)
    before = await generation(engine)
    r = await logged_in_client.post(individually_url(group), json={"job_id": a})
    assert r.status_code == 200, r.text
    assert r.json() == group_out(
        group,
        status="active",
        rep=rep,
        election="initial",
        members=[
            member(rep, "pending", rep=rep),
            member(a, "pending", rep=rep, individually=True),  # still a member, scored on its own
            member(b, "deferred_duplicate", rep=rep),
        ],
    )
    [moved] = await rows(engine, TARGET, j=a)
    assert (moved.state, moved.override, moved.override_at, moved.deferred_group_id) == (
        "pending",
        "individual",
        AT,
        None,
    )
    assert await generation(engine) == before + 1
    # a member whose target is not deferred: the flag alone (S:L3271), one bump
    r = await logged_in_client.post(individually_url(group), json={"job_id": rep})
    assert r.status_code == 200, r.text
    assert r.json()["members"][0] == member(rep, "pending", rep=rep, individually=True)
    [kept] = await rows(engine, TARGET, j=rep)
    assert (kept.state, kept.override, kept.override_at) == ("pending", "none", None)
    assert [(await rows(engine, FLAG, j=j))[0].score_individually for j in (rep, a, b)] == [True, True, False]
    assert await generation(engine) == before + 2


REFUSAL_MEMBERS: Final = ("status", "code", "title", "detail")


def refusal_of(r: httpx.Response) -> dict[str, Any]:
    """The members of a refusal's problem that do not depend on the request."""
    body: dict[str, Any] = r.json()
    return {k: body.get(k) for k in REFUSAL_MEMBERS}


@pytest.mark.parametrize("code", get_args(ScoreNowCode))
async def test_score_individually_refusal_maps_like_score_now(
    logged_in_client: httpx.AsyncClient,
    engine: AsyncEngine,
    active_tuple: int,
    monkeypatch: pytest.MonkeyPatch,
    code: ScoreNowCode,
) -> None:
    _, (a,), group = await grouped(engine, 1)
    (outsider,) = await seed_cards(engine, [Card(state="pending")])
    before = await generation(engine)
    # the domain's own refusal: a job that is no member of the group
    r = await logged_in_client.post(individually_url(group), json={"job_id": outsider})
    assert refusal_of(r) == {
        "status": 404,
        "code": "not_found",
        "title": "Group member not found",
        "detail": None,
    }

    async def refusing(conn: AsyncConnection, **_: Any) -> ScoreNowRefused:
        return ScoreNowRefused(code)

    # each refusal through both routes: score-now's answer is the one score-individually gives
    monkeypatch.setattr(targets, "score_now", refusing)
    monkeypatch.setattr(targets, "score_individually", refusing)
    key = {"Idempotency-Key": f"score-now-{code}".replace("_", "-").ljust(16, "x")}
    score_now = refusal_of(await logged_in_client.post(f"/api/jobs/{a}/score-now", json={}, headers=key))
    individually = refusal_of(await logged_in_client.post(individually_url(group), json={"job_id": a}))
    assert individually["status"] == (404 if code == "not_found" else 409)
    if code == "not_found":  # the title names what is missing: the job's target, the group's member
        assert (score_now.pop("title"), individually.pop("title")) == (
            "Job target not found",
            "Group member not found",
        )
    assert individually == score_now
    assert await generation(engine) == before
    assert (await rows(engine, TARGET, j=a))[0].state == "deferred_duplicate"


# --- confirm ----------------------------------------------------------------------------------------------


async def epoch_1(conn: AsyncConnection) -> None:
    await fence_epoch(conn, 1)


async def regroup(engine: AsyncEngine, job_id: int) -> GroupEffect:
    async with engine.begin() as conn:
        return await groups.regroup_job(conn, job_id, guard=epoch_1)


def jobs_maker(engine: AsyncEngine, job_with_postings: JobWithPostings) -> Callable[..., Any]:
    """`await make(source_id, **normalized overrides)`: a canonical job, each an hour after the last."""
    at = utcnow() - timedelta(days=2)
    made: list[int] = []

    async def make(source_id: str, **overrides: Any) -> int:
        job_id = await canonical_job(
            engine, job_with_postings, source_id, observed_at=at + timedelta(hours=len(made)), **overrides
        )
        made.append(job_id)
        return job_id

    return make


async def test_confirm_merges_the_members_into_the_representative(
    logged_in_client: httpx.AsyncClient,
    engine: AsyncEngine,
    current_epoch: int,
    job_with_postings: JobWithPostings,
) -> None:
    make = jobs_maker(engine, job_with_postings)
    x, y, z = [await make("jsearch", description_text=FULL_TEXT) for _ in "xyz"]
    group = (await regroup(engine, x)).group_id
    assert group is not None and (await regroup(engine, z)).joined
    assert (
        await scalar(engine, "SELECT representative_job_id FROM duplicate_groups WHERE id = :g", g=group) == x
    )
    before = await generation(engine)
    r = await logged_in_client.post("/api/duplicate-groups/10000000000/confirm")
    assert status_code(r) == (404, "not_found")

    r = await logged_in_client.post(f"/api/duplicate-groups/{group}/confirm")
    assert (r.status_code, r.json()) == (202, {"job_id": x})
    events = await rows(engine, EVENTS)
    assert [tuple(e) for e in events] == [("user_confirmed_merge", y, x), ("user_confirmed_merge", z, x)]
    merged = sa.text("SELECT id, merged_into_job_id FROM jobs WHERE id = ANY(:ids) ORDER BY id")
    assert [tuple(j) for j in await rows(engine, merged, ids=[x, y, z])] == [(x, None), (y, x), (z, x)]
    assert [tuple(d) for d in await rows(engine, DECISIONS)] == [
        (x, y, "same", "user_confirm"),
        (x, z, "same", "user_confirm"),
    ]
    assert await scalar(engine, "SELECT status FROM duplicate_groups WHERE id = :g", g=group) == "dissolved"
    assert await generation(engine) == before + 1
    # a dissolved group is confirmed no more
    r = await logged_in_client.post(f"/api/duplicate-groups/{group}/confirm")
    assert status_code(r) == (409, "conflict_state")
    assert await generation(engine) == before + 1
