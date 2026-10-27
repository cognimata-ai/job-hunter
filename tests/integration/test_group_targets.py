"""Duplicate groups with score targets (spec §8.7 L3251-3267, §7.17 #4; plan M3 Task 15, M1 Deviation 9, Codex
r1 #36, r3 B14, A1 issue 27).

- **Eligibility**: with an active tuple a member is eligible only while its active-tuple target is `pending`,
  `budget_deferred`, `in_progress`, `in_batch`, `deferred_duplicate` or published (`ELIGIBLE_SQL` and
  `representative.eligible` agree); without one M1's rule holds. A group with no eligible member records
  `all_failed`, `all_filtered` or `no_active_member`, and becomes scorable again (`eligible_again`) once a
  member is eligible.
- **Deferral**: `groups.deferrable` (entering deferral: not the representative, `override = 'none'`, not
  `score_individually`, no conflicting fact, the target `pending` or `budget_deferred`) and the persistent
  `groups.deferral_eligible` (staying deferred) agree with the regroup sweep's SQL.
- **Group events**: a split member's deferred target returns to `pending`; a representative change keeps a
  deferred member deferred while `deferral_eligible` holds against the new representative and returns it to
  `pending` otherwise; a full-text twin is elected and is an always-include candidate.

Groups of canonical jobs (`tests.support.groups.canonical_job`) pass G1-G5 with one another; the others are
`tests.support.m3` jobs with membership rows written directly. Every datum is invented.
"""

from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import timedelta
from typing import Any, Final
from uuid import uuid4

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.dedup import groups, representative
from jobhunter.dedup.groups import MemberRow, TargetRow, deferrable, deferral_eligible
from jobhunter.dedup.representative import ELIGIBLE_SQL
from jobhunter.scoring import admission, targets
from jobhunter.worker import reconcilers as r
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.collect import ctx
from tests.support.groups import FULL_TEXT, canonical_job, varied
from tests.support.m3 import (
    NOW,
    Job,
    active_ids,
    insert_bulk_operation,
    insert_job,
    insert_published,
    insert_submission,
)
from tests.support.scoring import (
    activate_score_tuple,
    member_group,
    score_llm_tuple,
    state_target,
    swept_of,
)

pytestmark = pytest.mark.integration

PARTIAL: Final = varied(30) + "…"  # 26 shingles, every one of them in FULL_TEXT's 56
_GROUP: Final = sa.text(
    "SELECT status, representative_job_id, unscorable_reason, election_reason FROM duplicate_groups "
    "WHERE id = :g"
)
_CURRENT_REVISION: Final = sa.text("SELECT current_revision_id FROM jobs WHERE id = :j")
_FLAG: Final = sa.text("UPDATE duplicate_group_members SET score_individually = true WHERE job_id = :j")
_LOCK_JOBS: Final = sa.text("SELECT id FROM jobs WHERE id = ANY(:ids) ORDER BY id FOR UPDATE")
_ELIGIBLE: Final = sa.text(
    "SELECT j.id FROM jobs j JOIN active_config ac ON ac.id = 1 "  # noqa: S608 (the module's constant)
    f"WHERE j.id = ANY(:ids) AND {ELIGIBLE_SQL} ORDER BY j.id"
)
# A target's attempt fails: claimed (`pending → in_progress`), then `failed_request` (its claim cleared).
_FAIL: Final = (
    sa.text(
        "UPDATE score_targets SET state = 'in_progress', claim_id = gen_random_uuid(), claim_epoch = 1, "
        "claimed_at = now(), claim_heartbeat_at = now() WHERE id = :t"
    ),
    sa.text(
        "UPDATE score_targets SET state = 'failed_request', state_reason = 'http_400', claim_id = NULL, "
        "claim_epoch = NULL, claim_heartbeat_at = NULL WHERE id = :t"
    ),
)


async def epoch_1(conn: AsyncConnection) -> None:
    """A guard that proves nothing: the test's own regroup."""


async def regroup(engine: AsyncEngine, job_id: int) -> groups.GroupEffect:
    async with engine.begin() as conn:
        return await groups.regroup_job(conn, job_id, guard=epoch_1)


async def group_row(engine: AsyncEngine, group_id: int) -> tuple[Any, ...]:
    async with engine.connect() as conn:
        return tuple((await conn.execute(_GROUP, {"g": group_id})).one())


async def job_ref(conn: AsyncConnection, job_id: int) -> Job:
    return Job(job_id, int((await conn.execute(_CURRENT_REVISION, {"j": job_id})).scalar_one()))


def jobs_maker(engine: AsyncEngine, job_with_postings: Any) -> Callable[..., Any]:
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


async def reelect(engine: AsyncEngine, group_id: int, member_ids: Sequence[int]) -> int | None:
    """`representative.reelect` in a transaction that holds the members' rows first (its precondition)."""
    async with engine.begin() as conn:
        await conn.execute(_LOCK_JOBS, {"ids": sorted(member_ids)})
        return await representative.reelect(conn, group_id, reason=groups.RECONCILER_REPAIR)


async def repair(engine: AsyncEngine, queue_app: Any) -> int:
    """One pass of the `group_representatives` reconciler."""
    _, reconcile = r.REGISTRY["group_representatives"]
    return await reconcile(ctx(engine, queue_app))


# ---- eligibility -------------------------------------------------------------------------------------------


async def test_a_member_whose_target_failed_is_not_eligible_and_without_a_tuple_m1_s_rule_holds(
    engine,
) -> None:
    async with engine.begin() as conn:
        a, b = await insert_job(conn), await insert_job(conn)
        group = await member_group(conn, a, [b])
    assert await reelect(engine, group, [a.job_id, b.job_id]) == a.job_id  # M1: no tuple, the first by order
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
        await state_target(conn, a, tuple_id, "failed_request")
    assert await reelect(engine, group, [a.job_id, b.job_id]) is None  # b has no active-tuple target either
    assert await group_row(engine, group) == ("active", None, "no_active_member", "reconciler_repair")
    async with engine.begin() as conn:
        await state_target(conn, b, tuple_id, "pending")
    assert await reelect(engine, group, [a.job_id, b.job_id]) == b.job_id
    assert await group_row(engine, group) == ("active", b.job_id, None, "reconciler_repair")


@pytest.mark.parametrize("tuple_active", [False, True], ids=["no_tuple", "active_tuple"])
async def test_eligible_sql_and_the_member_views_agree(engine, tuple_active: bool) -> None:
    """Every target state, no target, a closed job and a merged one: `ELIGIBLE_SQL` (with `active_config ac`)
    selects exactly the members `member_views` finds eligible."""
    states = [
        None,
        "pending",
        "budget_deferred",
        "in_progress",
        "in_batch",
        "deferred_duplicate",
        "published_accepted",
        "filtered_out",
        "failed_request",
        "failed_attempts_exhausted",
        "refused",
        "expired_unscored",
        "cancelled",
    ]
    async with engine.begin() as conn:
        tuple_id = await (activate_score_tuple(conn) if tuple_active else score_llm_tuple(conn))
        jobs = [await insert_job(conn) for _ in states]
        closed = await insert_job(conn)
        await conn.execute(
            sa.text("UPDATE jobs SET availability = 'closed' WHERE id = :j"), {"j": closed.job_id}
        )
        group = await member_group(conn, jobs[0], [*jobs[1:], closed])
        for job, state in zip(jobs, states, strict=True):
            if state is not None:
                await targets_in(conn, job, tuple_id, state, group)
        await state_target(conn, closed, tuple_id, "pending")
        ids = [job.job_id for job in (*jobs, closed)]
        selected = [int(i) for i in (await conn.execute(_ELIGIBLE, {"ids": ids})).scalars()]
        views = await representative.member_views(conn, group)
    assert [v.job_id for v in views if v.eligible] == selected
    if tuple_active:
        live = {"pending", "budget_deferred", "in_progress", "in_batch", "deferred_duplicate"}
        expected = [
            job.job_id
            for job, s in zip(jobs, states, strict=True)
            if s in live or str(s).startswith("published")
        ]
    else:
        expected = [job.job_id for job in jobs]  # M1: every unmerged, not closed member
    assert selected == expected


async def targets_in(conn: AsyncConnection, job: Job, tuple_id: int, state: str, group: int) -> int:
    """A target of the job in `state`, with the rows and columns its state's CHECK needs."""
    if state == "published_accepted":
        return (await insert_published(conn, job, tuple_id)).target_id
    columns: dict[str, Any] = {}
    if state == "in_progress":
        columns = {"claim_id": uuid4(), "claim_epoch": 1, "claimed_at": NOW, "claim_heartbeat_at": NOW}
    elif state == "in_batch":
        operation = await insert_bulk_operation(conn, tuple_id)
        columns = {"batch_submission_id": await insert_submission(conn, tuple_id, operation)}
    elif state == "deferred_duplicate":
        columns = {"deferred_group_id": group}
    elif state == "filtered_out":
        columns = {"filter_scope_version_id": (await active_ids(conn)).scope_version_id}
    return await state_target(conn, job, tuple_id, state, **columns)


# ---- no eligible member ------------------------------------------------------------------------------------


async def test_a_group_whose_members_all_failed_or_were_all_filtered_records_why(
    engine, current_epoch, job_with_postings, queue_app
) -> None:
    """L3259: no member eligible, the election records why; nothing is retried automatically, and the group
    is scorable again (`eligible_again`) once a member is (a scope change that its job passes)."""
    make = jobs_maker(engine, job_with_postings)
    mixed = [await make("jsearch", description_text=FULL_TEXT, company_raw="Initech") for _ in "ab"]
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
        scope = (await active_ids(conn)).scope_version_id
        failed = [await insert_job(conn) for _ in range(3)]
        filtered = [await insert_job(conn) for _ in range(2)]
        groups_of = {
            "failed": await member_group(conn, failed[0], failed[1:]),
            "filtered": await member_group(conn, filtered[0], filtered[1:]),
        }
        for job, state in zip(failed, ("failed_request", "refused", "expired_unscored"), strict=True):
            await state_target(conn, job, tuple_id, state)
        for job in filtered:
            await state_target(conn, job, tuple_id, "filtered_out", filter_scope_version_id=scope)
        made = [
            await state_target(conn, await job_ref(conn, job_id), tuple_id, "pending") for job_id in mixed
        ]
    mixed_group = (await regroup(engine, mixed[0])).group_id  # canonical jobs: they pass G1-G5 together
    assert mixed_group is not None and (await group_row(engine, mixed_group))[1] == mixed[0]
    async with engine.begin() as conn:
        for step in _FAIL:  # one member's attempt fails, the other is filtered out: no eligible member
            await conn.execute(step, {"t": made[0]})
        await conn.execute(
            sa.text(
                "UPDATE score_targets SET state = 'filtered_out', filter_scope_version_id = :s WHERE id = :t"
            ),
            {"s": scope, "t": made[1]},
        )
    assert await repair(engine, queue_app) == 3  # every representative is ineligible: one election each
    assert await group_row(engine, groups_of["failed"]) == ("active", None, "all_failed", "reconciler_repair")
    assert await group_row(engine, groups_of["filtered"]) == (
        "active",
        None,
        "all_filtered",
        "reconciler_repair",
    )
    assert await group_row(engine, mixed_group) == ("active", None, "no_active_member", "reconciler_repair")
    assert await repair(engine, queue_app) == 0  # nothing is retried automatically
    async with engine.begin() as conn:  # a scope change that the second member's job passes
        await conn.execute(
            sa.text(
                "UPDATE score_targets SET state = 'pending', filter_scope_version_id = NULL WHERE id = :t"
            ),
            {"t": made[1]},
        )
    assert await repair(engine, queue_app) == 1
    assert await group_row(engine, mixed_group) == ("active", mixed[1], None, "eligible_again")
    assert await repair(engine, queue_app) == 0


async def test_formation_and_a_representative_s_own_regroup_read_the_targets(
    engine, current_epoch, job_with_postings, queue_app
) -> None:
    """A group formed of two members whose targets failed records `all_failed` (`initial`); one formed before
    its members have targets (node 6 groups before node 9 creates them) records `no_active_member` until a
    member is eligible, when the reconciler elects it (`eligible_again`); a representative whose own regroup
    finds its target failed re-elects (`rep_inactive`)."""
    make = jobs_maker(engine, job_with_postings)
    failed = [await make("jsearch", description_text=FULL_TEXT, company_raw="Umbrella") for _ in "xy"]
    fresh = [await make("jsearch", description_text=FULL_TEXT, company_raw="Hooli") for _ in "pq"]
    live = [await make("jsearch", description_text=FULL_TEXT, company_raw="Globex") for _ in "uvw"]
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
        for job_id in failed:
            await state_target(conn, await job_ref(conn, job_id), tuple_id, "failed_request")
        made = {
            job_id: await state_target(conn, await job_ref(conn, job_id), tuple_id, "pending")
            for job_id in live
        }
    formed = await regroup(engine, failed[0])
    assert formed.created and formed.group_id is not None
    assert await group_row(engine, formed.group_id) == ("active", None, "all_failed", "initial")
    unscored = await regroup(engine, fresh[0])
    assert unscored.created and unscored.group_id is not None
    assert await group_row(engine, unscored.group_id) == ("active", None, "no_active_member", "initial")
    async with engine.begin() as conn:
        await state_target(conn, await job_ref(conn, fresh[1]), tuple_id, "pending")
    assert await repair(engine, queue_app) == 1
    assert await group_row(engine, unscored.group_id) == ("active", fresh[1], None, "eligible_again")
    group = (await regroup(engine, live[0])).group_id
    assert group is not None and (await regroup(engine, live[2])).joined
    assert (await group_row(engine, group))[1] == live[0]
    async with engine.begin() as conn:
        for step in _FAIL:
            await conn.execute(step, {"t": made[live[0]]})
    effect = await regroup(engine, live[0])
    assert effect.reelected and await group_row(engine, group) == ("active", live[1], None, "rep_inactive")


# ---- deferral ----------------------------------------------------------------------------------------------

REP: Final = MemberRow(
    job_id=1,
    score_individually=False,
    work_mode_possible=frozenset({"onsite"}),
    work_mode_source="structured",
    employment_types=frozenset({"full_time"}),
    employment_source="structured",
    seniority_tokens=frozenset({"senior"}),
)
MEMBER: Final = replace(REP, job_id=2)
TARGET: Final = TargetRow(id=10, state="pending", override="none", state_reason=None, deferred_group_id=None)


def test_deferrable_reads_each_clause() -> None:
    """L3261: each clause on its own makes a deferrable member not deferrable; `deferral_eligible` is the same
    predicate without the entering states."""
    assert deferrable(MEMBER, REP, TARGET) and deferral_eligible(MEMBER, REP, override="none")
    assert deferrable(MEMBER, REP, replace(TARGET, state="budget_deferred"))
    assert not deferrable(REP, REP, TARGET)  # the representative itself
    assert not deferrable(MEMBER, REP, replace(TARGET, override="score_now"))
    assert not deferrable(MEMBER, REP, replace(TARGET, override="individual"))
    assert not deferrable(replace(MEMBER, score_individually=True), REP, TARGET)
    assert not deferrable(MEMBER, REP, None)  # no target to defer
    for state in ("in_progress", "in_batch", "filtered_out", "deferred_duplicate", "published_accepted"):
        assert not deferrable(MEMBER, REP, replace(TARGET, state=state)), state
    # the persistent predicate keeps a deferred target: no entering-state clause (Codex r3 B14)
    assert deferral_eligible(MEMBER, REP, override="none")
    conflicts = [
        replace(MEMBER, work_mode_possible=frozenset({"remote"})),  # structured work modes differ
        replace(MEMBER, employment_types=frozenset({"part_time"})),  # structured employment types differ
        replace(MEMBER, seniority_tokens=frozenset({"junior"})),  # seniority tokens differ
    ]
    for member in conflicts:
        assert not deferrable(member, REP, TARGET) and not deferral_eligible(member, REP, override="none")
    # a set that is not structured on both sides is no conflict
    assert deferrable(
        replace(MEMBER, work_mode_possible=frozenset({"remote"}), work_mode_source="text"), REP, TARGET
    )
    assert deferrable(replace(MEMBER, employment_types=frozenset(), employment_source="none"), REP, TARGET)


@pytest.mark.parametrize(
    ("variant", "columns", "state", "override", "flagged"),
    [
        ("plain", {}, "pending", "none", False),
        ("budget_deferred", {}, "budget_deferred", "none", False),
        (
            "modes_differ",
            {"work_mode_possible": ["remote"], "work_mode_source": "structured"},
            "pending",
            "none",
            False,
        ),
        (
            "modes_text",
            {"work_mode_possible": ["remote"], "work_mode_source": "text"},
            "pending",
            "none",
            False,
        ),
        (
            "types_differ",
            {"employment_types": ["part_time"], "employment_source": "structured"},
            "pending",
            "none",
            False,
        ),
        ("tokens_differ", {"seniority_tokens": ["junior"]}, "pending", "none", False),
        ("score_now", {}, "pending", "score_now", False),
        ("individually", {}, "pending", "none", True),
    ],
)
async def test_the_regroup_sweep_defers_exactly_the_deferrable_members(
    engine, variant: str, columns: dict[str, object], state: str, override: str, flagged: bool
) -> None:
    """The sweep's SQL (`targets.DEFERRAL_ELIGIBLE_SQL` with the entering states) and `groups.deferrable`
    agree on each clause."""
    rep_facts: dict[str, object] = {
        "work_mode_possible": ["onsite"],
        "work_mode_source": "structured",
        "employment_types": ["full_time"],
        "employment_source": "structured",
        "seniority_tokens": ["senior"],
    }
    member_facts = {**rep_facts, **columns}
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
        rep, member = await insert_job(conn), await insert_job(conn)
        for job, facts in ((rep, rep_facts), (member, member_facts)):
            await set_facts(conn, job.job_id, facts)
        await member_group(conn, rep, [member])
        if flagged:
            await conn.execute(_FLAG, {"j": member.job_id})
        await state_target(conn, rep, tuple_id, "pending")
        target = await state_target(conn, member, tuple_id, state, override=override)
        await targets.regroup_sweep(conn, llm_tuple_id=tuple_id)
    expected = deferrable(
        row_of(member.job_id, member_facts, flagged),
        row_of(rep.job_id, rep_facts, False),
        TargetRow(id=target, state=state, override=override, state_reason=None, deferred_group_id=None),
    )
    assert expected == (variant in ("plain", "budget_deferred", "modes_text")), variant
    assert ((await swept_of(engine, target)).state == "deferred_duplicate") == expected, variant


_SET_FACTS: Final = sa.text(
    "UPDATE jobs SET work_mode_possible = CAST(:modes AS text[]), work_mode_source = :mode_source, "
    "employment_types = CAST(:types AS text[]), employment_source = :type_source, "
    "seniority_tokens = CAST(:tokens AS text[]) WHERE id = :j"
)


async def set_facts(conn: AsyncConnection, job_id: int, facts: dict[str, Any]) -> None:
    await conn.execute(
        _SET_FACTS,
        {
            "modes": facts["work_mode_possible"],
            "mode_source": facts["work_mode_source"],
            "types": facts["employment_types"],
            "type_source": facts["employment_source"],
            "tokens": facts["seniority_tokens"],
            "j": job_id,
        },
    )


def row_of(job_id: int, facts: dict[str, Any], flagged: bool) -> MemberRow:
    return MemberRow(
        job_id=job_id,
        score_individually=flagged,
        work_mode_possible=frozenset(facts["work_mode_possible"]),
        work_mode_source=facts["work_mode_source"],
        employment_types=frozenset(facts["employment_types"]),
        employment_source=facts["employment_source"],
        seniority_tokens=frozenset(facts["seniority_tokens"]),
    )


async def test_a_score_individually_member_is_never_deferred(engine) -> None:
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
        scope = int((await conn.execute(sa.text("SELECT scope_version_id FROM active_config"))).scalar_one())
        profile = int(
            (await conn.execute(sa.text("SELECT profile_version_id FROM active_config"))).scalar_one()
        )
        rep, member = await insert_job(conn), await insert_job(conn)
        await member_group(conn, rep, [member])
        await conn.execute(_FLAG, {"j": member.job_id})
        await state_target(conn, rep, tuple_id, "pending")
        made = await targets.create_or_revive(
            conn,
            job_id=member.job_id,
            job_revision_id=member.revision_id,
            llm_tuple_id=tuple_id,
            profile_version_id=profile,
            scope_version_id=scope,
            bulk_operation_id=None,
            now=utcnow(),
        )
        assert isinstance(made, targets.Created) and made.state == "pending"  # created pending (L2906)
        swept = await targets.regroup_sweep(conn, llm_tuple_id=tuple_id)
    assert swept.deferred == 0 and (await swept_of(engine, made.target_id)).state == "pending"


# ---- group events ------------------------------------------------------------------------------------------


async def test_a_split_member_s_deferred_target_returns_to_pending(engine, job_with_postings) -> None:
    make = jobs_maker(engine, job_with_postings)
    p, q, s = [await make("jsearch", description_text=FULL_TEXT) for _ in "pqs"]
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
        for job_id in (p, q, s):
            await state_target(conn, await job_ref(conn, job_id), tuple_id, "pending")
    group = (await regroup(engine, p)).group_id
    assert group is not None and (await regroup(engine, s)).joined
    assert (await group_row(engine, group))[1] == p  # the earliest sighting represents the group
    async with engine.begin() as conn:
        swept = await targets.regroup_sweep(conn, llm_tuple_id=tuple_id)
        target_q, target_s = [
            int(
                (
                    await conn.execute(sa.text("SELECT id FROM score_targets WHERE job_id = :j"), {"j": j})
                ).scalar_one()
            )
            for j in (q, s)
        ]
    assert swept.deferred == 2
    async with engine.begin() as conn:
        await groups.split_member(conn, group, s)
    split = await swept_of(engine, target_s)
    assert (split.state, split.deferred_group_id) == ("pending", None)
    assert (await swept_of(engine, target_q)).state == "deferred_duplicate"  # still deferred to p
    async with engine.begin() as conn:
        await groups.split_member(conn, group, q)  # p is left alone: the group is dissolved
    assert (await group_row(engine, group))[0] == "dissolved"
    released = await swept_of(engine, target_q)
    assert (released.state, released.deferred_group_id) == ("pending", None)


async def test_a_representative_change_keeps_an_eligible_member_deferred(
    engine, current_epoch, job_with_postings, queue_app
) -> None:
    """Codex r3 B14: the representative fails; the reconciler elects the next eligible member (a deferred one,
    `deferred_duplicate` being eligible), whose own target returns to `pending`; a deferred member that still
    satisfies `deferral_eligible` against the new representative stays deferred to the group; one whose
    structured employment types conflict with the new representative's (not with the old one's) returns to
    `pending`."""
    make = jobs_maker(engine, job_with_postings)
    a, b, c, d = [await make("jsearch", description_text=FULL_TEXT) for _ in "abcd"]
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
        for job_id, types in ((b, ["full_time"]), (d, ["part_time"])):  # a's and c's are not structured
            await conn.execute(
                sa.text(
                    "UPDATE jobs SET employment_types = CAST(:t AS text[]), employment_source = 'structured' "
                    "WHERE id = :j"
                ),
                {"t": types, "j": job_id},
            )
        made = {
            job_id: await state_target(conn, await job_ref(conn, job_id), tuple_id, "pending")
            for job_id in (a, b, c, d)
        }
    group = (await regroup(engine, a)).group_id
    assert group is not None
    for job_id in (c, d):
        assert (await regroup(engine, job_id)).joined
    assert (await group_row(engine, group))[1] == a
    async with engine.begin() as conn:
        assert (await targets.regroup_sweep(conn, llm_tuple_id=tuple_id)).deferred == 3
        for step in _FAIL:  # the representative's target fails
            await conn.execute(step, {"t": made[a]})
    assert await repair(engine, queue_app) == 1
    assert await group_row(engine, group) == ("active", b, None, "reconciler_repair")
    states = {job_id: await swept_of(engine, made[job_id]) for job_id in (b, c, d)}
    assert (states[b].state, states[b].deferred_group_id) == ("pending", None)  # the new representative
    assert (states[c].state, states[c].deferred_group_id) == ("deferred_duplicate", group)  # kept deferred
    assert (states[d].state, states[d].deferred_group_id) == ("pending", None)  # conflicts with b


async def test_a_full_text_twin_is_elected_and_always_included(engine, job_with_postings) -> None:
    make = jobs_maker(engine, job_with_postings)
    p = await make("adzuna", description_text=PARTIAL, description_completeness="partial")
    q = await make("adzuna", description_text=PARTIAL, description_completeness="partial")
    f = await make("greenhouse", description_text=FULL_TEXT)
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
        made = {
            job_id: await state_target(conn, await job_ref(conn, job_id), tuple_id, "pending")
            for job_id in (p, q, f)
        }
        profile = int(
            (await conn.execute(sa.text("SELECT profile_version_id FROM active_config"))).scalar_one()
        )
    group = (await regroup(engine, p)).group_id
    assert group is not None and (await group_row(engine, group))[1] == p
    effect = await regroup(engine, f)  # the full twin of the partial representative joins: it is elected
    assert effect.joined and effect.reelected
    assert await group_row(engine, group) == ("active", f, None, "fuller_member_joined")
    async with engine.connect() as conn:
        found = await admission.candidates(
            conn, llm_tuple_id=tuple_id, cycle_id=0, profile_version_id=profile, now=utcnow()
        )
    assert (made[f], "full_text_twin") in found.always_include
    assert made[p] not in [target for target, _ in found.always_include]  # partial: no twin of anything
