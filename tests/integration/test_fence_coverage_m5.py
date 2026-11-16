"""The writers M5 adds to §20.2's consumer table (plan M5 Task 8, Deviation 14, P2, D21).

Each writer checks `is_fenced` after taking the job row `FOR SHARE`, in the transaction that writes: under a
purge fence over job J (a `jobs` fence, one fence over several jobs where a test needs it) the writer skips J
or refuses it, and the same call goes through once the fence is released. A job found gone (a purge deleted it
after the writer read its work) counts as fenced: the writer writes nothing for it and raises nothing.

- `det_completeness` and the scope apply's precompute skip J (no `det_results` instance);
- `golden.record_feedback` and `golden.set_flags` (activating a case) refuse J with 409 `conflict_state`,
  `detail = "purge_in_progress"`;
- a group-wide action (`apply_to_group`) refuses when a member is fenced, and a split refuses when one of the
  group's members is (the same refusal);
- the `awaiting_embedding` reconciler and the `embed` node write no vector for a revision of J, and the
  similarity fill sets none on J's target;
- Score now, Retry and Score individually answer 404 for J, as the detail does;
- the Jobs list omits J, from its page, its counts and its facets, as its read snapshot sees the fence: the
  list reads "is any fence open" once per request in that snapshot (plan `2026-11-16-jobs-list-perf` Task P1,
  Ruling JP-1), so a fence released after the snapshot began still hides J and one raised after it does not.

`det_completeness` also meets a `source` fence that names J only through the lineage of J's revision
(`job_revision_sources`), the branch `is_fenced` evaluates at call time. The embed write leaves out a revision
deleted since its selection (its job surviving), and the embedder's backlog and a run's partial cause leave
out the revisions of a fenced job (M5 Task 8 fix round 1).

Every datum is invented.
"""

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import uuid4

import httpx
import procrastinate
import pytest
import sqlalchemy as sa
from jobhunter import embeddings
from jobhunter.clock import utcnow
from jobhunter.db.snapshot import read_snapshot
from jobhunter.embeddings import EmbedderClient, embed_awaiting, embedding_text, make_http_client, text_sha256
from jobhunter.errors import Refusal
from jobhunter.graphs.collect.nodes import finalize
from jobhunter.graphs.collect.nodes.embed import REVISIONS_AWAITING, make_embed
from jobhunter.graphs.collect.state import initial_state
from jobhunter.jobs.facets import facets
from jobhunter.jobs.listing import read_active, select_page
from jobhunter.jobs.query import JobsQuery
from jobhunter.privacy.fences import any_fence_open
from jobhunter.quota import ledger
from jobhunter.scoring import golden, similarity
from jobhunter.scoring import reconcilers as scoring_reconcilers  # registers `det_completeness`
from jobhunter.settings import scope_apply
from jobhunter.worker import operations as ops
from jobhunter.worker.embedding_reconcile import reconcile_awaiting_embedding
from jobhunter.worker.epoch import fence_epoch
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.collect import ctx, make_deps, queued_run
from tests.support.eval import scored_job
from tests.support.faults import PausePoint
from tests.support.fences import FenceKind, insert_test_fence
from tests.support.fixtures import count_rows
from tests.support.jobs_list import Card, seed_cards
from tests.support.m3 import Job, insert_job
from tests.support.pg import PgServer, blocked_or_done
from tests.support.procs import await_until
from tests.support.profile import idem
from tests.support.reconcile import run_reconciler
from tests.support.scoring import (
    activate_score_tuple,
    aged_job,
    embed_revision,
    embedding_at,
    fence_jobs,
    insert_targets,
    instances_of,
    profile_of_tuple,
    profile_vector_at,
    published_result,
    state_target,
    target_row,
)
from tests.support.settings import fresh_key, generation

pytestmark = pytest.mark.integration

NOW: Final = datetime(2026, 11, 10, 9, 30, tzinfo=UTC)
LATER: Final = timedelta(minutes=11)  # past the `awaiting_embedding` reconciler's grace
PURGE_IN_PROGRESS: Final = {"status": 409, "code": "conflict_state", "detail": "purge_in_progress"}

_RELEASE: Final = sa.text("UPDATE purge_fences SET released_at = now() WHERE id = :f")
_NEW_SCOPE: Final = sa.text(
    "INSERT INTO scope_versions (scope_hash, payload) SELECT :hash, payload || "
    "jsonb_build_object('industries_exclude', jsonb_build_array(CAST(:industry AS text))) "
    "FROM scope_versions WHERE id = (SELECT scope_version_id FROM active_config WHERE id = 1) RETURNING id"
)
_SET_PENDING: Final = sa.text("UPDATE active_config SET pending_scope_version_id = :s WHERE id = 1")
_POINTERS: Final = sa.text(
    "SELECT scope_version_id, pending_scope_version_id FROM active_config WHERE id = 1"
)
_JOB_OF_REVISION: Final = sa.text("SELECT job_id FROM job_revisions WHERE id = :r")
_EMBEDDING_STATUS: Final = sa.text("SELECT embedding_status FROM job_revisions WHERE id = :r")
_SIMILARITY: Final = sa.text("SELECT similarity FROM score_targets WHERE id = :t")
_ACTIONS: Final = sa.text("SELECT job_id, saved_at FROM user_job_actions ORDER BY job_id")
_GROUP_MEMBERS: Final = sa.text("SELECT count(*) FROM duplicate_group_members WHERE group_id = :g")
_FLAG: Final = sa.text("SELECT score_individually FROM duplicate_group_members WHERE job_id = :j")
_CASE_ACTIVE: Final = sa.text("SELECT active FROM golden_cases WHERE id = :i")
# A lineage row naming `:s` for the revision's description: what a posting of that source left behind.
_LINEAGE: Final = sa.text(
    "INSERT INTO job_revision_sources (job_revision_id, field, source_id) VALUES (:r, 'description_text', :s)"
)
# A source of none of the test jobs' postings: a fence on it matches a job by lineage alone.
LINEAGE_SOURCE: Final = "adzuna"
_EMBEDDINGS_OF_TEXT: Final = sa.text("SELECT count(*) FROM embeddings WHERE text_sha256 = :sha")
_LLM_INPUT: Final = sa.text("SELECT llm_input FROM job_revisions WHERE id = :r")
_OPEN_UNAVAILABLE: Final = sa.text(
    "SELECT count(*) FROM alerts WHERE code = 'embedder.unavailable' AND resolved_at IS NULL"
)
# A job with one published result (`published_result`), deleted child first as a purge's chunk deletes it.
_DELETE_PUBLISHED_JOB: Final = (
    "SET CONSTRAINTS ALL DEFERRED",
    "DELETE FROM det_results WHERE job_id = :j",
    "DELETE FROM llm_results WHERE target_id IN (SELECT id FROM score_targets WHERE job_id = :j)",
    "DELETE FROM score_drafts WHERE attempt_id IN (SELECT a.id FROM score_attempts a "
    "JOIN score_targets t ON t.id = a.target_id WHERE t.job_id = :j)",
    "DELETE FROM llm_calls WHERE target_id IN (SELECT id FROM score_targets WHERE job_id = :j)",
    "DELETE FROM score_attempts WHERE target_id IN (SELECT id FROM score_targets WHERE job_id = :j)",
    "DELETE FROM score_targets WHERE job_id = :j",
    "DELETE FROM job_revisions WHERE job_id = :j",
    "DELETE FROM jobs WHERE id = :j",
)
# A job of `revisions_awaiting` (one posting, one revision awaiting its embedding).
_DELETE_AWAITING_JOB: Final = (
    "SET CONSTRAINTS ALL DEFERRED",
    "DELETE FROM job_revisions WHERE job_id = :j",
    "DELETE FROM postings WHERE job_id = :j",
    "DELETE FROM jobs WHERE id = :j",
)


async def release(engine: AsyncEngine, fence_id: int) -> None:
    async with engine.begin() as conn:
        await conn.execute(_RELEASE, {"f": fence_id})


async def fence(engine: AsyncEngine, *job_ids: int) -> int:
    async with engine.begin() as conn:
        return await fence_jobs(conn, *job_ids)


async def scalar(engine: AsyncEngine, statement: sa.TextClause, **params: Any) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(statement, params)).scalar_one()


async def current_for(engine: AsyncEngine, result_id: int, scope_version_id: int) -> list[Any]:
    return [
        row
        for row in await instances_of(engine, result_id)
        if row.is_current and row.scope_version_id == scope_version_id
    ]


async def published_jobs(engine: AsyncEngine, n: int) -> list[tuple[Job, int]]:
    """`n` jobs, each with a published result of the score tuple (made active) and no instance yet."""
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
        made = []
        for _ in range(n):
            job = await insert_job(conn)
            made.append((job, (await published_result(conn, job, tuple_id)).result_id))
    return made


async def active_scope(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(_POINTERS)).one().scope_version_id)


async def pending_scope(engine: AsyncEngine) -> int:
    """A version of the active scope with one more excluded industry (an input of every instance), made the
    pending one; its id."""
    async with engine.begin() as conn:
        params = {"hash": hashlib.sha256(uuid4().bytes).digest(), "industry": uuid4().hex[:8]}
        scope_id = int((await conn.execute(_NEW_SCOPE, params)).scalar_one())
        await conn.execute(_SET_PENDING, {"s": scope_id})
    return scope_id


async def apply_scope(engine: AsyncEngine, scope_version_id: int) -> ops.OperationContext:
    """The `apply_scope` operation of the pending scope, claimed by epoch 1 and run to its end."""
    async with engine.begin() as conn:
        operation_id, _ = await ops.create_operation(
            conn,
            kind=scope_apply.KIND,
            payload={"scope_version_id": scope_version_id},
            dedup_key=scope_apply.dedup_key(scope_version_id),
            defer=None,
        )
    async with engine.begin() as conn:
        claim = await ops.claim_operation(conn, operation_id, epoch=1)
    assert claim is not None
    context = ops.OperationContext(claim=claim, engine=engine)
    await scope_apply.run_apply_scope(context)
    return context


def problem(r: httpx.Response) -> dict[str, Any]:
    body = r.json()
    return {"status": r.status_code, "code": body["code"], "detail": body.get("detail")}


def refusal(raised: pytest.ExceptionInfo[Refusal]) -> dict[str, Any]:
    return {"status": raised.value.status, "code": raised.value.code, "detail": raised.value.detail}


# ---- the det instances -------------------------------------------------------------------------------------


async def fence_by(engine: AsyncEngine, how: str, job: Job) -> int:
    """A fence over `job`: a `jobs` fence naming it, or a `source` fence naming it only through the lineage
    of its revision (the job has no posting of that source, and the fence no affected set)."""
    async with engine.begin() as conn:
        if how == "jobs":
            return await fence_jobs(conn, job.job_id)
        await conn.execute(_LINEAGE, {"r": job.revision_id, "s": LINEAGE_SOURCE})
        return await insert_test_fence(conn, kind="source", source_id=LINEAGE_SOURCE)


@pytest.mark.parametrize("fenced_by", ["jobs", "source_lineage"])
async def test_det_completeness_skips_a_fenced_job(
    engine: AsyncEngine, current_epoch: int, queue_app: procrastinate.App, fenced_by: str
) -> None:
    (fenced, fenced_result), (_, other_result) = await published_jobs(engine, 2)
    scope = await active_scope(engine)
    fence_id = await fence_by(engine, fenced_by, fenced)
    assert await run_reconciler("det_completeness", engine, now=utcnow(), epoch=1) == 1
    assert await current_for(engine, fenced_result, scope) == []
    assert len(await current_for(engine, other_result, scope)) == 1
    await release(engine, fence_id)
    assert await run_reconciler("det_completeness", engine, now=utcnow(), epoch=1) == 1
    assert len(await current_for(engine, fenced_result, scope)) == 1
    assert scoring_reconcilers.DET_CHUNK > 2  # one chunk held both results


async def test_scope_precompute_skips_a_fenced_job(engine: AsyncEngine, current_epoch: int) -> None:
    (fenced, fenced_result), (_, other_result) = await published_jobs(engine, 2)
    fence_id = await fence(engine, fenced.job_id)
    first = await pending_scope(engine)
    applied = await apply_scope(engine, first)
    # the fenced job's result got no instance, and the final check did not wait for it: the scope flipped
    assert await current_for(engine, fenced_result, first) == []
    assert len(await current_for(engine, other_result, first)) == 1
    assert applied.ended is not None and applied.ended.status == "succeeded"
    async with engine.connect() as conn:
        assert tuple((await conn.execute(_POINTERS)).one()) == (first, None)
    await release(engine, fence_id)
    second = await pending_scope(engine)
    await apply_scope(engine, second)
    assert len(await current_for(engine, fenced_result, second)) == 1
    assert len(await current_for(engine, other_result, second)) == 1


# ---- the golden set ----------------------------------------------------------------------------------------


async def test_record_feedback_refuses_a_fenced_job(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        scored = await scored_job(conn)
    fence_id = await fence(engine, scored.job.job_id)
    with pytest.raises(Refusal) as refused:
        async with engine.begin() as conn:
            await golden.record_feedback(
                conn, job_id=scored.job.job_id, kind="label", label="good", comment=None, now=NOW
            )
    assert refusal(refused) == PURGE_IN_PROGRESS
    assert await count_rows(engine, "score_feedback") == 0 and await count_rows(engine, "golden_cases") == 0
    await release(engine, fence_id)
    async with engine.begin() as conn:
        recorded = await golden.record_feedback(
            conn, job_id=scored.job.job_id, kind="label", label="good", comment=None, now=NOW
        )
    assert await count_rows(engine, "score_feedback") == 1
    assert await scalar(engine, _CASE_ACTIVE, i=recorded.golden_case_id) is True


async def test_set_flags_refuses_to_activate_a_case_of_a_fenced_job(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        scored = await scored_job(conn)
        case_id = (
            await golden.record_feedback(
                conn, job_id=scored.job.job_id, kind="label", label="ok", comment=None, now=NOW
            )
        ).golden_case_id
        await golden.set_flags(conn, case_id, active=False, is_canary=None, now=NOW)
    fence_id = await fence(engine, scored.job.job_id)
    with pytest.raises(Refusal) as refused:
        async with engine.begin() as conn:
            await golden.set_flags(conn, case_id, active=True, is_canary=None, now=NOW)
    assert refusal(refused) == PURGE_IN_PROGRESS
    assert await scalar(engine, _CASE_ACTIVE, i=case_id) is False
    async with engine.begin() as conn:  # deactivating is no activation: allowed under the fence
        assert not (await golden.set_flags(conn, case_id, active=False, is_canary=None, now=NOW)).active
    await release(engine, fence_id)
    async with engine.begin() as conn:
        assert (await golden.set_flags(conn, case_id, active=True, is_canary=None, now=NOW)).active


# ---- duplicate groups --------------------------------------------------------------------------------------


async def grouped(engine: AsyncEngine, deferred: int) -> tuple[int, list[int], int]:
    """A representative card (its target `pending`) and `deferred` members of its active group: the
    representative, the members and the group."""
    rep, *members = await seed_cards(
        engine, [Card(state="pending"), *[Card(deferred_to=0) for _ in range(deferred)]]
    )
    async with engine.connect() as conn:
        group = (
            await conn.execute(sa.text("SELECT duplicate_group_id FROM jobs WHERE id = :j"), {"j": rep})
        ).scalar_one()
    return rep, members, int(group)


async def test_group_actions_refuse_when_a_member_is_fenced(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, active_tuple: int
) -> None:
    rep, (member,), _ = await grouped(engine, 1)
    fence_id = await fence(engine, member)
    before = await generation(engine)
    url = f"/api/jobs/{rep}/actions"
    r = await logged_in_client.put(url, json={"saved": True, "apply_to_group": True})
    assert problem(r) == PURGE_IN_PROGRESS, r.text
    async with engine.connect() as conn:
        assert (await conn.execute(_ACTIONS)).all() == []
    assert await generation(engine) == before
    r = await logged_in_client.put(url, json={"saved": True})  # the job alone: no member is written
    assert r.status_code == 200 and r.json()["members"] == [], r.text
    await release(engine, fence_id)
    r = await logged_in_client.put(url, json={"saved": True, "apply_to_group": True})
    assert r.status_code == 200, r.text
    assert [m["job_id"] for m in r.json()["members"]] == [member]


async def test_split_refuses_a_fenced_member(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, active_tuple: int
) -> None:
    rep, (split, other), group = await grouped(engine, 2)
    before = await generation(engine)
    url = f"/api/duplicate-groups/{group}/split"
    for fenced in (split, other):  # the member split off, and another member of its group
        fence_id = await fence(engine, fenced)
        r = await logged_in_client.post(url, json={"job_id": split})
        assert problem(r) == PURGE_IN_PROGRESS, (fenced, r.text)
        assert await scalar(engine, _GROUP_MEMBERS, g=group) == 3
        assert await count_rows(engine, "dedup_decisions") == 0 and await generation(engine) == before
        await release(engine, fence_id)
    r = await logged_in_client.post(url, json={"job_id": split})
    assert r.status_code == 200, r.text
    assert sorted(m["job_id"] for m in r.json()["members"]) == sorted([rep, other])


# ---- the embeddings and the similarity ---------------------------------------------------------------------


async def jobs_of(engine: AsyncEngine, revision_ids: list[int]) -> list[int]:
    return [int(await scalar(engine, _JOB_OF_REVISION, r=revision)) for revision in revision_ids]


async def statuses(engine: AsyncEngine, revision_ids: list[int]) -> list[str]:
    return [str(await scalar(engine, _EMBEDDING_STATUS, r=revision)) for revision in revision_ids]


async def test_awaiting_embedding_skips_a_fenced_revision(
    engine: AsyncEngine,
    current_epoch: int,
    queue_app: procrastinate.App,
    fake_embedder: Any,
    revisions_awaiting: Any,
) -> None:
    revisions = await revisions_awaiting(engine, 2)
    fenced_job, _ = await jobs_of(engine, revisions)
    fence_id = await fence(engine, fenced_job)
    async with make_http_client() as http:
        client = EmbedderClient(fake_embedder.base_url, http)
        assert (
            await reconcile_awaiting_embedding(ctx(engine, queue_app, utcnow() + LATER, embedder=client)) == 1
        )
        assert fake_embedder.batches == [1]  # the fenced revision's text was not even sent
        assert await statuses(engine, revisions) == ["awaiting_embedding", "embedded"]
        assert await count_rows(engine, "embeddings") == 1
        await release(engine, fence_id)
        assert (
            await reconcile_awaiting_embedding(ctx(engine, queue_app, utcnow() + LATER, embedder=client)) == 1
        )
    assert await statuses(engine, revisions) == ["embedded", "embedded"]


async def test_the_embed_node_writes_no_vector_for_a_fenced_job(
    engine: AsyncEngine, current_epoch: int, fake_embedder: Any, revisions_awaiting: Any, monkeypatch: Any
) -> None:
    revisions = await revisions_awaiting(engine, 2)
    fenced_job, _ = await jobs_of(engine, revisions)
    owner = await queued_run(engine)
    pause = PausePoint(embeddings.AFTER_HTTP)
    monkeypatch.setattr(embeddings, "afault_point", pause)

    async def guard(conn: Any) -> None:  # the node's guard: the run's owner row
        await ledger.lock_run_owner(conn, owner)

    async with make_http_client() as http:
        client = EmbedderClient(fake_embedder.base_url, http)
        task = asyncio.create_task(embed_awaiting(engine, client, guard=guard))
        await asyncio.wait_for(pause.reached.wait(), 10)
        assert fake_embedder.batches == [2]  # both vectors are back: the fence comes after the selection
        fence_id = await fence(engine, fenced_job)
        pause.resume()
        report = await asyncio.wait_for(task, 30)
        assert (report.embedded, report.awaiting) == (1, 0)  # the fenced revision is no backlog
        assert await statuses(engine, revisions) == ["awaiting_embedding", "embedded"]
        assert await count_rows(engine, "embeddings") == 1  # no vector of the fenced job's text
        await release(engine, fence_id)
        assert (await embed_awaiting(engine, client, guard=guard)).embedded == 1
    assert await statuses(engine, revisions) == ["embedded", "embedded"]


async def test_the_similarity_fill_skips_a_fenced_target(engine: AsyncEngine) -> None:
    fenced, other = await insert_targets(engine, 2)
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
        await profile_vector_at(conn, await profile_of_tuple(conn, tuple_id), "summary", (0, 1.0))
        for n, target in enumerate((fenced, other)):
            vector = await embedding_at(conn, f"fill:{n}", (0, 0.6), (1, 0.8), kind="passage")
            await embed_revision(conn, target.job_revision_id, vector)
    fence_id = await fence(engine, fenced.job_id)

    async def fill() -> int:
        async with engine.begin() as conn:
            return await similarity.fill_missing_similarity(conn, llm_tuple_id=tuple_id, now=NOW)

    assert await fill() == 1
    assert await scalar(engine, _SIMILARITY, t=fenced.target_id) is None
    assert await scalar(engine, _SIMILARITY, t=other.target_id) == pytest.approx(0.6, abs=1e-3)
    await release(engine, fence_id)
    assert await fill() == 1
    assert await scalar(engine, _SIMILARITY, t=fenced.target_id) == pytest.approx(0.6, abs=1e-3)


# ---- the user's target moves -------------------------------------------------------------------------------


async def test_score_now_retry_and_score_individually_are_404_for_a_fenced_job(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, queue_app: procrastinate.App
) -> None:
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
        waiting = await aged_job(conn)
        waiting_target = await state_target(conn, waiting, tuple_id, "pending")
        failed = await aged_job(conn)
        failed_target = await state_target(conn, failed, tuple_id, "failed_request", charged_attempts=3)
    rep, (member,), group = await grouped(engine, 1)
    fence_id = await fence(engine, waiting.job_id, failed.job_id, member)
    before = await generation(engine)

    async def moves() -> list[httpx.Response]:
        return [
            await logged_in_client.post(
                f"/api/jobs/{waiting.job_id}/score-now", json={}, headers=idem(fresh_key("score-now"))
            ),
            await logged_in_client.post(
                f"/api/targets/{failed_target}/retry", headers=idem(fresh_key("retry"))
            ),
            await logged_in_client.post(
                f"/api/duplicate-groups/{group}/score-individually", json={"job_id": member}
            ),
        ]

    for r in await moves():
        assert (r.status_code, r.json()["code"]) == (404, "not_found"), r.text
    assert (await target_row(engine, waiting_target)).override == "none"
    assert (await target_row(engine, failed_target)).state == "failed_request"
    assert await scalar(engine, _FLAG, j=member) is False
    assert await generation(engine) == before
    await release(engine, fence_id)
    assert [r.status_code for r in await moves()] == [202, 202, 200]
    assert await scalar(engine, _FLAG, j=member) is True
    assert rep != member


# ---- the Jobs list -----------------------------------------------------------------------------------------


async def test_the_jobs_list_omits_a_fenced_job(engine: AsyncEngine, active_tuple: int) -> None:
    fenced, other = await seed_cards(engine, [Card(state="pending"), Card(state="pending")])
    fence_id = await fence(engine, fenced)

    async def listed() -> tuple[list[int], int | None]:
        async with read_snapshot(engine) as conn:
            selected = await select_page(
                conn, JobsQuery(), window=(None, None), after=None, limit=100, with_counts=True
            )
        return [
            row.job_id for row in selected.rows
        ], None if selected.counts is None else selected.counts.total

    assert await listed() == ([other], 1)
    await release(engine, fence_id)
    assert await listed() == ([other, fenced], 2)


FENCED_COMPANY: Final = "gesperrt gmbh"
FENCED_SOURCE: Final = "adzuna"
# In one snapshot: whether a fence is open, the first page's job ids, its counts (total, unscored), and the
# facets' company and source counts.
Seen = tuple[bool, list[int], tuple[int, int] | None, dict[str, int], dict[str, int]]


async def fenced_and_other(engine: AsyncEngine) -> list[int]:
    """Two unscored cards, the first of a company and a source of its own."""
    fenced = Card(state="pending", company_key=FENCED_COMPANY, source_ids=(FENCED_SOURCE,))
    return await seed_cards(engine, [fenced, Card(state="pending")])


async def in_snapshot(conn: AsyncConnection) -> Seen:
    """What the list and its facets show in `conn`'s snapshot (`Seen`), each reading the fence once."""
    query = JobsQuery()
    selected = await select_page(conn, query, window=(None, None), after=None, limit=100, with_counts=True)
    counted = await facets(conn, query, window=(None, None), company_q="", limit=100)
    counts = None if selected.counts is None else (selected.counts.total, selected.counts.unscored)
    return (
        await any_fence_open(conn),
        [row.job_id for row in selected.rows],
        counts,
        {c.company_key: c.count for c in counted.companies},
        {s.source_id: s.count for s in counted.sources},
    )


async def fence_of(engine: AsyncEngine, kind: FenceKind, job: int) -> int:
    """An open fence of `kind` that covers `job`: a `jobs` fence over it, a `source` fence of its source
    with it in the stored affected set (`seed_cards` writes no posting or lineage), or a `global` fence."""
    if kind == "jobs":
        return await fence(engine, job)
    async with engine.begin() as conn:
        if kind == "source":
            return await insert_test_fence(conn, kind="source", source_id=FENCED_SOURCE, job_ids=[job])
        return await insert_test_fence(conn, kind=kind)


@pytest.mark.parametrize("kind", ["jobs", "source", "global"])
async def test_the_page_its_counts_and_its_facets_omit_a_fenced_job_until_its_release(
    engine: AsyncEngine, active_tuple: int, kind: FenceKind
) -> None:
    fenced, other = await fenced_and_other(engine)
    fence_id = await fence_of(engine, kind, fenced)
    async with read_snapshot(engine) as conn:
        if kind == "global":  # every job is fenced
            assert await in_snapshot(conn) == (True, [], (0, 0), {}, {})
        else:
            assert await in_snapshot(conn) == (True, [other], (1, 1), {"musterfirma": 1}, {"jsearch": 1})
    await release(engine, fence_id)
    async with read_snapshot(engine) as conn:
        assert await in_snapshot(conn) == (
            False,
            [other, fenced],
            (2, 2),
            {"musterfirma": 1, FENCED_COMPANY: 1},
            {"jsearch": 1, FENCED_SOURCE: 1},
        )


async def test_the_page_and_its_facets_follow_their_snapshot_while_a_fence_opens_or_closes(
    engine: AsyncEngine, active_tuple: int
) -> None:
    fenced, other = await fenced_and_other(engine)
    hidden = (True, [other], (1, 1), {"musterfirma": 1}, {"jsearch": 1})
    listed = (
        False,
        [other, fenced],
        (2, 2),
        {"musterfirma": 1, FENCED_COMPANY: 1},
        {"jsearch": 1, FENCED_SOURCE: 1},
    )
    # a fence committed before the snapshot and released after it began still hides the job in it
    fence_id = await fence(engine, fenced)
    async with read_snapshot(engine) as conn:
        await read_active(conn)  # the snapshot is taken
        await release(engine, fence_id)
        assert await in_snapshot(conn) == hidden
    # a fence committed after the snapshot began does not
    async with read_snapshot(engine) as conn:
        await read_active(conn)
        fence_id = await fence(engine, fenced)
        assert await in_snapshot(conn) == listed
    await release(engine, fence_id)


# ---- a job deleted under its writer ------------------------------------------------------------------------


async def test_a_writer_meeting_a_deleted_job_writes_nothing(
    engine: AsyncEngine,
    migrated: PgServer,
    current_epoch: int,
    queue_app: procrastinate.App,
    fake_embedder: Any,
    revisions_awaiting: Any,
    monkeypatch: Any,
) -> None:
    # the embed node: the revision's job is deleted between the embedder's answer and the write
    (revision,) = await revisions_awaiting(engine, 1)
    (gone,) = await jobs_of(engine, [revision])
    owner = await queued_run(engine)
    pause = PausePoint(embeddings.AFTER_HTTP)
    monkeypatch.setattr(embeddings, "afault_point", pause)

    async def guard(conn: Any) -> None:
        await ledger.lock_run_owner(conn, owner)

    async with make_http_client() as http:
        task = asyncio.create_task(
            embed_awaiting(engine, EmbedderClient(fake_embedder.base_url, http), guard=guard)
        )
        await asyncio.wait_for(pause.reached.wait(), 10)
        async with engine.begin() as conn:
            for statement in _DELETE_AWAITING_JOB:
                await conn.execute(sa.text(statement), {"j": gone})
        pause.resume()
        report = await asyncio.wait_for(task, 30)
    assert report.embedded == 0
    assert await count_rows(engine, "embeddings") == 0  # no vector of the deleted revision's text

    # `det_completeness`: the job is deleted (uncommitted) after the chunk read its page, before its lock
    ((job, result),) = await published_jobs(engine, 1)
    async with engine.connect() as other:
        for statement in _DELETE_PUBLISHED_JOB:
            await other.execute(sa.text(statement), {"j": job.job_id})
        pass_ = asyncio.create_task(run_reconciler("det_completeness", engine, now=utcnow(), epoch=1))
        await await_until(lambda: blocked_or_done(migrated, pass_), 10)
        await other.commit()
    assert await asyncio.wait_for(pass_, 30) == 0
    assert await instances_of(engine, result) == [] and await count_rows(engine, "det_results") == 0


# ---- fix round 1: a revision deleted under the embed write, and the fenced backlog -----------------------


async def test_a_revision_deleted_during_its_embed_call_gets_no_vector(
    engine: AsyncEngine, current_epoch: int, fake_embedder: Any, revisions_awaiting: Any, monkeypatch: Any
) -> None:
    """A purge deletes a selected revision while its embed call runs; its job survives, unfenced (a source
    purge that rebuilt the job and released its fence). The write locks the revisions still awaiting and
    writes no `embeddings` row of the deleted revision's text (the reconciler's guard: the epoch first)."""
    deleted, kept = await revisions_awaiting(engine, 2)
    async with engine.connect() as conn:
        deleted_text = text_sha256(
            embedding_text((await conn.execute(_LLM_INPUT, {"r": deleted})).scalar_one())
        )
    pause = PausePoint(embeddings.AFTER_HTTP)
    monkeypatch.setattr(embeddings, "afault_point", pause)

    async def epoch(conn: Any) -> None:
        await fence_epoch(conn, current_epoch)

    async with make_http_client() as http:
        client = EmbedderClient(fake_embedder.base_url, http)
        task = asyncio.create_task(embed_awaiting(engine, client, guard=epoch, guard_step="epoch"))
        await asyncio.wait_for(pause.reached.wait(), 10)
        async with engine.begin() as conn:
            await conn.execute(
                sa.text("UPDATE jobs SET current_revision_id = NULL WHERE current_revision_id = :r"),
                {"r": deleted},
            )
            await conn.execute(sa.text("DELETE FROM job_revisions WHERE id = :r"), {"r": deleted})
        pause.resume()
        report = await asyncio.wait_for(task, 30)
    assert report.embedded == 1
    assert await statuses(engine, [kept]) == ["embedded"]
    assert await scalar(engine, _EMBEDDINGS_OF_TEXT, sha=deleted_text) == 0
    assert await count_rows(engine, "embeddings") == 1


async def test_a_fenced_backlog_leaves_the_embedder_alert_resolvable(
    engine: AsyncEngine,
    current_epoch: int,
    queue_app: procrastinate.App,
    fake_embedder: Any,
    revisions_awaiting: Any,
) -> None:
    """`embedder.unavailable` resolves once nothing a pass would take awaits: a fenced job's revision is no
    backlog while its fence stands, and is one again once it is released."""
    (revision,) = await revisions_awaiting(engine, 1)
    (job_id,) = await jobs_of(engine, [revision])
    async with engine.begin() as conn:
        await embeddings.raise_unavailable(conn, kind="unavailable", run_id=None)
    fence_id = await fence(engine, job_id)
    async with engine.connect() as conn:
        assert await embeddings.count_awaiting(conn) == 0
    async with make_http_client() as http:
        client = EmbedderClient(fake_embedder.base_url, http)
        assert (
            await reconcile_awaiting_embedding(ctx(engine, queue_app, utcnow() + LATER, embedder=client)) == 0
        )
    assert fake_embedder.batches == [] and await scalar(engine, _OPEN_UNAVAILABLE) == 0
    await release(engine, fence_id)
    async with engine.connect() as conn:
        assert await embeddings.count_awaiting(conn) == 1


async def test_a_run_whose_only_awaiting_revisions_are_fenced_is_not_partial(
    engine: AsyncEngine, current_epoch: int, revisions_awaiting: Any
) -> None:
    """The run end's I6 count, its partial cause `awaiting_embedding` and the embed node's
    `revisions_awaiting` leave out the revisions of a fenced job; released, they count again."""
    owner = await queued_run(engine)
    revisions = await revisions_awaiting(engine, 2)  # created in the run
    fence_id = await fence(engine, *await jobs_of(engine, revisions))
    node = make_embed(make_deps(engine, lambda instance: pytest.fail(f"no fetch: {instance.source_id}")))

    async def run_end() -> tuple[int, list[str]]:
        async with engine.connect() as conn:
            counted = await finalize.run_revisions(conn, owner.run_id)
            causes = await finalize.partial_conditions(conn, owner.run_id, revisions=counted)
        return counted.awaiting, list(causes)

    assert await run_end() == (0, [])
    assert (await node(initial_state(owner.run_id, owner.attempt_no)))["counters"][REVISIONS_AWAITING] == 0
    await release(engine, fence_id)
    assert await run_end() == (2, ["awaiting_embedding"])
    assert (await node(initial_state(owner.run_id, owner.attempt_no)))["counters"][REVISIONS_AWAITING] == 2
