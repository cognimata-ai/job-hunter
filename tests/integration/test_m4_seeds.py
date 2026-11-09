"""scripts/m4_seeds.py (plan M4 Task 32), on a collection of the fixtures in process (as
`test_collect_end_to_end.py` runs it) against the migrated test database. Each seed runs through a `run_sql`
executor on the test server's bootstrap (superuser) role, which `SET LOCAL session_replication_role` needs,
printing what `psql -At` prints (`superuser_sql`).

- the rejected item: two calls give the same ids and one `rejected` item, listed replayable by Needs
  attention; the replay operation (`items.replay_rejected` with the spec adapters, then
  `identity.resolve_item`) links it;
- the merge pair: two calls give one group; `groups.confirm_group` merges it; a seed after that confirm
  returns `None` and makes no group;
- the grounded answers (T8's sample off, `activate_test_policy(t8_sample_pct=0)`, so only T1 can send the
  draft to the critic): each answer of `grounded_answers` registered unchanged under its key (the keys are
  canonical, so they match the gateway's), one Score now cycle publishes the collected Greenhouse job
  `contested` with three critic notes (the first with T1), the disputed criterion's levels 3, 2, 2 over the
  drafts, and description evidence whose spans slice to the quotes; E2E setup's step-9 check of the job
  detail passes. Without the overlong summary (`trigger_critic=False`) no trigger fires: `accepted`, no
  critic note.

Every datum is a scrubbed fixture or invented (`SYNTHETIC_CV`).
"""

import dataclasses
import importlib.util
import json
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import httpx
import psycopg
import pytest
import sqlalchemy as sa
from jobhunter.dedup import groups
from jobhunter.graphs.score import graph as score_graph
from jobhunter.llm import fake
from jobhunter.sources import replay
from jobhunter.worker.executions import ExecutionRegistry
from jobhunter.worker.operations import execute_operation
from jobhunter.worker.tasks import defer_in_tx
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.collect import run_to_the_end
from tests.support.pg import PgServer
from tests.support.profile import idem, make_ready_draft
from tests.support.scoring import activate_test_policy, cycle_runner
from tests.support.settings import fresh_key

pytestmark = pytest.mark.integration

SCRIPTS: Final = Path(__file__).resolve().parents[2] / "scripts"
GETYOURGUIDE: Final = ("greenhouse", "getyourguide")


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """scripts/e2e_setup.py with `m4_seeds` and the smoke modules beside it, as it runs as a script; no cached
    copy of any stays behind (`test_smoke_m3_script.import_smoke`)."""
    monkeypatch.syspath_prepend(str(SCRIPTS))
    for name in ("smoke_m0", "smoke_m1", "smoke_m2", "smoke_m3", "m4_seeds"):
        monkeypatch.delitem(sys.modules, name, raising=False)
        monkeypatch.setitem(sys.modules, name, load(name))
    return load("e2e_setup")


@pytest.fixture
def seeds(setup: ModuleType) -> ModuleType:
    module: ModuleType = sys.modules["m4_seeds"]
    return module


def _psql_text(value: object) -> str:
    """A field as `psql -At` prints it."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "t" if value else "f"
    if isinstance(value, dict | list):
        return json.dumps(value)
    return str(value)


def superuser_sql(server: PgServer) -> Callable[[str], str]:
    """A `run_sql` on the bootstrap role: each statement's rows (`|`-joined) or its command tag, as `psql
    -At -c` prints a script. Without parameters psycopg sends the script as one simple query."""

    def run(statement: str) -> str:
        with psycopg.connect(server.dsn("superuser"), autocommit=True) as conn:
            cursor = conn.execute(statement.encode("utf-8"))
            out: list[str] = []
            while True:
                if cursor.description is None:
                    out.append(cursor.statusmessage or "")
                else:
                    out.extend("|".join(_psql_text(v) for v in row) for row in cursor.fetchall())
                if not cursor.nextset():
                    return "\n".join(out)

    return run


async def scalar(engine: AsyncEngine, statement: str, **params: object) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(sa.text(statement), params)).scalar_one()


async def rows(engine: AsyncEngine, statement: str, **params: object) -> list[sa.Row[Any]]:
    async with engine.connect() as conn:
        return list((await conn.execute(sa.text(statement), params)).all())


async def one(engine: AsyncEngine, statement: str, **params: object) -> tuple[Any, ...]:
    """The one row of `statement`, as a tuple."""
    async with engine.connect() as conn:
        return tuple((await conn.execute(sa.text(statement), params)).one())


# ---- the rejected item -------------------------------------------------------------------------------------


async def test_seed_rejected_item_is_replayable_and_idempotent(
    engine, current_epoch, fake_http, fake_embedder, collect_deps, migrated, logged_in_client, seeds
) -> None:
    await run_to_the_end(engine, collect_deps, companies=(), enabled=("arbeitnow",))
    run_sql = superuser_sql(migrated)
    response, item = seeds.seed_rejected_item(run_sql)
    assert seeds.seed_rejected_item(run_sql) == (response, item)
    found = await rows(
        engine,
        "SELECT id, raw_response_id, item_index, disposition, reason_code FROM raw_items "
        "WHERE disposition = 'rejected'",
    )
    assert [tuple(r) for r in found] == [(item, response, 0, "rejected", "identity_error")]
    copied = await one(
        engine,
        "SELECT f.attempt_seq, f.status, f.spec ->> 'm4_seed' AS seed, r.body_ref_id IS NOT NULL AS ref, "
        "r.body_sha256 = o.body_sha256 AS same_body FROM raw_responses r "
        "JOIN fetch_requests f ON f.id = r.fetch_request_id AND f.raw_response_id = r.id "
        "JOIN raw_responses o ON o.id = r.body_ref_id WHERE r.id = :r",
        r=response,
    )
    assert tuple(copied) == (8, "succeeded", "true", True, True)
    attention = await logged_in_client.get("/api/system/needs-attention")
    assert attention.status_code == 200, attention.text
    listed = [i for i in attention.json()["raw_items"]["items"] if i["raw_response_id"] == response]
    assert [(i["rejected"], i["reasons"], i["replayable"]) for i in listed] == [(1, ["identity_error"], True)]

    async with engine.begin() as conn:
        operation = await replay.request_replay(conn, raw_item_ids=[item], defer=defer_in_tx)
    await execute_operation(engine, operation, epoch=current_epoch, registry=ExecutionRegistry())
    status, progress = await one(engine, "SELECT status, progress FROM operations WHERE id = :o", o=operation)
    assert (status, progress) == ("succeeded", {"asked": 1, "moved": 1, "resolved": 1})
    disposition, observation = await one(
        engine, "SELECT disposition, observation_id FROM raw_items WHERE id = :i", i=item
    )
    assert disposition == "linked" and observation is not None


async def test_seed_rejected_item_inserts_nothing_without_an_item_0(
    engine, current_epoch, fake_http, fake_embedder, collect_deps, migrated, seeds
) -> None:
    """The newest Arbeitnow page-1 response without an item 0 (moved to another index, as a page whose first
    element was never stored): the seed writes no fetch request, response or item, and fails naming that
    page."""
    await run_to_the_end(engine, collect_deps, companies=(), enabled=("arbeitnow",))
    run_sql = superuser_sql(migrated)
    newest = await scalar(
        engine,
        "SELECT r.id FROM fetch_requests f JOIN raw_responses r ON r.id = f.raw_response_id "
        "JOIN source_instances s ON s.id = f.source_instance_id WHERE s.source_id = 'arbeitnow' "
        "AND f.status = 'succeeded' AND f.operation = 'list' AND f.page_no = 1 "
        "ORDER BY r.fetched_at DESC, f.id DESC LIMIT 1",
    )
    async with engine.begin() as conn:
        await conn.execute(
            sa.text("UPDATE raw_items SET item_index = 999 WHERE raw_response_id = :r AND item_index = 0"),
            {"r": newest},
        )
    counted = (
        "SELECT (SELECT count(*) FROM fetch_requests), (SELECT count(*) FROM raw_responses), "
        "(SELECT count(*) FROM raw_items)"
    )
    before = await one(engine, counted)
    with pytest.raises(sys.modules["smoke_m0"].CheckFailed, match=f"response {newest} has no item 0"):
        seeds.seed_rejected_item(run_sql)
    assert await one(engine, counted) == before
    assert await scalar(engine, "SELECT count(*) FROM fetch_requests WHERE spec ? 'm4_seed'") == 0


# ---- the merge pair ----------------------------------------------------------------------------------------


# A company key that sorts before every collected one, given to an Adzuna job and another job below.
ADZUNA_FIRST: Final = "0000-e2e-adzuna-first"
_ELIGIBLE_COMPANIES: Final = (
    "SELECT j.company_key FROM jobs j WHERE j.merged_into_job_id IS NULL "
    "AND j.current_revision_id IS NOT NULL AND NOT j.synthetic_eval AND j.duplicate_group_id IS NULL "
    "GROUP BY 1 HAVING count(*) >= 2 ORDER BY 1 LIMIT 1"
)


async def test_seed_merge_pair_is_idempotent_and_confirmable(
    engine, current_epoch, fake_http, fake_embedder, collect_deps, migrated, seeds
) -> None:
    # Adzuna is read on the fixtures (a quota source: a free run reads it too); one of its ungrouped jobs and
    # an Arbeitnow job are given the company key that sorts first, which the seed must pass over.
    await run_to_the_end(
        engine,
        collect_deps,
        queries=("software engineer",),
        companies=(GETYOURGUIDE,),
        enabled=("adzuna", "arbeitnow", "ats"),
    )
    run_sql = superuser_sql(migrated)
    adzuna_job, other_job = await one(
        engine,
        "SELECT (SELECT min(id) FROM jobs WHERE 'adzuna' = ANY(source_ids) AND duplicate_group_id IS NULL), "
        "(SELECT min(id) FROM jobs WHERE 'arbeitnow' = ANY(source_ids) AND NOT 'adzuna' = ANY(source_ids))",
    )
    assert adzuna_job is not None and other_job is not None
    async with engine.begin() as conn:
        await conn.execute(
            sa.text("UPDATE jobs SET company_key = :k WHERE id IN (:a, :b)"),
            {"k": ADZUNA_FIRST, "a": adzuna_job, "b": other_job},
        )
    assert await scalar(engine, _ELIGIBLE_COMPANIES) == ADZUNA_FIRST  # it would be chosen but for Adzuna
    before = await scalar(engine, "SELECT count(*) FROM duplicate_groups")
    seeded = seeds.seed_merge_pair(run_sql)
    assert seeded is not None and seeds.seed_merge_pair(run_sql) == seeded
    group, representative, member = seeded
    assert not {representative, member} & {adzuna_job, other_job}
    assert await scalar(engine, "SELECT count(*) FROM duplicate_groups") == before + 1
    (made,) = await rows(
        engine,
        "SELECT g.status, g.election_reason, g.representative_job_id, "
        "array_agg(m.job_id ORDER BY m.job_id) AS members, "
        "bool_and(m.evidence = '{\"e2e_seed\": true}') AS marked, "
        "(SELECT array_agg(id ORDER BY id) FROM jobs WHERE duplicate_group_id = g.id) AS grouped "
        "FROM duplicate_groups g JOIN duplicate_group_members m ON m.group_id = g.id WHERE g.id = :g "
        "GROUP BY g.id",
        g=group,
    )
    pair = sorted((representative, member))
    assert tuple(made) == ("active", "initial", representative, pair, True, pair)
    companies, adzuna = await one(
        engine,
        "SELECT count(DISTINCT company_key), "
        "count(*) FILTER (WHERE 'adzuna' = ANY(source_ids)) FROM jobs "
        "WHERE id = ANY(CAST(:ids AS bigint[]))",
        ids=pair,
    )
    assert (companies, adzuna) == (1, 0)
    lengths = dict(
        tuple(r)
        for r in await rows(
            engine,
            "SELECT j.id, length(r.description_text) FROM jobs j JOIN job_revisions r "
            "ON r.id = j.current_revision_id WHERE j.id = ANY(CAST(:ids AS bigint[]))",
            ids=pair,
        )
    )
    assert lengths[representative] >= lengths[member]

    async with engine.begin() as conn:
        assert await groups.confirm_group(conn, group) == representative
    (event,) = await rows(
        engine, "SELECT kind, from_job_id, into_job_id, reversed_by_event_id FROM merge_events"
    )
    assert tuple(event) == ("user_confirmed_merge", member, representative, None)
    count = await scalar(engine, "SELECT count(*) FROM duplicate_groups")
    assert seeds.seed_merge_pair(run_sql) is None  # seed -> confirm -> seed: nothing new
    assert await scalar(engine, "SELECT count(*) FROM duplicate_groups") == count


# ---- the grounded answers ----------------------------------------------------------------------------------


async def grounded_target(
    setup: ModuleType,
    seeds: ModuleType,
    engine: AsyncEngine,
    client: httpx.AsyncClient,
    op_runner: Any,
    upload: Any,
    collect_deps: Any,
    run_sql: Callable[[str], str],
    *,
    trigger_critic: bool,
) -> tuple[int, int, list[str]]:
    """The Greenhouse board collected, `SYNTHETIC_CV`'s profile confirmed, and E2E setup's grounded job (the
    lowest job id with a pending target and three sentences) given its answers and Score now; `(job_id,
    target_id, quotes)`."""
    op_runner.deps = dataclasses.replace(op_runner.deps, embedder=collect_deps.embedder)
    async with engine.begin() as conn:
        await activate_test_policy(conn, t8_sample_pct=0)
    await run_to_the_end(engine, collect_deps, companies=(GETYOURGUIDE,), enabled=("ats",))
    draft_id = await make_ready_draft(client, op_runner, upload)
    confirmed = await client.post(
        f"/api/profile/drafts/{draft_id}/confirm",
        json={"expected_rescore_ack": True},
        headers=idem(fresh_key("confirm")),
    )
    assert confirmed.status_code == 201, (confirmed.status_code, confirmed.text)
    await op_runner.run_pending()  # the profile's vectors and the targets
    candidates = json.loads(run_sql(setup.GROUNDED_CANDIDATES))
    chosen = next(
        c for c in candidates if c["state"] == "pending" and setup.quotes_of(c["description"]) is not None
    )
    job_id = int(chosen["job_id"])
    revision, description, skill = seeds.grounded_inputs(job_id, run_sql)
    assert skill == "Python"  # the synthetic stream's first skill
    answers = seeds.grounded_answers(revision, description, skill, trigger_critic=trigger_critic)
    purposes = ("generate", "critic", "revise") if trigger_critic else ("generate",)
    expected = {
        fake.answer_key(purpose, {"job_revision_id": revision}, n)
        for purpose, rounds in (("generate", (0,)), ("critic", (1, 2, 3)), ("revise", (1, 2)))
        if purpose in purposes
        for n in rounds
    }
    assert set(answers) == expected
    for key, body in answers.items():
        fake.answer(key, body)
    moved = await client.post(f"/api/jobs/{job_id}/score-now", json={}, headers=idem(fresh_key("score-now")))
    assert moved.status_code == 202, (moved.status_code, moved.text)
    runner = await cycle_runner(engine, op_runner.deps.gateway, epoch=op_runner.epoch)
    cycle = await score_graph.execute_cycle(
        runner.graphs,
        runner.deps,
        trigger="score_now",
        epoch=op_runner.epoch,
        fake_llm=True,
        admission_stopped=lambda: False,
    )
    assert cycle is not None
    return job_id, int(moved.json()["target_id"]), seeds.grounded_quotes(description)


async def test_grounded_answers_publish_contested_with_description_spans(
    engine, logged_in_client, op_runner, upload, fake_llm, collect_deps, fake_embedder, migrated, setup, seeds
) -> None:
    assert (len(seeds.LONG_SUMMARY.split()), len(seeds.LONG_SUMMARY) < 450) == (61, True)
    run_sql = superuser_sql(migrated)
    job_id, target, quotes = await grounded_target(
        setup, seeds, engine, logged_in_client, op_runner, upload, collect_deps, run_sql, trigger_critic=True
    )
    state, result = await one(
        engine, "SELECT state, published_result_id FROM score_targets WHERE id = :t", t=target
    )
    assert state == "published_contested"
    notes = await rows(
        engine,
        "SELECT n.round, n.triggers, n.derived_outcome FROM critic_notes n JOIN score_attempts a "
        "ON a.id = n.attempt_id WHERE a.target_id = :t ORDER BY n.round",
        t=target,
    )
    assert [(n.round, n.derived_outcome) for n in notes] == [(1, "revise"), (2, "revise"), (3, "revise")]
    assert "T1" in notes[0].triggers
    levels = await rows(
        engine,
        "SELECT d.round, (d.output_json -> 'criteria' -> :c ->> 'level')::int AS level FROM score_drafts d "
        "JOIN score_attempts a ON a.id = d.attempt_id WHERE a.target_id = :t ORDER BY d.round",
        t=target,
        c=seeds.DISPUTED_CRITERION,
    )
    assert [(r.round, r.level) for r in levels] == [(0, 3), (1, 2), (2, 2)]
    spans = await rows(
        engine,
        "SELECT e.quote, substr(v.description_text, e.span_start + 1, e.span_end - e.span_start) AS spanned "
        "FROM result_evidence e JOIN llm_results l ON l.id = e.llm_result_id "
        "JOIN job_revisions v ON v.id = l.job_revision_id "
        "WHERE e.llm_result_id = :r AND e.source_kind = 'description'",
        r=result,
    )
    assert len(spans) >= 3 and all(s.quote == s.spanned for s in spans)
    assert {s.quote for s in spans} == set(quotes)
    detail = await logged_in_client.get(f"/api/jobs/{job_id}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert setup.grounded_problem(body) is None
    assert len(body["contested"]["rejected_drafts"]) == 2


async def test_a_clean_grounded_answer_publishes_accepted_without_a_critic(
    engine, logged_in_client, op_runner, upload, fake_llm, collect_deps, fake_embedder, migrated, setup, seeds
) -> None:
    assert len(seeds.CLEAN_SUMMARY.split()) == 40
    _, target, _ = await grounded_target(
        setup,
        seeds,
        engine,
        logged_in_client,
        op_runner,
        upload,
        collect_deps,
        superuser_sql(migrated),
        trigger_critic=False,
    )
    state, outcome = await one(
        engine,
        "SELECT t.state, r.critic_outcome FROM score_targets t JOIN llm_results r "
        "ON r.id = t.published_result_id WHERE t.id = :t",
        t=target,
    )
    assert (state, outcome) == ("published_accepted", "not_run")
    notes = await scalar(
        engine,
        "SELECT count(*) FROM critic_notes n JOIN score_attempts a ON a.id = n.attempt_id "
        "WHERE a.target_id = :t",
        t=target,
    )
    assert notes == 0
