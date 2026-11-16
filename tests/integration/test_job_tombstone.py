"""Job tombstoning (spec §20.1 L6397, §20.2 L6481-6491; plan M5 Task 13, D12, M5-R3, Deviations 18 and 50) and
the shared job-graph deletion (`privacy.deletion_graph.delete_job_graph`).

`retention_daily`'s rule `job_tombstones` opens a purge per batch of due roots (`job_tombstone.open_due`,
called here without a deferrer); each `purge` operation is run as the worker runs it (`tests.support.purges`'
`Purger`): the quiesce, the canonical file, phase B (the targets in chunks of 200, then one graph
chunk per root) and phase C's verified completion. The seed-backed test proves the graph complete: every
table holds a row of the two-year-old seed, and the purges commit against the real schema with every
foreign key, CHECK and trigger enabled. Every row is invented.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.transitions import transition
from jobhunter.dedup import groups, merge
from jobhunter.dedup.identity import tombstone_key_hash
from jobhunter.dedup.revisions import FIELD_CONFLICT_ALERT
from jobhunter.graphs.checkpointer import open_saver
from jobhunter.normalize.keys import AdKey
from jobhunter.observability.alerts import raise_alert
from jobhunter.privacy import deletion_graph, purge
from jobhunter.privacy.kinds import job_tombstone
from jobhunter.privacy.retention import run_retention_daily
from jobhunter.profile.storage import CvStore
from jobhunter.settings.dryrun import count_retained_current_revisions
from jobhunter.worker import operations as ops
from jobhunter.worker.tasks import job_task_name
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.collect import (
    JobWithPostings,
    pending_item,
    put_user_actions,
    queued_run,
    resolved,
    user_actions,
)
from tests.support.faults import SimulatedCrash, no_fault, raising_at
from tests.support.groups import FULL_TEXT, canonical_job
from tests.support.logs import logged
from tests.support.m2 import insert_eval_run, insert_llm_call, insert_profile_chain
from tests.support.m3 import Job, insert_golden_case, insert_id, insert_job, insert_row
from tests.support.pg import PgServer, blocked_or_done
from tests.support.privacy_seed import CLOSED_ADZUNA_KEY, SHARED_KEY, seed_two_year_old_graph
from tests.support.procs import await_until
from tests.support.purges import Purger, completed, manifest_of
from tests.support.scoring import member_group

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("current_epoch")]

OLD: Final = job_tombstone.TOMBSTONE_AFTER + timedelta(days=1)
_AGE: Final = sa.text(
    "UPDATE jobs SET availability = :availability, availability_changed_at = :changed WHERE id = :j"
)
_SYNTHETIC: Final = sa.text("UPDATE jobs SET synthetic_eval = true WHERE id = :j")
_OPENED: Final = sa.text(
    "SELECT m.id AS manifest_id, m.request, o.id AS operation_id FROM deletion_manifests m "
    "JOIN operations o ON o.kind = 'purge' AND o.payload -> 'manifest_id' = to_jsonb(m.id) "
    "WHERE m.kind = 'job_tombstone' ORDER BY m.id"
)
_FENCE: Final = sa.text("SELECT job_ids, released_at FROM purge_fences WHERE manifest_id = :m ORDER BY id")
_EXISTING: Final = sa.text("SELECT id FROM jobs WHERE id = ANY(CAST(:ids AS bigint[])) ORDER BY id")
_TOMBSTONES: Final = sa.text(
    "SELECT former_job_id, id, user_actions FROM job_tombstones "
    "WHERE former_job_id = ANY(CAST(:ids AS bigint[]))"
)
_KEY_OWNER: Final = sa.text("SELECT job_id FROM ad_keys WHERE key_ns = :ns AND key_value = :v")
_TOMBSTONE_OF_KEY: Final = sa.text("SELECT tombstone_id FROM tombstone_keys WHERE key_hash = :h")
_LEFT: Final = sa.text(
    "SELECT (SELECT count(*) FROM job_revisions WHERE job_id = ANY(CAST(:ids AS bigint[]))) AS revisions, "
    "(SELECT count(*) FROM postings WHERE job_id = ANY(CAST(:ids AS bigint[]))) AS postings, "
    "(SELECT count(*) FROM score_targets WHERE job_id = ANY(CAST(:ids AS bigint[]))) AS targets, "
    "(SELECT count(*) FROM det_results WHERE job_id = ANY(CAST(:ids AS bigint[]))) AS instances, "
    "(SELECT count(*) FROM user_job_actions WHERE job_id = ANY(CAST(:ids AS bigint[]))) AS actions, "
    "(SELECT count(*) FROM ad_keys WHERE job_id = ANY(CAST(:ids AS bigint[]))) AS keys, "
    "(SELECT count(*) FROM merge_events WHERE from_job_id = ANY(CAST(:ids AS bigint[])) "
    "OR into_job_id = ANY(CAST(:ids AS bigint[]))) AS merges, "
    "(SELECT count(*) FROM duplicate_group_members WHERE job_id = ANY(CAST(:ids AS bigint[]))) AS memberships"
)
_FEEDBACK: Final = sa.text("SELECT count(*) AS n, count(job_id) AS with_job FROM score_feedback")
_CASE_ROWS: Final = (
    "SELECT (SELECT count(*) FROM golden_cases WHERE id = :g) + (SELECT count(*) FROM eval_case_results "
    "WHERE golden_case_id = :g) + (SELECT count(*) FROM llm_calls WHERE golden_case_id = :g)"
)
HIDDEN: Final = datetime(2026, 9, 1, 8, 30, tzinfo=UTC)
SEEN: Final = datetime(2026, 8, 19, 9, 0, tzinfo=UTC)


# ---- the harness (`tests.support.purges`: `purge_cfg`, `purger`, `manifest_of`, `completed`) --------------


@pytest.fixture
def statements_with_params(engine: AsyncEngine) -> Any:
    """Every statement with its parameters that the engine's connections send while the test runs, in order
    (`test_purge_protocol`'s `statements` records the statements alone)."""
    sent: list[tuple[str, Any]] = []

    def record(conn: Any, cursor: Any, statement: str, parameters: Any, *args: Any) -> None:
        sent.append((statement, parameters))

    sa.event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        yield sent
    finally:
        sa.event.remove(engine.sync_engine, "before_cursor_execute", record)


@dataclass(frozen=True)
class Opened:
    manifest_id: int
    roots: list[int]
    operation_id: int


async def opened(engine: AsyncEngine) -> list[Opened]:
    """Every tombstone purge opened so far, oldest first."""
    async with engine.connect() as conn:
        rows = (await conn.execute(_OPENED)).all()
    return [Opened(int(r.manifest_id), list(r.request["job_ids"]), int(r.operation_id)) for r in rows]


async def open_due(engine: AsyncEngine, now: datetime | None = None) -> int:
    return await job_tombstone.open_due(engine, now=utcnow() if now is None else now, epoch=1, defer=None)


async def tombstone(engine: AsyncEngine, purger: Purger, now: datetime | None = None) -> list[Opened]:
    """Open the due tombstones and run each purge; the purges opened."""
    before = {o.manifest_id for o in await opened(engine)}
    await open_due(engine, now)
    made = [o for o in await opened(engine) if o.manifest_id not in before]
    for o in made:
        await purger.run(o.operation_id)
    return made


async def old_job(
    conn: AsyncConnection,
    *,
    availability: str = "closed",
    age: timedelta = OLD,
    merged_into: int | None = None,
    now: datetime | None = None,
) -> Job:
    """A job (`tests.support.m3.insert_job`) whose availability changed `age` before `now`."""
    job = await insert_job(conn, merged_into_job_id=merged_into)
    changed = (utcnow() if now is None else now) - age
    await conn.execute(_AGE, {"availability": availability, "changed": changed, "j": job.job_id})
    return job


async def existing(engine: AsyncEngine, ids: list[int] | tuple[int, ...]) -> list[int]:
    async with engine.connect() as conn:
        return [int(i) for i in (await conn.execute(_EXISTING, {"ids": list(ids)})).scalars()]


async def scalar(engine: AsyncEngine, statement: str, **params: object) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(sa.text(statement), params)).scalar_one_or_none()


async def tombstones(engine: AsyncEngine, ids: list[int] | tuple[int, ...]) -> dict[int, Any]:
    async with engine.connect() as conn:
        return {int(r.former_job_id): r for r in await conn.execute(_TOMBSTONES, {"ids": list(ids)})}


async def left_of(engine: AsyncEngine, ids: list[int] | tuple[int, ...]) -> tuple[int, ...]:
    async with engine.connect() as conn:
        return tuple((await conn.execute(_LEFT, {"ids": list(ids)})).one())


_POSTING: Final = sa.text(
    "INSERT INTO postings (source_instance_id, native_id, job_id, first_seen_at, last_seen_at, availability, "
    "availability_changed_at) SELECT id, :native, :job, :t, :t, 'closed', :t FROM source_instances "
    "WHERE source_id = 'jsearch' AND instance_key = 'default' RETURNING id"
)


async def posting(conn: AsyncConnection, job: Job, native: str, at: datetime) -> int:
    return int((await conn.execute(_POSTING, {"native": native, "job": job.job_id, "t": at})).scalar_one())


# ---- the seed: the whole graph -----------------------------------------------------------------------------


async def test_a_closed_job_older_than_90_days_is_tombstoned(engine: AsyncEngine, purger: Purger) -> None:
    """On the two-year-old seed (every table holds a row): one batch opens for every due root, the purge
    deletes each root's whole graph with the jobs merged into it, and the commits pass every foreign key,
    CHECK and trigger. The tombstones keep the hashed keys and the four user-action timestamps, never the
    note; a key another job's posting supplies goes to it; the exempt jobs and the feedback stay; the seed's
    conflicts naming the closed job lose it (the one left without a candidate goes) and its alerts resolve."""
    now = utcnow()
    async with engine.begin() as conn:
        seed = await seed_two_year_old_graph(conn, now=now)
    async with engine.connect() as conn:
        feedback_before = tuple((await conn.execute(_FEEDBACK)).one())
    made = await tombstone(engine, purger, now)
    assert [o.roots for o in made] == [list(seed.due_job_ids)]
    manifest = await completed(purger, made[0].manifest_id, made[0].operation_id)
    assert manifest.scope is not None
    gone = sorted({*seed.due_job_ids, *seed.merged_job_ids})
    assert manifest.scope["operations"] == [
        {"op": "fence", "kind": "jobs", "key": "jobs", "job_ids": gone},
        {"op": "row_delete", "table": "jobs", "ids": gone, "bound": {"by": "id"}},
    ]  # no export op: a tombstone keeps the exports
    assert await existing(engine, gone) == []
    assert await left_of(engine, gone) == (0, 0, 0, 0, 0, 0, 0, 0)  # the content went with the jobs
    assert await existing(engine, seed.exempt_job_ids) == sorted(seed.exempt_job_ids)

    stones = await tombstones(engine, gone)
    assert sorted(stones) == sorted(seed.due_job_ids)  # one per root, none for a merged job
    closed = stones[seed.closed_job_id]
    assert set(closed.user_actions) == {"seen_at", "saved_at", "applied_at", "hidden_at"}  # never the note
    assert (closed.user_actions["saved_at"], closed.user_actions["applied_at"]) == (None, None)
    for name in ("seen_at", "hidden_at"):
        assert datetime.fromisoformat(closed.user_actions[name]).utcoffset() == timedelta(0)
    async with engine.connect() as conn:
        found = await conn.execute(_TOMBSTONE_OF_KEY, {"h": tombstone_key_hash(CLOSED_ADZUNA_KEY)})
        assert found.scalar_one() == closed.id
        assert (await conn.execute(_TOMBSTONE_OF_KEY, {"h": tombstone_key_hash(SHARED_KEY)})).first() is None
        heir = await conn.execute(_KEY_OWNER, {"ns": SHARED_KEY.ns, "v": SHARED_KEY.value})
        assert heir.scalar_one() == seed.active_job_id
        feedback = tuple((await conn.execute(_FEEDBACK)).one())
        conflicts = {
            int(c.id): list(c.candidate_job_ids)
            for c in await conn.execute(
                sa.text(
                    "SELECT id, candidate_job_ids FROM identity_conflicts "
                    "WHERE id = ANY(CAST(:ids AS bigint[]))"
                ),
                {"ids": [seed.lone_conflict_id, seed.shared_conflict_id]},
            )
        }
        alerts = {
            str(key): bool(resolved)
            for key, resolved in await conn.execute(
                sa.text("SELECT dedup_key, resolved_at IS NOT NULL FROM alerts WHERE dedup_key = ANY(:keys)"),
                {
                    "keys": [
                        f"{groups.AMBIGUOUS_GROUPING}:job:{seed.closed_job_id}",
                        f"{merge.IDENTITY_CONFLICT_ALERT}:conflict:{seed.lone_conflict_id}",
                    ]
                },
            )
        }
    assert feedback == (feedback_before[0], feedback_before[1] - 1)  # kept, its job NULL
    # the seed's own conflicts and alerts: the one naming only the closed job goes, the other loses it
    assert conflicts == {seed.shared_conflict_id: [seed.active_job_id]}
    assert alerts == {
        f"{groups.AMBIGUOUS_GROUPING}:job:{seed.closed_job_id}": True,
        f"{merge.IDENTITY_CONFLICT_ALERT}:conflict:{seed.lone_conflict_id}": True,
    }

    # nothing is due any more: a second pass opens no purge
    assert await open_due(engine, now) == 0


# ---- the exemptions ----------------------------------------------------------------------------------------


async def test_saved_applied_and_active_golden_jobs_are_exempt(engine: AsyncEngine) -> None:
    """Only an unmerged, non-synthetic root closed or stale for more than 90 days is due, unless it or a job
    merged into it is saved, applied or labelled by an active golden case; one batch's fence names it."""
    async with engine.begin() as conn:
        chain = await insert_profile_chain(conn, 41)
        due = await old_job(conn)
        stale = await old_job(conn, availability="stale")
        saved = await old_job(conn)
        await put_user_actions(conn, saved.job_id, saved_at=utcnow())
        applied = await old_job(conn, availability="stale")
        await put_user_actions(conn, applied.job_id, applied_at=utcnow())
        golden = await old_job(conn)
        await insert_golden_case(conn, golden, chain.profile_version_id)
        golden_merged = await old_job(conn)
        merged = await old_job(conn, merged_into=golden_merged.job_id)
        await insert_golden_case(conn, merged, chain.profile_version_id)
        inactive_case = await old_job(conn)
        await insert_golden_case(conn, inactive_case, chain.profile_version_id, active=False)
        await old_job(conn, age=job_tombstone.TOMBSTONE_AFTER - timedelta(days=1))  # not long enough
        await old_job(conn, availability="active")
        await old_job(conn, availability="unknown")
        synthetic = await old_job(conn)
        await conn.execute(_SYNTHETIC, {"j": synthetic.job_id})
        await old_job(conn, merged_into=due.job_id)  # merged: no root of its own
    assert await open_due(engine) == 3
    [o] = await opened(engine)
    assert o.roots == sorted([due.job_id, stale.job_id, inactive_case.job_id])
    async with engine.connect() as conn:
        [fence] = (await conn.execute(_FENCE, {"m": o.manifest_id})).all()
    merged_into_due = await scalar(engine, "SELECT id FROM jobs WHERE merged_into_job_id = :j", j=due.job_id)
    assert fence.job_ids == sorted([due.job_id, stale.job_id, inactive_case.job_id, merged_into_due])
    assert fence.released_at is None
    assert await open_due(engine) == 0  # fenced already: no second batch of them


async def test_an_exemption_gained_after_the_fence_is_honoured(engine: AsyncEngine, purger: Purger) -> None:
    """D12: J2 is saved between A0 and the quiesce (as a save that read no fence would be): the content
    leaves it out, the purge deletes J1 alone, and J2's fence ends at the completion. A batch whose every root
    became exempt completes deleting nothing."""
    async with engine.begin() as conn:
        j1 = await old_job(conn)
        j2 = await old_job(conn)
    await open_due(engine)
    [o] = await opened(engine)
    assert o.roots == [j1.job_id, j2.job_id]
    async with engine.begin() as conn:
        await put_user_actions(conn, j2.job_id, saved_at=utcnow())
    await purger.run(o.operation_id)
    manifest = await completed(purger, o.manifest_id, o.operation_id)
    assert manifest.scope is not None
    assert manifest.scope["operations"] == [
        {"op": "fence", "kind": "jobs", "key": "jobs", "job_ids": [j1.job_id]},
        {"op": "row_delete", "table": "jobs", "ids": [j1.job_id], "bound": {"by": "id"}},
    ]
    assert await existing(engine, [j1.job_id, j2.job_id]) == [j2.job_id]
    assert sorted(await tombstones(engine, [j1.job_id, j2.job_id])) == [j1.job_id]
    assert await scalar(engine, "SELECT is_fenced(:j)", j=j2.job_id) is False

    # a batch whose every root became exempt deletes nothing and completes, its fence released
    async with engine.begin() as conn:
        j3 = await old_job(conn)
    await open_due(engine)
    [_, again] = await opened(engine)
    assert again.roots == [j3.job_id]
    async with engine.begin() as conn:
        await put_user_actions(conn, j3.job_id, applied_at=utcnow())
    await purger.run(again.operation_id)
    manifest = await completed(purger, again.manifest_id, again.operation_id)
    assert manifest.scope is not None
    assert manifest.scope["operations"] == [{"op": "fence", "kind": "jobs", "key": "jobs", "job_ids": []}]
    assert await existing(engine, [j3.job_id]) == [j3.job_id]
    assert await scalar(engine, "SELECT is_fenced(:j)", j=j3.job_id) is False


@pytest.mark.parametrize("availability", ["active", "closed"])
async def test_a_root_reopened_before_the_quiesce_is_not_tombstoned(
    engine: AsyncEngine, purger: Purger, availability: str
) -> None:
    """Ruling T13-3: J2 is observed again between A0 and the quiesce (a collection writer that began before
    the fence moved it back to `active`, or re-opened and closed it again): no longer due at A0's cutoff, it
    is left out of the content, its note survives, and its fence ends at the completion. J1 is tombstoned."""
    async with engine.begin() as conn:
        j1 = await old_job(conn)
        j2 = await old_job(conn)
        await put_user_actions(conn, j2.job_id, seen_at=SEEN, note="Erfundene Notiz.")
    await open_due(engine)
    [o] = await opened(engine)
    assert o.roots == [j1.job_id, j2.job_id]
    async with engine.begin() as conn:
        await conn.execute(_AGE, {"availability": availability, "changed": utcnow(), "j": j2.job_id})
    await purger.run(o.operation_id)
    manifest = await completed(purger, o.manifest_id, o.operation_id)
    assert manifest.scope is not None
    assert manifest.scope["operations"] == [
        {"op": "fence", "kind": "jobs", "key": "jobs", "job_ids": [j1.job_id]},
        {"op": "row_delete", "table": "jobs", "ids": [j1.job_id], "bound": {"by": "id"}},
    ]
    assert await existing(engine, [j1.job_id, j2.job_id]) == [j2.job_id]
    row = await user_actions(engine, j2.job_id)
    assert (row.seen_at, row.note) == (SEEN, "Erfundene Notiz.")
    assert await scalar(engine, "SELECT is_fenced(:j)", j=j2.job_id) is False


async def test_a_golden_case_activated_before_the_quiesce_exempts_its_job(
    engine: AsyncEngine, purger: Purger
) -> None:
    """D12: J2's inactive golden case is made active between A0 and the quiesce (here by SQL:
    `golden.set_flags` refuses a fenced job, so only a writer that began before the fence could do it):
    `compute`'s golden-active re-check leaves J2 out, and its case and revision stay."""
    async with engine.begin() as conn:
        chain = await insert_profile_chain(conn, 47)
        j1 = await old_job(conn)
        j2 = await old_job(conn)
        case = await insert_golden_case(conn, j2, chain.profile_version_id, active=False)
    await open_due(engine)
    [o] = await opened(engine)
    assert o.roots == [j1.job_id, j2.job_id]
    async with engine.begin() as conn:
        await conn.execute(sa.text("UPDATE golden_cases SET active = true WHERE id = :g"), {"g": case})
    await purger.run(o.operation_id)
    manifest = await completed(purger, o.manifest_id, o.operation_id)
    assert manifest.scope is not None
    assert [op.get("ids", op.get("job_ids")) for op in manifest.scope["operations"]] == [
        [j1.job_id],
        [j1.job_id],
    ]
    assert await existing(engine, [j1.job_id, j2.job_id]) == [j2.job_id]
    assert await scalar(engine, _CASE_ROWS, g=case) == 1


# ---- the graph ---------------------------------------------------------------------------------------------


_ENTRY: Final = "SELECT state || ':' || coalesce(evidence, '-') FROM llm_spend_ledger WHERE id = :l"
_CALL_STATUS: Final = "SELECT status FROM llm_calls WHERE id = :c"


@pytest.mark.parametrize(
    ("entry_state", "settled"),
    [("reserved", "not_billed:never_dispatched"), ("dispatched", "ambiguous:-")],
)
async def test_an_inactive_golden_case_is_deleted_before_the_revisions(
    engine: AsyncEngine, purger: Purger, entry_state: str, settled: str
) -> None:
    """The inactive case on the job's revision goes before the revisions (its key restricts them), with its
    eval case result and its eval call by cascade; the call's entry, still `reserved` or `dispatched`, is
    settled first (released, or made `ambiguous` at its reservation), so no entry is left without its call
    (G04)."""
    async with engine.begin() as conn:
        chain = await insert_profile_chain(conn, 42)
        job = await old_job(conn)
        case = await insert_golden_case(conn, job, chain.profile_version_id, active=False)
        run = await insert_eval_run(conn, "completed")
        ledger, call = await insert_llm_call(conn, purpose="generate", eval_run_id=run)
        await conn.execute(
            sa.text("UPDATE llm_calls SET golden_case_id = :g WHERE id = :c"), {"g": case, "c": call}
        )
        if entry_state == "dispatched":  # sent before the case was made inactive, never answered
            await transition(conn, "llm_spend_ledger", ledger, ["reserved"], "dispatched")
            await transition(conn, "llm_calls", call, ["reserved"], "dispatched")
        await insert_row(
            conn,
            "eval_case_results",
            {
                "eval_run_id": run,
                "golden_case_id": case,
                "outcome": "accepted",
                "critic_ran": False,
                "llm_call_ids": [call],
                "output": {},
            },
        )
    assert await scalar(engine, _ENTRY, l=ledger) == f"{entry_state}:-"
    assert await scalar(engine, _CASE_ROWS, g=case) == 3
    [o] = await tombstone(engine, purger)
    await completed(purger, o.manifest_id, o.operation_id)
    assert await scalar(engine, _CASE_ROWS, g=case) == 0
    assert await existing(engine, [job.job_id]) == []
    assert await scalar(engine, _ENTRY, l=ledger) == settled


async def test_merged_jobs_join_the_batch(
    engine: AsyncEngine, purger: Purger, statements_with_params: list[tuple[str, Any]]
) -> None:
    """R with M1 merged into it and M2 merged into M1: the request names R, the fence the three, and the
    graph chunk deletes them deepest first (M2, M1, R; `jobs.merged_into_job_id` restricts), one tombstone,
    R's."""
    async with engine.begin() as conn:
        root = await old_job(conn)
        m1 = await old_job(conn, merged_into=root.job_id)
        m2 = await old_job(conn, merged_into=m1.job_id)
        chain = await insert_profile_chain(conn, 43)
        for job in (root, m1, m2):
            await insert_id(
                conn,
                "score_targets",
                {
                    "job_revision_id": job.revision_id,
                    "llm_tuple_id": chain.llm_tuple_id,
                    "job_id": job.job_id,
                    "state": "pending",
                },
            )
    await open_due(engine)
    [o] = await opened(engine)
    assert o.roots == [root.job_id]
    async with engine.connect() as conn:
        [fence] = (await conn.execute(_FENCE, {"m": o.manifest_id})).all()
    assert fence.job_ids == sorted([root.job_id, m1.job_id, m2.job_id])
    statements_with_params.clear()
    await purger.run(o.operation_id)
    await completed(purger, o.manifest_id, o.operation_id)
    deleted = [
        params["job"]
        for statement, params in statements_with_params
        if statement == "DELETE FROM jobs WHERE id = %(job)s"
    ]
    assert deleted == [m2.job_id, m1.job_id, root.job_id]
    assert sorted(await tombstones(engine, [root.job_id, m1.job_id, m2.job_id])) == [root.job_id]
    assert await left_of(engine, [root.job_id, m1.job_id, m2.job_id]) == (0, 0, 0, 0, 0, 0, 0, 0)


_CURRENT: Final = sa.text("SELECT current_revision_id FROM jobs WHERE id = :j")


async def canonical(engine: AsyncEngine, job_with_postings: JobWithPostings, company: str) -> Job:
    """A canonical job of `company` (`tests.support.groups.canonical_job`: one JSearch posting, one title,
    place and full text), so the jobs of one company confirm each other (§8.7 G2-G5)."""
    job_id = await canonical_job(
        engine,
        job_with_postings,
        "jsearch",
        observed_at=utcnow() - timedelta(days=2),
        description_text=FULL_TEXT,
        company_raw=company,
    )
    async with engine.connect() as conn:
        return Job(job_id, int((await conn.execute(_CURRENT, {"j": job_id})).scalar_one()))


@dataclass(frozen=True)
class Grouped:
    representative: Job
    members: tuple[Job, ...]
    group_id: int


async def grouped(
    engine: AsyncEngine,
    job_with_postings: JobWithPostings,
    company: str,
    n: int,
    *,
    deferred: int | None = None,
) -> Grouped:
    """`n` canonical jobs of `company` (`canonical`) in one active group: the first its representative, closed
    long ago; with `deferred` (a tuple id), each other member's target deferred to the group."""
    jobs = [await canonical(engine, job_with_postings, company) for _ in range(n)]
    async with engine.begin() as conn:
        rep, members = jobs[0], tuple(jobs[1:])
        await conn.execute(_AGE, {"availability": "closed", "changed": utcnow() - OLD, "j": rep.job_id})
        group = await member_group(conn, rep, list(members))
        for member in members if deferred is not None else ():
            await insert_id(
                conn,
                "score_targets",
                {
                    "job_revision_id": member.revision_id,
                    "llm_tuple_id": deferred,
                    "job_id": member.job_id,
                    "state": "deferred_duplicate",
                    "deferred_group_id": group,
                },
            )
    return Grouped(rep, members, group)


_GROUP: Final = sa.text(
    "SELECT g.status, g.representative_job_id, (SELECT array_agg(m.job_id ORDER BY m.job_id) "
    "FROM duplicate_group_members m WHERE m.group_id = g.id) AS members "
    "FROM duplicate_groups g WHERE g.id = :g"
)
_TARGET_STATE: Final = sa.text("SELECT state, deferred_group_id FROM score_targets WHERE job_id = :j")


async def test_a_representative_is_replaced_before_it_is_deleted(
    engine: AsyncEngine, purger: Purger, job_with_postings: JobWithPostings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The representative of a group of three leaves it through `groups.leave_group`, which re-elects; the
    representative of a group of two dissolves it, and its surviving member's deferred target returns to
    `pending` (D01). The graph chunk locks the surviving members' rows with the job's, in its one ordered
    statement, before the operation owner."""
    async with engine.begin() as conn:
        chain = await insert_profile_chain(conn, 44)
    three = await grouped(engine, job_with_postings, "Initech", 3)
    two = await grouped(engine, job_with_postings, "Vandelay", 2, deferred=chain.llm_tuple_id)
    events: list[tuple[str, Any]] = []
    lock_jobs, owner, delete_graph = (
        merge.lock_jobs,
        ops.lock_operation_owner,
        deletion_graph.delete_job_graph,
    )

    async def recording_lock(conn: AsyncConnection, ids: Any, *, with_groups_of: Any = ()) -> Any:
        held = await lock_jobs(conn, ids, with_groups_of=with_groups_of)
        events.append(("jobs", (sorted(ids), sorted(with_groups_of), sorted(held))))
        return held

    async def recording_owner(conn: AsyncConnection, claim: Any) -> None:
        events.append(("owner", None))
        await owner(conn, claim)

    async def recording_graph(conn: AsyncConnection, job_id: int, **kwargs: Any) -> Any:
        events.append(("graph", job_id))
        return await delete_graph(conn, job_id, **kwargs)

    monkeypatch.setattr(merge, "lock_jobs", recording_lock)
    monkeypatch.setattr(purge, "lock_operation_owner", recording_owner)
    monkeypatch.setattr(deletion_graph, "delete_job_graph", recording_graph)
    [o] = await tombstone(engine, purger)
    await completed(purger, o.manifest_id, o.operation_id)
    for g in (three, two):
        rep, survivors = g.representative.job_id, sorted(m.job_id for m in g.members)
        at = events.index(("graph", rep))
        # the graph chunk: the job and its group's members in one statement, then the owner, then the graph
        assert events[at - 2 : at + 1] == [
            ("jobs", ([rep], [rep], sorted([rep, *survivors]))),
            ("owner", None),
            ("graph", rep),
        ]
    async with engine.connect() as conn:
        status, representative, members = (await conn.execute(_GROUP, {"g": three.group_id})).one()
        assert status == "active" and members == sorted(m.job_id for m in three.members)
        assert representative in members  # re-elected among the survivors
        assert tuple((await conn.execute(_GROUP, {"g": two.group_id})).one()) == ("dissolved", None, None)
        [survivor] = two.members
        assert tuple((await conn.execute(_TARGET_STATE, {"j": survivor.job_id})).one()) == ("pending", None)


async def test_a_co_member_joining_during_the_graph_lock_starts_the_chunk_again(
    engine: AsyncEngine,
    purger: Purger,
    job_with_postings: JobWithPostings,
    migrated: PgServer,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """J3 joins J's group while the graph chunk's job statement waits for J2's row: the statement's snapshot
    never saw J3, so the co-members read after it find a member it does not hold; the round rolls back and
    the next one holds J3 too, so J leaves the group with every member held (`leave_group` checks it), and
    the group re-elects among J2 and J3."""
    caplog.set_level(logging.INFO)
    g = await grouped(engine, job_with_postings, "Initrode", 2)
    j3 = await canonical(engine, job_with_postings, "Initrode")
    j, (j2,) = g.representative, g.members
    await open_due(engine)
    [o] = await opened(engine)
    monkeypatch.setattr(purge, "afault_point", raising_at(purge.AFTER_RECORD))
    await purger.run(o.operation_id)  # A1-A3 done, nothing deleted yet
    assert await existing(engine, [j.job_id]) == [j.job_id]
    monkeypatch.setattr(purge, "afault_point", no_fault)
    async with engine.connect() as blocker:
        await blocker.begin()
        await blocker.execute(sa.text("SELECT id FROM jobs WHERE id = :j FOR UPDATE"), {"j": j2.job_id})
        task = asyncio.create_task(purger.again(o.operation_id))
        await await_until(lambda: blocked_or_done(migrated, task), 10)
        assert not task.done()
        await insert_row(
            blocker, "duplicate_group_members", {"group_id": g.group_id, "job_id": j3.job_id, "evidence": {}}
        )
        await blocker.execute(
            sa.text("UPDATE jobs SET duplicate_group_id = :g WHERE id = :j"),
            {"g": g.group_id, "j": j3.job_id},
        )
        await blocker.commit()
    await asyncio.wait_for(task, 30)
    await completed(purger, o.manifest_id, o.operation_id)
    assert len(logged(caplog, "purge_chunk_set_changed")) == 1
    assert await existing(engine, [j.job_id, j2.job_id, j3.job_id]) == [j2.job_id, j3.job_id]
    async with engine.connect() as conn:
        status, representative, members = (await conn.execute(_GROUP, {"g": g.group_id})).one()
    assert (status, members) == ("active", [j2.job_id, j3.job_id]) and representative in members


async def test_a_restart_after_the_target_chunks_resumes_with_the_graph(
    engine: AsyncEngine, purger: Purger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deviation 50: a crash after the target chunks committed and before the graph chunk leaves the targets
    gone and the job in place; the requeued operation runs phase B on the stored scope again, finds no
    target left and deletes the graph."""
    async with engine.begin() as conn:
        chain = await insert_profile_chain(conn, 46)
        job = await old_job(conn)
        await insert_id(
            conn,
            "score_targets",
            {
                "job_revision_id": job.revision_id,
                "llm_tuple_id": chain.llm_tuple_id,
                "job_id": job.job_id,
                "state": "pending",
            },
        )
    delete_graph = deletion_graph.delete_job_graph

    async def crash(conn: AsyncConnection, job_id: int, **kwargs: Any) -> Any:
        raise SimulatedCrash("graph")

    monkeypatch.setattr(deletion_graph, "delete_job_graph", crash)
    await open_due(engine)
    [o] = await opened(engine)
    await purger.run(o.operation_id)
    row = await scalar(
        engine, "SELECT status || ':' || last_error_code FROM operations WHERE id = :i", i=o.operation_id
    )
    assert row == "pending:bug"
    assert (await manifest_of(engine, o.manifest_id)).file_sha256 is not None
    revisions, _, targets, *_ = await left_of(engine, [job.job_id])
    assert (revisions, targets) == (1, 0)  # the targets went; the graph stays
    assert await existing(engine, [job.job_id]) == [job.job_id]
    monkeypatch.setattr(deletion_graph, "delete_job_graph", delete_graph)
    await purger.again(o.operation_id)
    await completed(purger, o.manifest_id, o.operation_id)
    assert await existing(engine, [job.job_id]) == []
    assert sorted(await tombstones(engine, [job.job_id])) == [job.job_id]


async def test_a_graph_with_more_than_200_targets_is_deleted_in_bounded_chunks(
    engine: AsyncEngine, purger: Purger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A root and a job merged into it with 260 targets in all: target chunks of at most 200 (200, then
    60), then the graph chunk; `delete_target_set` never refuses a set and the manifest completes."""
    async with engine.begin() as conn:
        chain = await insert_profile_chain(conn, 45)
        root = await old_job(conn)
        merged = await old_job(conn, merged_into=root.job_id)
        for job in (root, merged):
            await conn.execute(
                sa.text(
                    "INSERT INTO job_revisions (job_id, revision_no, material_hash, llm_input, "
                    "description_text, description_completeness, ad_language, embedding_status) "
                    "SELECT :j, n, int4send(n), '{}', 'Erfundene Testanzeige.', 'full', 'de', "
                    "'awaiting_embedding' FROM generate_series(2, 130) n"
                ),
                {"j": job.job_id},
            )
        await conn.execute(
            sa.text(
                "INSERT INTO score_targets (job_revision_id, llm_tuple_id, job_id, state) "
                "SELECT id, :t, job_id, 'pending' FROM job_revisions "
                "WHERE job_id = ANY(CAST(:jobs AS bigint[]))"
            ),
            {"t": chain.llm_tuple_id, "jobs": [root.job_id, merged.job_id]},
        )
    calls: list[tuple[str, int]] = []
    purge_set, delete_graph = deletion_graph.purge_target_set, deletion_graph.delete_job_graph

    async def recording_set(conn: AsyncConnection, ids: Any, **kwargs: Any) -> Any:
        calls.append(("targets", len(ids)))
        return await purge_set(conn, ids, **kwargs)

    async def recording_graph(conn: AsyncConnection, job_id: int, **kwargs: Any) -> Any:
        calls.append(("graph", job_id))
        return await delete_graph(conn, job_id, **kwargs)

    monkeypatch.setattr(deletion_graph, "purge_target_set", recording_set)
    monkeypatch.setattr(deletion_graph, "delete_job_graph", recording_graph)
    [o] = await tombstone(engine, purger)
    await completed(purger, o.manifest_id, o.operation_id)
    assert calls == [("targets", 200), ("targets", 60), ("graph", root.job_id)]
    assert await left_of(engine, [root.job_id, merged.job_id]) == (0, 0, 0, 0, 0, 0, 0, 0)


_EVENT_IDS: Final = sa.text("SELECT id FROM merge_events ORDER BY id")
_CONFLICTS: Final = sa.text(
    "SELECT id, posting_job_id, candidate_job_ids, status FROM identity_conflicts ORDER BY id"
)


async def merge_event(conn: AsyncConnection, kind: str, from_job: Job, into_job: Job) -> int:
    return await insert_id(
        conn,
        "merge_events",
        {
            "kind": kind,
            "from_job_id": from_job.job_id,
            "into_job_id": into_job.job_id,
            "posting_ids": [0],
            "evidence": {"rule": "test"},
        },
    )


async def conflict(conn: AsyncConnection, posting_id: int, posting_job: Job, candidates: list[Job]) -> int:
    return await insert_id(
        conn,
        "identity_conflicts",
        {
            "posting_id": posting_id,
            "posting_job_id": posting_job.job_id,
            "candidate_job_ids": [c.job_id for c in candidates],
            "evidence": {},
            "status": "open",
        },
    )


async def test_merge_events_and_conflicts_naming_the_job_go_first(
    engine: AsyncEngine, purger: Purger
) -> None:
    """The merge events naming J (from or into: M's merge into J, and X's merge into J with the unmerge that
    reversed it, its `reversed_by_event_id` cleared first) are deleted, another pair's stays; the conflicts on
    J's posting and those left without a candidate go (their alerts resolved), J leaves the others'
    candidates."""
    now = utcnow()
    async with engine.begin() as conn:
        j = await old_job(conn)
        m = await old_job(conn, merged_into=j.job_id)
        x, y, z = await insert_job(conn), await insert_job(conn), await insert_job(conn)
        await merge_event(conn, "strong_key_merge", m, j)
        merged_x = await merge_event(conn, "strong_key_merge", x, j)
        unmerged_x = await merge_event(conn, "unmerge", x, j)
        await conn.execute(
            sa.text("UPDATE merge_events SET reversed_by_event_id = :u WHERE id = :e"),
            {"u": unmerged_x, "e": merged_x},
        )
        merged_y = await merge_event(conn, "strong_key_merge", y, z)
        on_j = await conflict(conn, await posting(conn, j, "c-j", now), j, [x])
        only_j = await conflict(conn, await posting(conn, x, "c-x", now), x, [j])
        with_j = await conflict(conn, await posting(conn, y, "c-y", now), y, [j, z])
        without_j = await conflict(conn, await posting(conn, z, "c-z", now), z, [y])
        for c in (on_j, only_j, with_j, without_j):
            await raise_alert(
                conn,
                code=merge.IDENTITY_CONFLICT_ALERT,
                severity="warn",
                subject=f"conflict:{c}",
                message="m",
            )
    [o] = await tombstone(engine, purger)
    await completed(purger, o.manifest_id, o.operation_id)
    async with engine.connect() as conn:
        assert [int(i) for i in (await conn.execute(_EVENT_IDS)).scalars()] == [merged_y]
        assert [tuple(r) for r in await conn.execute(_CONFLICTS)] == [
            (with_j, y.job_id, [z.job_id], "open"),
            (without_j, z.job_id, [y.job_id], "open"),
        ]
        open_alerts = await conn.execute(
            sa.text(
                "SELECT dedup_key FROM alerts WHERE resolved_at IS NULL AND code = :c ORDER BY dedup_key"
            ),
            {"c": merge.IDENTITY_CONFLICT_ALERT},
        )
        assert sorted(open_alerts.scalars()) == sorted(
            f"{merge.IDENTITY_CONFLICT_ALERT}:conflict:{c}" for c in (with_j, without_j)
        )


async def test_a_shared_key_is_reassigned_to_the_earliest_remaining_provenance(
    engine: AsyncEngine, purger: Purger
) -> None:
    """J's `htidocid` key that X's and Y's postings also supply goes to Y, whose provenance row is the
    earliest left once J's posting is gone; J's own key is deleted and hashed into J's tombstone, the shared
    one not."""
    now = utcnow()
    shared, own = AdKey("htidocid", "t13-shared"), AdKey("adzuna", "t13-own")
    async with engine.begin() as conn:
        j = await old_job(conn)
        x, y = await insert_job(conn), await insert_job(conn)
        p_j = await posting(conn, j, "k-j", now)
        p_x = await posting(conn, x, "k-x", now)
        p_y = await posting(conn, y, "k-y", now)
        for key in (shared, own):
            await insert_row(
                conn,
                "ad_keys",
                {"key_ns": key.ns, "key_value": key.value, "exclusive": key.exclusive, "job_id": j.job_id},
            )
        for key, posting_id, minutes in (
            (shared, p_j, 0),
            (shared, p_x, 120),
            (shared, p_y, 60),
            (own, p_j, 0),
        ):
            await insert_row(
                conn,
                "ad_key_sources",
                {
                    "key_ns": key.ns,
                    "key_value": key.value,
                    "posting_id": posting_id,
                    "first_seen_at": now + timedelta(minutes=minutes),
                },
            )
    [o] = await tombstone(engine, purger)
    await completed(purger, o.manifest_id, o.operation_id)
    [stone] = (await tombstones(engine, [j.job_id])).values()
    async with engine.connect() as conn:
        assert (await conn.execute(_KEY_OWNER, {"ns": shared.ns, "v": shared.value})).scalar_one() == y.job_id
        assert (await conn.execute(_KEY_OWNER, {"ns": own.ns, "v": own.value})).first() is None
        assert (
            await conn.execute(_TOMBSTONE_OF_KEY, {"h": tombstone_key_hash(own)})
        ).scalar_one() == stone.id
        assert (await conn.execute(_TOMBSTONE_OF_KEY, {"h": tombstone_key_hash(shared)})).first() is None
        sources = await conn.execute(
            sa.text("SELECT posting_id FROM ad_key_sources WHERE key_value = :v ORDER BY posting_id"),
            {"v": shared.value},
        )
        assert [int(p) for p in sources.scalars()] == sorted([p_x, p_y])


# ---- a re-posted ad ----------------------------------------------------------------------------------------


async def reposted(engine: AsyncEngine, purger: Purger, native_id: str) -> tuple[int, int, int]:
    """J, linked from an Adzuna item, seen and hidden with a note, then closed long ago and tombstoned; the
    same ad re-posted and resolved again. `(J, its tombstone, the new job)`."""
    owner = await queued_run(engine)
    item = await pending_item(engine, owner, "adzuna", {"native_id": native_id})
    assert await resolved(engine, owner, item) == "linked"
    job_id = int(
        await scalar(
            engine,
            "SELECT p.job_id FROM posting_observations o JOIN postings p ON p.id = o.posting_id "
            "WHERE o.raw_item_id = :i",
            i=item,
        )
    )
    async with engine.begin() as conn:
        await put_user_actions(conn, job_id, seen_at=SEEN, hidden_at=HIDDEN, note="Erfundene Notiz.")
        await conn.execute(_AGE, {"availability": "closed", "changed": utcnow() - OLD, "j": job_id})
    [o] = await tombstone(engine, purger)
    await completed(purger, o.manifest_id, o.operation_id)
    [stone] = (await tombstones(engine, [job_id])).values()
    again = await pending_item(engine, owner, "adzuna", {"native_id": native_id})
    assert await resolved(engine, owner, again) == "linked"
    new_job = int(
        await scalar(
            engine,
            "SELECT p.job_id FROM posting_observations o JOIN postings p ON p.id = o.posting_id "
            "WHERE o.raw_item_id = :i",
            i=again,
        )
    )
    return job_id, int(stone.id), new_job


async def test_a_reposted_ad_keeps_its_hidden_state(engine: AsyncEngine, purger: Purger) -> None:
    """§8.6 step 3: the re-posted ad's key is in `tombstone_keys`, so its new job is restored from J's
    tombstone, hidden and seen as J was; the note is gone with J."""
    job_id, stone, new_job = await reposted(engine, purger, "t13-reposted")
    assert new_job != job_id
    assert (
        await scalar(engine, "SELECT restored_from_tombstone_id FROM jobs WHERE id = :j", j=new_job) == stone
    )
    row = await user_actions(engine, new_job)
    assert (row.seen_at, row.saved_at, row.applied_at, row.hidden_at, row.note) == (
        SEEN,
        None,
        None,
        HIDDEN,
        None,
    )


async def test_a_second_tombstone_of_a_reposted_key_upserts(engine: AsyncEngine, purger: Purger) -> None:
    """The re-posted job is tombstoned in its turn: its key's `tombstone_keys` row now names the second
    tombstone (`ON CONFLICT (key_hash) DO UPDATE`, P15); the first tombstone stays."""
    _, first, new_job = await reposted(engine, purger, "t13-twice")
    key_hash = tombstone_key_hash(AdKey("adzuna", "t13-twice"))
    assert (
        await scalar(engine, "SELECT tombstone_id FROM tombstone_keys WHERE key_hash = :h", h=key_hash)
        == first
    )
    async with engine.begin() as conn:
        await conn.execute(_AGE, {"availability": "closed", "changed": utcnow() - OLD, "j": new_job})
    [o] = await tombstone(engine, purger)
    await completed(purger, o.manifest_id, o.operation_id)
    [second] = (await tombstones(engine, [new_job])).values()
    assert (
        await scalar(engine, "SELECT tombstone_id FROM tombstone_keys WHERE key_hash = :h", h=key_hash)
        == second.id
    )
    assert await scalar(engine, "SELECT count(*) FROM job_tombstones WHERE id = :t", t=first) == 1


# ---- what tombstoning leaves -------------------------------------------------------------------------------


async def test_tombstoning_deletes_no_export(engine: AsyncEngine, purger: Purger) -> None:
    """M5-R3, Deviation 18: a tombstone's content has no export op (`deletes_exports = False`), so a ready
    export keeps its row and its file."""
    async with engine.begin() as conn:
        await old_job(conn)
        export_id = int(
            (
                await conn.execute(
                    sa.text(
                        "INSERT INTO exports (status, expires_at) "
                        "VALUES ('ready', now() + interval '7 days') "
                        "RETURNING id"
                    )
                )
            ).scalar_one()
        )
        name = f"{export_id}-{uuid.uuid4()}.jhx"
        await conn.execute(
            sa.text("UPDATE exports SET storage_path = :p WHERE id = :i"), {"p": name, "i": export_id}
        )
    archive = purger.cfg.exports_dir / name
    archive.write_bytes(b"\x00 encrypted segments")
    [o] = await tombstone(engine, purger)
    manifest = await completed(purger, o.manifest_id, o.operation_id)
    assert manifest.scope is not None
    assert [op["op"] for op in manifest.scope["operations"]] == ["fence", "row_delete"]
    assert (
        await scalar(engine, "SELECT status || ':' || storage_path FROM exports WHERE id = :i", i=export_id)
        == f"ready:{name}"
    )
    assert archive.read_bytes() == b"\x00 encrypted segments"


async def test_ambiguous_grouping_resolves_for_a_tombstoned_job(engine: AsyncEngine, purger: Purger) -> None:
    """M1 Carry-forward 11: the `dedup.ambiguous_grouping` alert of a tombstoned job is resolved, also for a
    job in no group (no leave path resolves it), and so is its `dedup.field_conflict` alert (as a merge
    resolves its from-job's): no later event of a deleted job would. Another job's stay open."""
    async with engine.begin() as conn:
        j = await old_job(conn)
        other = await insert_job(conn)
        for job in (j, other):
            for code, subject in (
                (groups.AMBIGUOUS_GROUPING, f"job:{job.job_id}"),
                (FIELD_CONFLICT_ALERT, f"job:{job.job_id}:work_mode_possible"),
            ):
                await raise_alert(conn, code=code, severity="info", subject=subject, message="m")
    [o] = await tombstone(engine, purger)
    await completed(purger, o.manifest_id, o.operation_id)
    async with engine.connect() as conn:
        rows = await conn.execute(
            sa.text("SELECT dedup_key, resolved_at IS NOT NULL FROM alerts WHERE code = ANY(:c)"),
            {"c": [groups.AMBIGUOUS_GROUPING, FIELD_CONFLICT_ALERT]},
        )
        resolved_by_key = {str(k): bool(r) for k, r in rows}
    assert resolved_by_key == {
        f"{groups.AMBIGUOUS_GROUPING}:job:{j.job_id}": True,
        f"{FIELD_CONFLICT_ALERT}:job:{j.job_id}:work_mode_possible": True,
        f"{groups.AMBIGUOUS_GROUPING}:job:{other.job_id}": False,
        f"{FIELD_CONFLICT_ALERT}:job:{other.job_id}:work_mode_possible": False,
    }


async def test_count_retained_current_revisions_excludes_tombstoned_jobs(
    engine: AsyncEngine, purger: Purger
) -> None:
    """C10: the dry run's count of retained current revisions needs no clause for tombstones: a tombstoned job
    no longer exists."""
    async with engine.begin() as conn:
        await old_job(conn)
        await insert_job(conn)
        before = await count_retained_current_revisions(conn)
    [o] = await tombstone(engine, purger)
    await completed(purger, o.manifest_id, o.operation_id)
    async with engine.connect() as conn:
        assert await count_retained_current_revisions(conn) == before - 1


async def test_at_most_ten_batches_per_run(
    engine: AsyncEngine, purger: Purger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pass opens at most `TOMBSTONE_BATCHES_PER_RUN` (10) batches of at most `TOMBSTONE_BATCH` roots (one
    here), in id order; the next pass opens the rest, the fenced ones left out."""
    monkeypatch.setattr(job_tombstone, "TOMBSTONE_BATCH", 1)
    async with engine.begin() as conn:
        jobs = [(await old_job(conn)).job_id for _ in range(12)]
    assert await open_due(engine) == 10
    assert [o.roots for o in await opened(engine)] == [[job] for job in jobs[:10]]
    assert await open_due(engine) == 2
    assert [o.roots for o in await opened(engine)][10:] == [[job] for job in jobs[10:]]
    assert await open_due(engine) == 0


async def test_the_daily_retention_opens_the_due_tombstones(
    engine: AsyncEngine, purger: Purger, queue_app: Any, migrated: PgServer
) -> None:
    """`retention_daily`'s rule `job_tombstones`, after `score_history`, fences the due roots in a purge whose
    job it defers (`defer_in_tx`) and counts them; the purge then deletes them."""
    async with engine.begin() as conn:
        job = await old_job(conn)
    async with open_saver(migrated.dsn("app")) as saver:
        report = await run_retention_daily(
            engine, queue_app, saver, cv_store=CvStore(purger.cfg.cv_store_dir), now=utcnow(), epoch=1
        )
    assert report["job_tombstones"] == 1
    [o] = await opened(engine)
    assert o.roots == [job.job_id]
    deferred = await scalar(
        engine,
        "SELECT count(*) FROM procrastinate_jobs WHERE task_name = :t "
        "AND CAST(args ->> 'operation_id' AS bigint) = :o",
        t=job_task_name("run_operation"),
        o=o.operation_id,
    )
    assert deferred == 1
    await purger.run(o.operation_id)
    await completed(purger, o.manifest_id, o.operation_id)
    assert await existing(engine, [job.job_id]) == []
