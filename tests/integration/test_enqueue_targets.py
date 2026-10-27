"""Node 9, `enqueue_targets`, and I5 on the run's tuple (spec §9.2 node 9, §9.7, §17.2 I5, §17.3; plan Task
14, R9, Deviation 9, A2 R2): `graphs.collect.nodes.enqueue_targets` and `finalize`'s I5.

With the run's tuple active, the node gives every current revision of the run's jobs (created or changed in
the run) a target of that tuple, its similarity from the profile's vectors, and cancels the unpublished
targets of the revisions that stopped being current (`revision_superseded`); a fenced job is skipped and
counted. When a confirm superseded the run's tuple meanwhile, the node creates nothing (the confirm's
`create_targets_for_tuple` owns the new tuple) and I5 passes with `"targets": "tuple_superseded"`. I5 fails
for a revision of the run left without a target. The run report's `revisions.targets` counts what the node
did.

Every datum is invented.
"""

from typing import Any

import pytest
import sqlalchemy as sa
from jobhunter.graphs.collect.nodes.enqueue_targets import make_enqueue_targets
from jobhunter.graphs.collect.nodes.finalize import InvariantResult, check_invariants, run_revisions
from jobhunter.graphs.collect.state import CollectUpdate, initial_state
from jobhunter.sources.base import SourceAdapter
from jobhunter.sources.instances import InstanceRow
from jobhunter.worker.runs import RunOwner
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.collect import make_deps, queued_run, run_to_the_end
from tests.support.m3 import Job, insert_job, insert_published
from tests.support.scoring import (
    activate_score_tuple,
    embed_revision,
    embedding_at,
    fence_jobs,
    profile_of_tuple,
    profile_vector_at,
    state_target,
    target_row,
)
from tests.support.settings import confirm_new_profile

pytestmark = pytest.mark.integration

_REVISION = sa.text(
    "INSERT INTO job_revisions (job_id, revision_no, material_hash, llm_input, description_text, "
    "description_completeness, ad_language, embedding_status) VALUES (:j, :n, :h, '{}', "
    "'Erfundene Testanzeige, neu.', 'full', 'de', 'awaiting_embedding') RETURNING id"
)
# a job's current revision changes, as identity changes it (`jobs.updated_at` with it)
_CURRENT = sa.text("UPDATE jobs SET current_revision_id = :r, updated_at = now() WHERE id = :j")
_TARGETS_OF_JOB = sa.text(
    "SELECT job_revision_id, state, state_reason FROM score_targets WHERE job_id = :j AND llm_tuple_id = :t "
    "ORDER BY job_revision_id"
)


def no_adapters(instance: InstanceRow) -> SourceAdapter:
    raise AssertionError("node 9 fetches nothing")


async def run_node(engine: AsyncEngine, owner: RunOwner, tuple_id: int | None) -> CollectUpdate:
    node = make_enqueue_targets(make_deps(engine, no_adapters))
    return await node({**initial_state(owner.run_id, owner.attempt_no), "llm_tuple_id": tuple_id})


async def i5(engine: AsyncEngine, owner: RunOwner) -> InvariantResult:
    async with engine.connect() as conn:
        revisions = await run_revisions(conn, owner.run_id)
        results = await check_invariants(conn, owner.run_id, outcome_conflicts=0, revisions=revisions)
    return {r.code: r for r in results}["I5"]


async def new_revision(engine: AsyncEngine, job: Job) -> int:
    async with engine.begin() as conn:
        revision = int((await conn.execute(_REVISION, {"j": job.job_id, "n": 2, "h": b"\x02"})).scalar_one())
        await conn.execute(_CURRENT, {"r": revision, "j": job.job_id})
    return revision


async def targets_of_job(engine: AsyncEngine, job: Job, tuple_id: int) -> list[tuple[Any, ...]]:
    async with engine.connect() as conn:
        return [tuple(r) for r in await conn.execute(_TARGETS_OF_JOB, {"j": job.job_id, "t": tuple_id})]


@pytest.fixture
async def tuple_id(engine: AsyncEngine) -> int:
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
        await profile_vector_at(conn, await profile_of_tuple(conn, tuple_id), "summary", (1023, 1.0))
    return tuple_id


async def test_a_run_creates_targets_for_its_tuple_with_similarity(
    engine: AsyncEngine, current_epoch: int, tuple_id: int
) -> None:
    async with engine.begin() as conn:
        before_run = await insert_job(conn)  # not the run's: no target
    owner = await queued_run(engine)
    async with engine.begin() as conn:
        embedded = await insert_job(conn)
        awaiting = await insert_job(conn)
        fenced = await insert_job(conn)
        await embed_revision(
            conn, embedded.revision_id, await embedding_at(conn, "run", (0, 0.6), (1023, 0.8))
        )
        await fence_jobs(conn, fenced.job_id)
    assert (await i5(engine, owner)).ok is False  # two revisions of the run have no target yet
    update = await run_node(engine, owner, tuple_id)
    assert update["steps"] == ["enqueue_targets"]
    assert update["counters"] == {"targets_created": 2, "targets_skipped_fenced": 1}
    made = {
        job.job_id: await targets_of_job(engine, job, tuple_id)
        for job in (embedded, awaiting, fenced, before_run)
    }
    assert made[embedded.job_id] == [(embedded.revision_id, "pending", None)]
    assert made[awaiting.job_id] == [(awaiting.revision_id, "pending", None)]
    assert made[fenced.job_id] == [] and made[before_run.job_id] == []
    async with engine.connect() as conn:
        similarities: dict[Any, Any] = dict(
            (
                await conn.execute(
                    sa.text("SELECT job_id, similarity FROM score_targets WHERE llm_tuple_id = :t"),
                    {"t": tuple_id},
                )
            ).all()
        )
    assert (
        similarities[embedded.job_id] == pytest.approx(0.8, abs=2e-3)
        and similarities[awaiting.job_id] is None
    )
    assert await i5(engine, owner) == InvariantResult(
        "I5", True, {"llm_tuple_id": tuple_id, "revisions": 2, "missing": 0}
    )
    # a second pass (a database retry, a resumed attempt) finds nothing to do
    assert (await run_node(engine, owner, tuple_id))["counters"] == {"targets_skipped_fenced": 1}


async def test_the_superseded_revisions_unpublished_target_is_cancelled(
    engine: AsyncEngine, current_epoch: int, tuple_id: int
) -> None:
    async with engine.begin() as conn:
        changed, republished = await insert_job(conn), await insert_job(conn)
        old_target = await state_target(conn, changed, tuple_id, "pending")
        published = await insert_published(conn, republished, tuple_id)
    owner = await queued_run(engine)
    changed_revision = await new_revision(engine, changed)
    republished_revision = await new_revision(engine, republished)
    update = await run_node(engine, owner, tuple_id)
    assert update["counters"] == {"targets_created": 2, "targets_cancelled": 1}
    assert await targets_of_job(engine, changed, tuple_id) == [
        (changed.revision_id, "cancelled", "revision_superseded"),
        (changed_revision, "pending", None),
    ]
    assert await targets_of_job(engine, republished, tuple_id) == [
        (republished.revision_id, "published_accepted", None),  # a published target is terminal
        (republished_revision, "pending", None),
    ]
    assert (await target_row(engine, old_target)).state == "cancelled"
    assert (await target_row(engine, published.target_id)).state == "published_accepted"


async def test_i5_passes_after_a_mid_run_confirm(
    engine: AsyncEngine, current_epoch: int, tuple_id: int, queue_app: object
) -> None:
    # R9: a confirm during the run makes another tuple active; the run's own tuple is superseded, and the
    # confirm's create_targets_for_tuple owns the new one
    owner = await queued_run(engine)
    async with engine.begin() as conn:
        job = await insert_job(conn)
    _, new_tuple = await confirm_new_profile(engine)
    update = await run_node(engine, owner, tuple_id)
    assert update["counters"] == {"targets_skipped_tuple_superseded": 1}
    assert (
        await targets_of_job(engine, job, tuple_id) == []
        and await targets_of_job(engine, job, new_tuple) == []
    )
    assert await i5(engine, owner) == InvariantResult(
        "I5", True, {"llm_tuple_id": tuple_id, "targets": "tuple_superseded"}
    )


async def test_i5_is_vacuous_for_a_run_without_a_tuple(engine: AsyncEngine, current_epoch: int) -> None:
    owner = await queued_run(engine)  # no profile is active: the run has no tuple
    async with engine.begin() as conn:
        await insert_job(conn)
    assert (await run_node(engine, owner, None)) == {"steps": ["enqueue_targets"]}
    assert await i5(engine, owner) == InvariantResult("I5", True, {"llm_tuple_id": None})


async def test_a_run_reports_the_targets_it_made(
    engine: AsyncEngine,
    current_epoch: int,
    tuple_id: int,
    fake_http: object,
    fake_embedder: object,
    collect_deps: Any,
) -> None:
    rid = await run_to_the_end(engine, collect_deps)
    async with engine.connect() as c:
        status, report = (
            await c.execute(sa.text("SELECT status, report FROM runs WHERE id = :i"), {"i": rid})
        ).one()
        targets = (
            await c.execute(
                sa.text("SELECT count(*) FROM score_targets WHERE llm_tuple_id = :t"), {"t": tuple_id}
            )
        ).scalar_one()
    assert status == "succeeded" and targets > 0
    assert report["revisions"]["targets"] == {"created": targets, "revived": 0, "cancelled": 0, "skipped": {}}
    assert report["execution"]["invariants"]["I5"] == {
        "ok": True,
        "detail": {"llm_tuple_id": tuple_id, "revisions": targets, "missing": 0},
    }
