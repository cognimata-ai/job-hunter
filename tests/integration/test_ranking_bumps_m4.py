"""The ranking bumps of M4 Task 3 on PostgreSQL (spec §7.1 L1255; plan M4 Task 3, B30, B32).

The ranking generation labels the Jobs list's pages and raises its "Ranking changed" banner, so it advances on
every transaction that changes what a page shows, at most once and as its last statement, and not otherwise:
- a claim (`pending → in_progress`) stays in the unscored bucket and leaves it as it is (B30);
- a reclaim (`stuck_targets`, `orphaned_cycles`, `fail_cycle` and both takeover sweeps) leaves it as it is
  while every claim goes back to `pending`, and advances it once when any claim ends
  `failed_attempts_exhausted`, however many do;
- `det_completeness` advances it once per chunk that wrote a current instance of the active scope, never for
  the pending scope's;
- `awaiting_embedding` advances it once when it set a similarity, never for a pass with nothing to fill;
- `hard_filter_completeness` advances it when a job's active-scope `passed` changed, never for a rewrite of
  equal values (B32).

Each reads the generation before and after (`tests.support.settings.generation`). Every datum is invented.
"""

import hashlib
from collections.abc import Sequence
from datetime import timedelta
from typing import Any, Final, cast
from uuid import uuid4

import procrastinate
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.embeddings import EmbedderClient, make_http_client
from jobhunter.graphs.score.context import ScoreContext
from jobhunter.graphs.score.deps import ScoreDeps
from jobhunter.graphs.score.nodes import claim as claim_node
from jobhunter.graphs.score.state import CycleState
from jobhunter.scoring import cycles, filters
from jobhunter.scoring import reconcilers as scoring_reconcilers
from jobhunter.scoring.admission import Picked
from jobhunter.worker.embedding_reconcile import reconcile_awaiting_embedding
from jobhunter.worker.epoch import Swept
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.collect import ctx
from tests.support.embedder import FakeEmbedder
from tests.support.llm import FakeLLM
from tests.support.m3 import active_ids, insert_cycle, insert_job
from tests.support.scoring import (
    NoClaims,
    PendingTarget,
    activate_score_tuple,
    embed_revision,
    embedding_at,
    insert_target,
    insert_targets,
    profile_of_tuple,
    profile_vector_at,
    published_result,
    score_deps,
    target_of,
)
from tests.support.settings import generation

pytestmark = pytest.mark.integration

LATER: Final = timedelta(minutes=11)  # past `awaiting_embedding`'s 10-minute grace
_AGE_CLAIMS: Final = sa.text(
    "UPDATE score_targets SET claim_heartbeat_at = now() - interval '10 minutes' "
    "WHERE id = ANY(CAST(:ids AS bigint[]))"
)
_AGE_CYCLE: Final = sa.text(
    "UPDATE score_cycles SET heartbeat_at = now() - interval '10 minutes' WHERE id = :c"
)
_RUNNING_CYCLE: Final = sa.text("SELECT id FROM score_cycles WHERE status = 'running'")
_SPEND: Final = sa.text(
    "UPDATE score_targets SET charged_attempts = 4 WHERE id = ANY(CAST(:ids AS bigint[]))"
)
_NEW_SCOPE: Final = sa.text(
    "INSERT INTO scope_versions (scope_hash, payload) SELECT :hash, payload || "
    "jsonb_build_object('industries_exclude', jsonb_build_array('tabak')) FROM scope_versions "
    "WHERE id = (SELECT scope_version_id FROM active_config WHERE id = 1) RETURNING id"
)
_SET_PENDING: Final = sa.text("UPDATE active_config SET pending_scope_version_id = :s WHERE id = 1")
_ACTIVE_SCOPE: Final = sa.text("SELECT scope_version_id FROM active_config WHERE id = 1")
_INSTANCES: Final = sa.text(
    "SELECT d.scope_version_id FROM det_results d WHERE d.llm_result_id = :r AND d.is_current "
    "ORDER BY d.scope_version_id"
)
_SIMILARITY: Final = sa.text("SELECT similarity FROM score_targets WHERE id = :t")
_FILTER_ROW: Final = sa.text(
    "SELECT passed, inputs_hash FROM hard_filter_results WHERE job_id = :j AND scope_version_id = :s"
)
_SET_HASH: Final = sa.text("UPDATE jobs SET det_facts_hash = :h WHERE id = :j")
_SET_LANGUAGE: Final = sa.text("UPDATE jobs SET ad_language = :l, det_facts_hash = :h WHERE id = :j")


async def active_scope(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(_ACTIVE_SCOPE)).scalar_one())


async def add_pending_scope(engine: AsyncEngine) -> int:
    """A scope version of the active scope with one industry excluded, made the pending one; its id."""
    async with engine.begin() as conn:
        digest = hashlib.sha256(uuid4().bytes).digest()
        scope_id = int((await conn.execute(_NEW_SCOPE, {"hash": digest})).scalar_one())
        await conn.execute(_SET_PENDING, {"s": scope_id})
    return scope_id


# ---- the claim ---------------------------------------------------------------------------------------------


async def run_claim(deps: ScoreDeps, cycle_id: int, target: PendingTarget) -> Any:
    """The `claim` node of a round admitting `target` (`claim_node.make_claim`, its own transaction)."""
    async with deps.engine.connect() as conn:
        active = await active_ids(conn)
    context = ScoreContext(
        cycle_id,
        1,
        target.llm_tuple_id,
        active.scope_version_id,
        active.evaluation_policy_id,
        deps,
        NoClaims(),
    )
    state = {
        "current_round": [
            {"target_id": target.target_id, "lane": "similarity", "always_include_reason": None}
        ],
        "claim_rounds": 0,
    }
    return await claim_node.make_claim(deps)(cast(CycleState, state), {"configurable": {"cycle": context}})


async def test_a_claim_leaves_the_generation_unchanged(
    engine: AsyncEngine, current_epoch: int, fake_llm: FakeLLM
) -> None:
    deps = await score_deps(engine, fake_llm.gateway(engine))
    (pending,) = await insert_targets(engine, 1)
    async with engine.begin() as conn:
        cycle_id = await insert_cycle(conn, pending.llm_tuple_id)
    before = await generation(engine)
    update = await run_claim(deps, cycle_id, pending)
    assert [ref["target_id"] for ref in update["claimed"]] == [pending.target_id]
    assert (await target_of(engine, pending.target_id)).state == "in_progress"  # claimed, committed
    assert await generation(engine) == before  # pending and in_progress share the unscored bucket


# ---- the reclaims ------------------------------------------------------------------------------------------

RECLAIMERS: Final = (
    "stuck_targets",
    "orphaned_cycles",
    "fail_cycle",
    "score_targets_sweep",
    "score_cycles_sweep",
)


async def claim_in_cycle(engine: AsyncEngine, targets: Sequence[PendingTarget]) -> int:
    """The running cycle of epoch 1 (the one a reclaim that ends no cycle left, else a new one) claims
    `targets` (`claim_round`: their attempts are the cycle's), claims and cycle beaten 10 minutes ago; its
    id."""
    ids = [t.target_id for t in targets]
    async with engine.begin() as conn:
        running = (await conn.execute(_RUNNING_CYCLE)).scalar_one_or_none()
        cycle_id = await insert_cycle(conn, targets[0].llm_tuple_id) if running is None else int(running)
        active = await active_ids(conn)
        claimed = await claim_node.claim_round(
            conn,
            [Picked(target_id, lane="similarity") for target_id in ids],
            epoch=1,
            cycle_id=cycle_id,
            execution_config_id=active.execution_config_id,
            evaluation_policy_id=active.evaluation_policy_id,
        )
        assert sorted(c.id for c in claimed) == sorted(ids)
        await conn.execute(_AGE_CLAIMS, {"ids": ids})
        await conn.execute(_AGE_CYCLE, {"c": cycle_id})
    return cycle_id


async def reclaim_by(
    engine: AsyncEngine, queue_app: procrastinate.App, reclaimer: str, cycle_id: int
) -> None:
    """The reclaimer's own transaction: each reclaims the cycle's three claims (the cycles' ends fail it)."""
    if reclaimer == "stuck_targets":
        assert await scoring_reconcilers.reconcile_stuck_targets(ctx(engine, queue_app)) == 3  # claims
        return
    if reclaimer == "orphaned_cycles":
        assert await cycles.reconcile_orphaned_cycles(ctx(engine, queue_app)) == 1  # cycles
        return
    # the cycle task's own transaction (`fail_cycle`), or a takeover sweep's
    async with engine.begin() as conn:
        if reclaimer == "fail_cycle":
            assert await cycles.fail_cycle(conn, cycle_id, epoch=1, error_class="NodeFailed") == 3
        elif reclaimer == "score_targets_sweep":
            swept = await cycles.sweep_older_epoch_claims(conn, 2)
            assert isinstance(swept, Swept) and swept.count == 3  # the claims of epoch 1 < 2
        else:
            swept = await cycles.sweep_older_epoch_cycles(conn, 2)
            assert isinstance(swept, Swept) and swept.count == 1  # the cycle, its claims reclaimed with it


@pytest.mark.parametrize("reclaimer", RECLAIMERS)
async def test_a_reclaim_to_pending_does_not_bump_and_to_exhausted_bumps_once(
    engine: AsyncEngine, current_epoch: int, queue_app: procrastinate.App, reclaimer: str
) -> None:
    targets = await insert_targets(engine, 3)
    ids = [t.target_id for t in targets]
    first = await claim_in_cycle(engine, targets)
    before = await generation(engine)
    await reclaim_by(engine, queue_app, reclaimer, first)
    assert [(await target_of(engine, i)).state for i in ids] == ["pending"] * 3
    assert await generation(engine) == before  # in_progress → pending: the unscored bucket still

    second = await claim_in_cycle(engine, targets)  # a later cycle claims them again (attempt 2)
    async with engine.begin() as conn:
        await conn.execute(_SPEND, {"ids": ids[:2]})  # two have no charged attempt left
    await reclaim_by(engine, queue_app, reclaimer, second)
    assert [(await target_of(engine, i)).state for i in ids] == [
        "failed_attempts_exhausted",
        "failed_attempts_exhausted",
        "pending",
    ]
    assert await generation(engine) == before + 1  # once for the transaction, however many crossed


# ---- det_completeness --------------------------------------------------------------------------------------


async def test_det_completeness_bumps_when_it_writes_an_active_scope_instance_only(
    engine: AsyncEngine, current_epoch: int, queue_app: procrastinate.App
) -> None:
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
        published = [await published_result(conn, await insert_job(conn), tuple_id) for _ in range(2)]
    scope = await active_scope(engine)
    before = await generation(engine)
    assert await scoring_reconcilers.reconcile_det_completeness(ctx(engine, queue_app)) == 2
    assert await generation(engine) == before + 1  # one chunk, one bump
    assert await scoring_reconcilers.reconcile_det_completeness(ctx(engine, queue_app)) == 0
    assert await generation(engine) == before + 1  # nothing written: no bump
    pending = await add_pending_scope(engine)
    assert await scoring_reconcilers.reconcile_det_completeness(ctx(engine, queue_app)) == 2
    async with engine.connect() as conn:
        for result in published:
            found = await conn.execute(_INSTANCES, {"r": result.result_id})
            assert list(found.scalars()) == sorted([scope, pending])
    assert await generation(engine) == before + 1  # the pending scope's instances: the list shows none


# ---- awaiting_embedding ------------------------------------------------------------------------------------


async def test_filled_similarity_bumps_once_and_nothing_to_fill_does_not(
    engine: AsyncEngine, current_epoch: int, fake_embedder: FakeEmbedder, queue_app: procrastinate.App
) -> None:
    made = [await insert_target(engine) for _ in range(2)]
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
        await profile_vector_at(conn, await profile_of_tuple(conn, tuple_id), "summary", (0, 1.0))
        for target in made:  # embedded by a run's `embed` node: nothing is left for the embedder
            passage = await embedding_at(conn, f"revision:{target.job_revision_id}", (0, 0.6), (1, 0.8))
            await embed_revision(conn, target.job_revision_id, passage)
    before = await generation(engine)
    async with make_http_client() as http:
        client = EmbedderClient(fake_embedder.base_url, http)
        assert (
            await reconcile_awaiting_embedding(ctx(engine, queue_app, utcnow() + LATER, embedder=client)) == 0
        )
        async with engine.connect() as conn:
            for target in made:
                found = (await conn.execute(_SIMILARITY, {"t": target.target_id})).scalar_one()
                assert found == pytest.approx(0.6, abs=1e-3)
        assert await generation(engine) == before + 1  # two similarities, one bump
        assert (
            await reconcile_awaiting_embedding(ctx(engine, queue_app, utcnow() + LATER, embedder=client)) == 0
        )
    assert fake_embedder.batches == []  # nothing was embedded
    assert await generation(engine) == before + 1  # nothing to fill: no bump


# ---- hard_filter_completeness ------------------------------------------------------------------------------


async def test_hard_filter_completeness_bumps_when_passed_changes_only(
    engine: AsyncEngine, current_epoch: int, queue_app: procrastinate.App
) -> None:
    async with engine.begin() as conn:
        job = (await insert_job(conn)).job_id  # no target: only the result itself can move the list
    scope = await active_scope(engine)
    reconcile = filters.hard_filter_completeness

    async def stored() -> Any:
        async with engine.connect() as conn:
            return (await conn.execute(_FILTER_ROW, {"j": job, "s": scope})).one()

    before = await generation(engine)
    assert await reconcile(ctx(engine, queue_app)) == 1  # its first result
    assert (await stored()).passed is True
    assert await generation(engine) == before + 1  # no result → passed: the list's column changed

    # its hash moves while its facts do not: the row is rewritten with the same `passed`
    rehashed = hashlib.sha256(b"det facts rehashed").digest()
    async with engine.begin() as conn:
        await conn.execute(_SET_HASH, {"h": rehashed, "j": job})
    assert await reconcile(ctx(engine, queue_app)) == 1
    assert (await stored()).passed is True and bytes((await stored()).inputs_hash) == rehashed
    assert await generation(engine) == before + 1  # an equal rewrite: no bump

    # a pending scope's missing row recomputes the job for both scopes: the active one's again equal
    await add_pending_scope(engine)
    assert await reconcile(ctx(engine, queue_app)) == 1
    assert await generation(engine) == before + 1

    # its language becomes one the scope excludes: `passed` flips under the active scope
    changed = hashlib.sha256(b"det facts changed").digest()
    async with engine.begin() as conn:
        await conn.execute(_SET_LANGUAGE, {"l": "other", "h": changed, "j": job})
    assert await reconcile(ctx(engine, queue_app)) == 1
    assert (await stored()).passed is False
    assert await generation(engine) == before + 2  # once
    assert await reconcile(ctx(engine, queue_app)) == 0
    assert await generation(engine) == before + 2  # nothing stale: no bump
