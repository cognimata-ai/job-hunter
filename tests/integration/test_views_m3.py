"""The current-score views (§7.18, `db/views.sql`, migration 0008).

`current_scores` joins the active LLM tuple, the active scope and the current score instance; it never picks
the newest result by time. `display_scores` shows a deferred member of an active group its representative's
score ("scored via") and labels a member with a score of its own "possible duplicate of". Neither view shows
a synthetic golden-case job (`jobs.synthetic_eval`, Codex r1 #32, Deviation 21).
"""

import importlib
import re
from decimal import Decimal

import psycopg
import pytest
import sqlalchemy as sa
from jobhunter.db.migrate import run_migrate_async
from jobhunter.db.session import make_null_engine
from sqlalchemy.ext.asyncio import AsyncConnection

from tests.support.m2 import ProfileChain, insert_profile_chain
from tests.support.m3 import (
    active_ids,
    insert_det_result,
    insert_group,
    insert_id,
    insert_job,
    insert_published,
    insert_row,
    insert_target,
)
from tests.support.pg import PgServer

pytestmark = pytest.mark.integration

CURRENT = sa.text(
    "SELECT job_id, target_state, llm_result_id, result_status, det_result_id, score_low, score_mid, "
    "score_high, passes_hard_filters FROM current_scores WHERE job_id = ANY (:ids) ORDER BY job_id"
)
DISPLAY = sa.text(
    "SELECT job_id, det_result_id, score_mid, result_status, scored_via_job_id, "
    "possible_duplicate_of_job_id, rendered_in_group_card, target_state FROM display_scores "
    "WHERE job_id = ANY (:ids) ORDER BY job_id"
)


async def activate(c: AsyncConnection) -> ProfileChain:
    """A confirmed profile whose tuple `active_config` points at."""
    chain = await insert_profile_chain(c)
    await c.execute(
        sa.text("UPDATE active_config SET profile_version_id = :p, llm_tuple_id = :t"),
        {"p": chain.profile_version_id, "t": chain.llm_tuple_id},
    )
    return chain


async def test_current_scores_show_the_current_instance_of_the_active_scope(engine) -> None:
    async with engine.connect() as c:
        chain = await activate(c)
        scope = (await active_ids(c)).scope_version_id
        other_scope = await insert_id(c, "scope_versions", {"scope_hash": b"m3-test-scope", "payload": {}})
        scored, unscored = await insert_job(c), await insert_job(c)
        first = await insert_published(c, scored, chain.llm_tuple_id)
        second = await insert_published(c, unscored, chain.llm_tuple_id)
        for result, job in ((first.result_id, scored.job_id), (second.result_id, unscored.job_id)):
            # a superseded instance of the active scope and the current one of another scope
            await insert_det_result(c, result, job, scope, low=Decimal(10), high=Decimal(30), current=False)
            await insert_det_result(c, result, job, other_scope, low=Decimal(70), high=Decimal(90))
        current = await insert_det_result(
            c, first.result_id, scored.job_id, scope, low=Decimal(40), high=Decimal(61)
        )
        await insert_row(
            c,
            "hard_filter_results",
            {
                "job_id": scored.job_id,
                "scope_version_id": scope,
                "passed": True,
                "inputs_hash": b"\x00",
                "det_algorithm_version": "det-test",
            },
        )
        rows = (await c.execute(CURRENT, {"ids": [scored.job_id, unscored.job_id]})).all()
    assert [tuple(row) for row in rows] == [
        (
            scored.job_id,
            "published_accepted",
            first.result_id,
            "accepted",
            current,
            Decimal("40.00"),
            Decimal("50.50"),
            Decimal("61.00"),
            True,
        ),
        # published, but no current instance under the active scope: no score, and no filter result yet
        (unscored.job_id, "published_accepted", second.result_id, "accepted", None, None, None, None, None),
    ]


async def test_display_scores_label_the_members_of_a_group(engine) -> None:
    async with engine.connect() as c:
        chain = await activate(c)
        scope = (await active_ids(c)).scope_version_id
        representative, deferred, own, ungrouped = [await insert_job(c) for _ in range(4)]
        group = await insert_group(c, representative, [deferred, own])
        dets = {}
        for job, (low, high) in (
            (representative, (Decimal(60), Decimal(80))),
            (own, (Decimal(20), Decimal(40))),
            (ungrouped, (Decimal(0), Decimal(10))),
        ):
            result = (await insert_published(c, job, chain.llm_tuple_id)).result_id
            dets[job.job_id] = await insert_det_result(c, result, job.job_id, scope, low=low, high=high)
        await insert_target(
            c, deferred, chain.llm_tuple_id, state="deferred_duplicate", deferred_group_id=group
        )
        ids = [job.job_id for job in (representative, deferred, own, ungrouped)]
        rows = (await c.execute(DISPLAY, {"ids": ids})).all()
    rep = representative.job_id
    published = "published_accepted"
    assert [tuple(row) for row in rows] == [
        (rep, dets[rep], Decimal("70.00"), "accepted", None, None, False, published),
        # the representative's instance, scored via the representative's job (L3066-3067)
        (deferred.job_id, dets[rep], Decimal("70.00"), "accepted", rep, None, True, "deferred_duplicate"),
        # its own score, labelled as a possible duplicate of the representative
        (own.job_id, dets[own.job_id], Decimal("30.00"), "accepted", None, rep, False, published),
        # A1 issue 34: an ungrouped job has neither label
        (ungrouped.job_id, dets[ungrouped.job_id], Decimal("5.00"), "accepted", None, None, False, published),
    ]


async def test_without_an_active_tuple_every_unmerged_job_shows_without_a_target(engine) -> None:
    """A1 issue 33: before the first confirmed profile `active_config.llm_tuple_id` is NULL."""
    async with engine.connect() as c:
        tuple_pointer = sa.text("SELECT llm_tuple_id FROM active_config")
        assert (await c.execute(tuple_pointer)).scalar_one() is None
        chain = await insert_profile_chain(c)
        scored, bare = await insert_job(c), await insert_job(c)
        result = (await insert_published(c, scored, chain.llm_tuple_id)).result_id
        scope = (await active_ids(c)).scope_version_id
        await insert_det_result(c, result, scored.job_id, scope, low=Decimal(50), high=Decimal(70))
        merged = await insert_job(c, merged_into_job_id=scored.job_id)
        ids = [scored.job_id, bare.job_id, merged.job_id]
        current = (await c.execute(CURRENT, {"ids": ids})).all()
        display = (await c.execute(DISPLAY, {"ids": ids})).all()
    assert [tuple(row) for row in current] == [
        (scored.job_id, None, None, None, None, None, None, None, None),
        (bare.job_id, None, None, None, None, None, None, None, None),
    ]
    assert [tuple(row) for row in display] == [
        (scored.job_id, None, None, None, None, None, None, None),
        (bare.job_id, None, None, None, None, None, None, None),
    ]


async def test_a_synthetic_eval_job_is_in_neither_view(engine) -> None:
    async with engine.connect() as c:
        chain = await activate(c)
        scope = (await active_ids(c)).scope_version_id
        synthetic, real = await insert_job(c, synthetic_eval=True), await insert_job(c)
        for job in (synthetic, real):
            result = (await insert_published(c, job, chain.llm_tuple_id)).result_id
            await insert_det_result(c, result, job.job_id, scope, low=Decimal(50), high=Decimal(70))
        ids = [synthetic.job_id, real.job_id]
        current = (await c.execute(CURRENT, {"ids": ids})).all()
        display = (await c.execute(DISPLAY, {"ids": ids})).all()
    assert [row.job_id for row in current] == [real.job_id]
    assert [row.job_id for row in display] == [real.job_id]


# A result collects score instances over time (§11.11): every scope or det-facts change supersedes one and
# writes a new current one, and a pending scope's precompute adds a current one under that scope. One round
# writes an instance for every result in turn, so a result's instances lie scattered over the table, as months
# of rounds leave them.
_SCOPES = sa.text(
    "INSERT INTO scope_versions (scope_hash, payload) "
    "SELECT sha256(convert_to('m3-plan-' || g, 'UTF8')), '{}' FROM generate_series(1, 20) AS g RETURNING id"
)
_ROUND = sa.text(
    "INSERT INTO det_results (llm_result_id, scope_version_id, job_id, det_algorithm_version, is_current, "
    "superseded_at, det_criteria, knockouts, industry_match, caps_applied, score_low, score_mid, "
    "score_high, coverage, confidence, det_inputs, det_inputs_hash) "
    "SELECT r.id, :scope, r.job, 'det-test', :current, CASE WHEN :current THEN NULL ELSE now() END, "
    "'{}', '{}', 'neutral', '{}', 40, 50, 60, 0.9, 'high', '{}', '\\x00' "
    "FROM unnest(CAST(:results AS bigint[]), CAST(:jobs AS bigint[])) AS r(id, job)"
)
SUPERSEDED_ROUNDS = 150  # the planner prefers det_results_current from about 60 on (PG17)


async def test_one_jobs_current_score_is_read_through_the_two_indexes(fresh_pg_server, make_config) -> None:
    """The lookup of one job's current score finds the target by its unique key and the instance by the
    partial `det_results_current`, so it stays one index probe however many superseded instances a result
    collects (`det_results_result`, the Deviation 2 index of the cascade, would read them all). 30 scored jobs
    whose results have 150 superseded instances and current ones under 21 scopes each, analysed as the
    migrator (the owner may ANALYZE). On a server of its own: ANALYZE writes `pg_class` statistics in
    place, which no rollback takes back, and the rows leave heap pages, so the shared server's planner
    estimates would change for every later test."""
    await run_migrate_async(make_config("migrate", fresh_pg_server))
    engine = make_null_engine(fresh_pg_server.dsn("migrator", driver="sqlalchemy"))
    try:
        async with engine.connect() as c:
            chain = await activate(c)
            active = (await active_ids(c)).scope_version_id
            jobs = [await insert_job(c) for _ in range(30)]
            results = [(await insert_published(c, job, chain.llm_tuple_id)).result_id for job in jobs]
            rows = {"results": results, "jobs": [job.job_id for job in jobs]}
            for _ in range(SUPERSEDED_ROUNDS):
                await c.execute(_ROUND, {**rows, "scope": active, "current": False})
            for scope in [*(await c.execute(_SCOPES)).scalars(), active]:
                await c.execute(_ROUND, {**rows, "scope": scope, "current": True})
            await c.execute(sa.text("ANALYZE jobs, active_config, score_targets, llm_results, det_results"))
            await c.execute(sa.text("SET LOCAL enable_seqscan = off"))  # 30 targets fit one page
            explain = sa.text("EXPLAIN SELECT * FROM current_scores WHERE job_id = :j")
            plan = "\n".join((await c.execute(explain, {"j": jobs[15].job_id})).scalars())
    finally:
        await engine.dispose()
    assert "score_targets_job_revision_id_llm_tuple_id_key" in plan, plan
    assert "det_results_current" in plan, plan


VIEW_DEFINITIONS = (
    "SELECT viewname, definition FROM pg_views WHERE viewname IN ('current_scores', 'display_scores') "
    "ORDER BY viewname"
)


def test_the_database_views_are_those_of_views_sql(migrated: PgServer) -> None:
    """`db/views.sql` is the one source of both views: replacing them from it changes nothing, so a later
    `CREATE OR REPLACE` from the file starts from what migration 0008 created."""
    migration = importlib.import_module("jobhunter.db.migrations.versions.0008_m3_scoring")
    statements = migration.view_statements()
    names = [re.match(r"CREATE OR REPLACE VIEW (\w+) AS\b", statement) for statement in statements]
    assert [name.group(1) if name else None for name in names] == ["current_scores", "display_scores"]
    with psycopg.connect(migrated.dsn("migrator")) as conn:
        before = conn.execute(VIEW_DEFINITIONS).fetchall()
        for statement in statements:
            conn.execute(statement)
        assert conn.execute(VIEW_DEFINITIONS).fetchall() == before
        conn.rollback()
