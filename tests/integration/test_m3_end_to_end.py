"""Task 28: M3 acceptance in process, on the fake LLM (keyed answers and the fake Message Batches API), the
fake embedder and the api.

`test_scoring_end_to_end_on_the_fake_llm` drives the score lane through every M3 path once:
1. jobs of synthetic ads, then a pasted CV through the profile flow and the confirm: the confirm's
   `create_targets_for_tuple` gives each retained current revision a target of the new tuple, and the
   `awaiting_embedding` pass sets their similarity once `embed_profile` has written the profile's vectors;
2. a periodic cycle (`tick.tick_score`, then the `score_cycle` task's body, `graph.execute_cycle`) publishes
   with the full routing: a clean draft without a critic, a required certification (T5) whose critic
   accepts, one whose critic asks for a revision that it then accepts, and one contested after two
   revisions; every published result has its current `det_results` instance for the active scope;
3. a scope save (a location and a radius) and its apply: the precompute, the final check and the flip, and
   the target moves (the far job's unpublished target `filtered_out`); then Score now with `force` on that
   target publishes it with `override = 'score_anyway'`;
4. feedback on a published job and its golden case; then the adversarial ten and 30 synthetic labelled cases
   (Ruling T28-a: the gate's 30 are the user's, Ruling T20-b) and a golden eval run on the fake, whose
   test-retest sample is scored twice: the gate `passed`. The two adversarial fixtures that expect a
   knockout are answered with the fact their ad states, confirmed by the critic, so the recall is whole;
5. a second confirm: the old tuple's unpublished work cancelled, its bulk operation with it, the new one
   opened, `create_targets_for_tuple` and its `batch_submit`, a 250-item batch on the fake, `reconciled` with
   its decisions (248 published, 2 follow-ups), and the follow-up cycle;
6. `budget_counters` and `recompute` clean, every bulk operation within its budget, the retention pass
   deleting nothing of the active tuple, and no marker word of an ad, a draft or the CV in a log line, a
   checkpoint row (the cycles checkpoint on PostgreSQL), a cycle report or an operation payload.

Every request goes through the api (`logged_in_client`) and every operation runs in process (`op_runner`).
Two targets wait out a backoff set by hand (as `fail`'s transient path leaves one), so that one is still
unpublished at the scope save and one at the second confirm. Every datum is invented: `SYNTHETIC_CV`, the
synthetic ads of `tests.support.validation` and the adversarial fixtures, and bodies built here; no paid call.
"""

import dataclasses
import json
from collections import Counter
from collections.abc import AsyncIterator, Mapping
from datetime import timedelta
from typing import Any, Final

import httpx
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.embeddings import EmbedderClient, make_http_client
from jobhunter.graphs.checkpointer import open_saver
from jobhunter.graphs.score import graph as score_graph
from jobhunter.llm import budget, spend_ledger
from jobhunter.llm.schemas.critic import CriticOut
from jobhunter.llm.schemas.profile import ConfirmedProfile
from jobhunter.privacy.retention import run_retention_daily
from jobhunter.profile.storage import CvStore
from jobhunter.settings import validate
from jobhunter.worker import tick
from jobhunter.worker.embedding_reconcile import reconcile_awaiting_embedding
from jobhunter.worker.tasks import defer_in_tx
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.batches import decisions, poll_once, scripted_batches, submission_rows, submission_state
from tests.support.collect import ctx
from tests.support.eval import (
    CaseRef,
    eval_critic_body,
    eval_deps,
    eval_generator_body,
    execute,
    fake_eval_answers,
    insert_cases,
    run_row,
    seed_adversarial,
    start_eval,
)
from tests.support.llm import FakeLLM, score_profile
from tests.support.m3 import Job
from tests.support.pg import PgServer
from tests.support.profile import SYNTHETIC_CV, draft_row, idem
from tests.support.reconcile import run_reconciler
from tests.support.scoring import (
    activate_test_policy,
    cycle_runner,
    insert_score_job,
    place_far,
    review_item,
    synthetic_review,
)
from tests.support.settings import active_scope_id, fresh_key, pending_scope_id, save_scope, scope_ids_of
from tests.support.validation import DESCRIPTION, fact_item, synthetic_ad

pytestmark = pytest.mark.integration

CERTIFICATION: Final = "certification:aws solutions architect"
CERT_QUOTE: Final = "AWS Solutions Architect erforderlich"
PLAIN_AD: Final = synthetic_ad()
CERT_AD: Final = synthetic_ad(description=f"{DESCRIPTION} Zertifizierung {CERT_QUOTE}.")
CERT_FACT: Final = fact_item("certification", "AWS Solutions Architect", CERT_QUOTE, required=True)
CONFIRMED: Final = review_item(CERTIFICATION, "fact_confirmed", CERT_QUOTE)
# A grounded dispute of `role_fit`: its quote lies in the ad's description.
DISPUTE: Final = review_item("role_fit", "level_too_low", "Backend Engineer (m/w/d)", suggested_level=4)
LATER: Final = timedelta(minutes=11)  # past the `awaiting_embedding` reconciler's grace
RADIUS_KM: Final = 40
GRADED: Final = 30  # the user's cases of the gate (Ruling T28-a)
BATCH_ITEMS: Final = 250
# Words of the ads, the drafts and the reviews (and of the CV) that only the score lane's and the profile's
# own tables may hold, compared case-folded.
MARKERS: Final = (
    "Du entwickelst Services",  # the synthetic ad's description
    "Solutions Architect",  # the certification the ads require and the drafts assert
    "Sicherheitsüberprüfung",  # an adversarial fixture's ad
    "Synthetic rationale",  # every draft's criterion rationale
    "A synthetic summary",  # every draft's summary
    "Eine erfundene Zusammenfassung",
    "Musterfrau",  # the CV's candidate
    "Datenplattform",
)
ADDED_TITLE: Final = {"title": "Platform Engineer", "lang": "en"}
LEVELS: Final = {"poor": 1, "ok": 2, "good": 3}


async def rows(engine: AsyncEngine, statement: str, **params: object) -> list[sa.Row[Any]]:
    async with engine.connect() as conn:
        return list((await conn.execute(sa.text(statement), params)).all())


async def scalar(engine: AsyncEngine, statement: str, **params: object) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(sa.text(statement), params)).scalar_one()


def draft_body(
    ad_text: str,
    profile: ConfirmedProfile,
    *,
    levels: dict[str, int] | None = None,
    facts: tuple[Any, ...] = (),
) -> dict[str, Any]:
    """A candidate C generator answer grounded in `ad_text` and `profile`: every criterion at level 3
    (`levels` replace some), with `facts`."""
    body = eval_generator_body("e2e", ad_text, profile, level=3)
    for criterion in body["criteria"]:
        criterion["level"] = (levels or {}).get(criterion["criterion"], criterion["level"])
    body["facts"] = list(facts)
    return body


async def confirmed_draft(client: httpx.AsyncClient, op_runner: Any, upload: Any, engine: AsyncEngine) -> int:
    """`SYNTHETIC_CV` pasted, its text approved and its profile extracted (the committed `extract_profile`
    stream): the draft's id, `ready`."""
    draft_id = int(await upload(SYNTHETIC_CV))
    await op_runner.run_pending()
    r = await client.post(f"/api/profile/drafts/{draft_id}/text-review", headers=idem(fresh_key("approve")))
    assert r.status_code == 202, (r.status_code, r.text)
    await op_runner.run_pending()
    assert (await draft_row(engine, draft_id)).status == "ready"
    return draft_id


async def confirm(client: httpx.AsyncClient, draft_id: int) -> dict[str, Any]:
    r = await client.post(
        f"/api/profile/drafts/{draft_id}/confirm",
        json={"expected_rescore_ack": True},
        headers=idem(fresh_key("confirm")),
    )
    assert r.status_code == 201, (r.status_code, r.text)
    body: dict[str, Any] = r.json()
    return body


async def profile_of(engine: AsyncEngine, version_id: int) -> ConfirmedProfile:
    found = await scalar(engine, "SELECT profile_json FROM profile_versions WHERE id = :v", v=version_id)
    return ConfirmedProfile.model_validate(found)


async def targets_of(engine: AsyncEngine, tuple_id: int) -> dict[int, sa.Row[Any]]:
    """The tuple's targets by job id."""
    found = await rows(
        engine,
        "SELECT id, job_id, job_revision_id, state, state_reason, similarity, override, published_result_id, "
        "lane, filter_scope_version_id, bulk_operation_id FROM score_targets WHERE llm_tuple_id = :t "
        "ORDER BY id",
        t=tuple_id,
    )
    return {int(r.job_id): r for r in found}


def label_of(i: int, n: int) -> str:
    return ("poor", "ok", "good")[i * 3 // n]


async def graded_cases(engine: AsyncEngine, fake_llm: FakeLLM, n: int) -> list[CaseRef]:
    """`n` `week1` cases of the synthetic ad, labelled `poor`, `ok` and `good` in thirds by id, the fake's
    generator answering each at the level of its label (1, 2, 3) and the critic accepting, so the scores
    follow the labels (as `test_eval_gate.py` grades its set)."""
    made = (await insert_cases(engine, n))[-n:]
    ad, scorer = synthetic_ad(), score_profile()
    async with engine.begin() as conn:
        for i, case in enumerate(made):
            await conn.execute(
                sa.text("UPDATE golden_cases SET label = :l WHERE id = :g"),
                {"l": label_of(i, n), "g": case.id},
            )
    for i, case in enumerate(made):
        body = eval_generator_body("graded", ad.description_text, scorer, level=LEVELS[label_of(i, n)])
        fake_llm.answer("generate", case.job_revision_id, body)
        for round_ in (1, 2):
            fake_llm.answer("revise", case.job_revision_id, body, round=round_)
        for round_ in (1, 2, 3):
            fake_llm.answer(
                "critic", case.job_revision_id, eval_critic_body("graded"), round=round_, schema=CriticOut
            )
    return made


# The adversarial fixtures that expect a knockout (`expected_knockouts`), each with the fact its ad states:
# the generator's wire item and the critic's confirmation of it (a binding fact is T5: the critic reviews it).
KNOCKOUT_FACTS: Final = {
    "clearance_required": (
        "security_clearance:clearance",
        fact_item(
            "security_clearance",
            "required",
            "Sicherheitsüberprüfung nach dem Sicherheitsüberprüfungsgesetz (Ü2)",
            qualifier="Ü2",
            required=True,
        ),
    ),
    "english_ad_german_c1": (
        "mandatory_language:de",
        fact_item(
            "required_language", "de", "Fluent German at level C1 is mandatory", qualifier="C1", required=True
        ),
    ),
}
_CASE_INPUTS: Final = (
    "SELECT g.id, g.job_revision_id, r.description_text, v.profile_json FROM golden_cases g "
    "JOIN job_revisions r ON r.id = g.job_revision_id JOIN profile_versions v ON v.id = g.profile_version_id "
    "WHERE g.id = ANY(:ids)"
)


async def knockout_answers(engine: AsyncEngine, fake_llm: FakeLLM, keys: dict[int, str]) -> None:
    """The answers of the two knockout fixtures (`KNOCKOUT_FACTS`), replacing `fake_eval_answers`' generic
    ones, which assert no fact: the generator states the fact its ad states, and every critic round confirms
    it, so the expected knockout applies."""
    by_case = {case_id: key for case_id, key in keys.items() if key in KNOCKOUT_FACTS}
    assert sorted(by_case.values()) == sorted(KNOCKOUT_FACTS)
    for case in await rows(engine, _CASE_INPUTS, ids=list(by_case)):
        fact_id, item = KNOCKOUT_FACTS[by_case[int(case.id)]]
        profile = ConfirmedProfile.model_validate(case.profile_json)
        body = eval_generator_body("knockout", case.description_text, profile, level=1)
        body["facts"] = [item]
        quote = item["evidence"][0]
        fake_llm.answer("generate", case.job_revision_id, body)
        for round_ in (1, 2):
            fake_llm.answer("revise", case.job_revision_id, body, round=round_)
        review = synthetic_review(review_item(fact_id, "fact_confirmed", quote))
        for round_ in (1, 2, 3):
            fake_llm.answer("critic", case.job_revision_id, review, round=round_, schema=CriticOut)


async def retained(conn: Any) -> int:
    """The current revisions of the retained, unmerged, non-synthetic jobs (what `create_targets_for_tuple`
    covers)."""
    found = await conn.execute(
        sa.text(
            "SELECT count(*) FROM jobs WHERE merged_into_job_id IS NULL AND current_revision_id IS NOT NULL "
            "AND NOT synthetic_eval"
        )
    )
    return int(found.scalar_one())


def graded_jobs(cases: list[CaseRef]) -> list[Job]:
    return [Job(case.job_id, case.job_revision_id) for case in cases]


ACTIVE_TUPLE_ROWS: Final = {
    "score_targets": "SELECT count(*) FROM score_targets WHERE llm_tuple_id = :t",
    "score_attempts": "SELECT count(*) FROM score_attempts a JOIN score_targets t ON t.id = a.target_id "
    "WHERE t.llm_tuple_id = :t",
    "score_drafts": "SELECT count(*) FROM score_drafts d JOIN score_attempts a ON a.id = d.attempt_id "
    "JOIN score_targets t ON t.id = a.target_id WHERE t.llm_tuple_id = :t",
    "llm_results": "SELECT count(*) FROM llm_results r JOIN score_targets t ON t.id = r.target_id "
    "WHERE t.llm_tuple_id = :t",
    "det_results": "SELECT count(*) FROM det_results d JOIN llm_results r ON r.id = d.llm_result_id "
    "JOIN score_targets t ON t.id = r.target_id WHERE t.llm_tuple_id = :t",
    "batch_items": "SELECT count(*) FROM batch_items i JOIN score_targets t ON t.id = i.target_id "
    "WHERE t.llm_tuple_id = :t",
}


async def active_tuple_rows(engine: AsyncEngine, tuple_id: int) -> dict[str, int]:
    """How many rows of each score-lane table belong to the tuple."""
    return {table: int(await scalar(engine, sql, t=tuple_id)) for table, sql in ACTIVE_TUPLE_ROWS.items()}


def as_text(row: Mapping[Any, Any]) -> str:
    """Every column of a row as one case-folded text (bytes decoded leniently, the rest as JSON)."""
    parts = []
    for value in row.values():
        if isinstance(value, bytes | memoryview):
            parts.append(bytes(value).decode("utf-8", errors="replace"))
        else:
            parts.append(json.dumps(value, default=str, ensure_ascii=False))
    return "\x1f".join(parts).casefold()


async def outside_the_lane(engine: AsyncEngine, log_events: Any) -> dict[str, list[str]]:
    """The logged events, every checkpoint row, every cycle report and every operation payload, each as
    case-folded text."""
    found: dict[str, list[str]] = {
        "logs": [json.dumps(e, ensure_ascii=False).casefold() for e in log_events()]
    }
    async with engine.connect() as conn:
        for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            result = await conn.execute(sa.text(f"SELECT * FROM {table}"))  # noqa: S608 - constant names
            found[table] = [as_text(row._mapping) for row in result]
        result = await conn.execute(sa.text("SELECT report FROM score_cycles"))
        found["score_cycles.report"] = [as_text(row._mapping) for row in result]
        result = await conn.execute(sa.text("SELECT kind, payload FROM operations"))
        found["operations.payload"] = [as_text(row._mapping) for row in result]
    return found


async def current_instances(engine: AsyncEngine, tuple_id: int, scope_version_id: int) -> set[int]:
    """The tuple's published results with a current `det_results` instance for the scope."""
    found = await rows(
        engine,
        "SELECT DISTINCT d.llm_result_id FROM det_results d JOIN llm_results r ON r.id = d.llm_result_id "
        "JOIN score_targets t ON t.id = r.target_id "
        "WHERE t.llm_tuple_id = :t AND d.scope_version_id = :s AND d.is_current",
        t=tuple_id,
        s=scope_version_id,
    )
    return {int(r.llm_result_id) for r in found}


@pytest.fixture
async def saver(migrated: PgServer) -> AsyncIterator[Any]:
    """The worker's checkpointer on the test database (the cycles' threads, and the retention's)."""
    async with open_saver(migrated.dsn("app")) as opened:
        yield opened


async def test_scoring_end_to_end_on_the_fake_llm(
    saver,
    cv_store_dir,
    logged_in_client,
    engine,
    op_runner,
    upload,
    current_epoch,
    fake_llm: FakeLLM,
    collect_deps,
    fake_embedder,
    queue_app,
    log_events,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = logged_in_client
    op_runner.deps = dataclasses.replace(op_runner.deps, embedder=collect_deps.embedder)
    async with engine.begin() as conn:
        await activate_test_policy(conn)  # the seeded thresholds, T8 off (Ruling P48)

    # 1. The jobs, the profile and the confirm: a target of the new tuple for each retained current revision.
    async with engine.begin() as conn:
        plain = await insert_score_job(conn, PLAIN_AD)
        t5, revised, contested = [await insert_score_job(conn, CERT_AD) for _ in range(3)]
        far = await insert_score_job(conn, PLAIN_AD)
        await place_far(conn, far.job_id)  # on site in Leipzig: beyond the radius of the scope saved in 3
        parked = await insert_score_job(conn, PLAIN_AD)  # still pending when the second confirm supersedes it
    jobs: dict[str, Job] = {
        "plain": plain,
        "t5": t5,
        "revised": revised,
        "contested": contested,
        "far": far,
        "parked": parked,
    }
    draft_id = await confirmed_draft(client, op_runner, upload, engine)
    confirmed = await confirm(client, draft_id)
    assert confirmed["targets_to_create"] == len(jobs)
    version_id = int(confirmed["profile_version_id"])
    tuple_id = int(await scalar(engine, "SELECT llm_tuple_id FROM active_config WHERE id = 1"))
    await op_runner.run_pending()  # the profile's vectors, its query proposal and the targets
    (creation,) = await rows(
        engine, "SELECT status, payload FROM operations WHERE kind = 'create_targets_for_tuple'"
    )
    assert creation.status == "succeeded" and creation.payload["llm_tuple_id"] == tuple_id
    created = await targets_of(engine, tuple_id)
    assert {job.job_id for job in jobs.values()} == set(created)
    assert {r.state for r in created.values()} == {"pending"}
    async with make_http_client() as http:
        embedder = EmbedderClient(fake_embedder.base_url, http)
        await reconcile_awaiting_embedding(ctx(engine, queue_app, utcnow() + LATER, embedder=embedder))
    assert all(r.similarity is not None for r in (await targets_of(engine, tuple_id)).values())

    # 2. A periodic cycle with the full routing. The far and the parked jobs' targets wait out a backoff (as
    # `fail`'s transient path leaves one): unpublished at the scope save of 3, and at the second confirm.
    profile = await profile_of(engine, version_id)
    revision = {name: job.revision_id for name, job in jobs.items()}
    fake_llm.answer("generate", revision["plain"], draft_body(PLAIN_AD.description_text, profile))
    certified = draft_body(CERT_AD.description_text, profile, facts=(CERT_FACT,))
    disputed = draft_body(CERT_AD.description_text, profile, levels={"role_fit": 2}, facts=(CERT_FACT,))
    fake_llm.answer("generate", revision["t5"], certified)
    fake_llm.answer("critic", revision["t5"], synthetic_review(CONFIRMED), round=1, schema=CriticOut)
    fake_llm.answer("generate", revision["revised"], disputed)
    asked = synthetic_review(CONFIRMED, DISPUTE, verdict="revise")
    fake_llm.answer("critic", revision["revised"], asked, round=1, schema=CriticOut)
    fake_llm.answer("revise", revision["revised"], certified, round=1)
    fake_llm.answer("critic", revision["revised"], synthetic_review(CONFIRMED), round=2, schema=CriticOut)
    fake_llm.answer("generate", revision["contested"], disputed)
    for critic_round in (1, 2, 3):
        fake_llm.answer("critic", revision["contested"], asked, round=critic_round, schema=CriticOut)
    for revise_round in (1, 2):
        fake_llm.answer("revise", revision["contested"], disputed, round=revise_round)
    async with engine.begin() as conn:
        await conn.execute(
            sa.text(
                "UPDATE score_targets SET not_before = now() + interval '6 hours' "
                "WHERE id = ANY(CAST(:t AS bigint[]))"
            ),
            {"t": [created[far.job_id].id, created[parked.job_id].id]},
        )
    report = await tick.tick_score(
        engine, now=utcnow(), epoch=current_epoch, defer=defer_in_tx, fake_llm=True
    )
    assert report.requested and report.pause_reason is None
    runner = await cycle_runner(engine, op_runner.deps.gateway, epoch=current_epoch)
    deps = dataclasses.replace(runner.deps, saver=saver)  # the cycles checkpoint on PostgreSQL
    runner = dataclasses.replace(runner, deps=deps, graphs=score_graph.build_score_graphs(deps))
    cycle_id = await score_graph.execute_cycle(
        runner.graphs,
        runner.deps,
        trigger="periodic",
        epoch=current_epoch,
        fake_llm=True,
        admission_stopped=lambda: False,
    )
    assert cycle_id is not None
    (cycle,) = await rows(
        engine, "SELECT status, trigger, report FROM score_cycles WHERE id = :c", c=cycle_id
    )
    assert (cycle.status, cycle.trigger) == ("completed", "periodic")
    assert cycle.report["outcomes"] == {"published_accepted": 3, "published_contested": 1}
    scored = await targets_of(engine, tuple_id)
    assert {name: scored[job.job_id].state for name, job in jobs.items()} == {
        "plain": "published_accepted",
        "t5": "published_accepted",
        "revised": "published_accepted",
        "contested": "published_contested",
        "far": "pending",
        "parked": "pending",
    }
    notes = await rows(
        engine,
        "SELECT a.target_id, n.round, n.derived_outcome FROM critic_notes n "
        "JOIN score_attempts a ON a.id = n.attempt_id ORDER BY n.id",
    )
    rounds = {
        name: [(n.round, n.derived_outcome) for n in notes if n.target_id == scored[job.job_id].id]
        for name, job in jobs.items()
    }
    assert rounds == {
        "plain": [],  # no trigger: no critic
        "t5": [(1, "accept")],  # the required certification (T5)
        "revised": [(1, "revise"), (2, "accept")],
        "contested": [(1, "revise"), (2, "revise"), (3, "revise")],
        "far": [],
        "parked": [],
    }
    results = {
        r.id: r
        for r in await rows(
            engine,
            "SELECT r.id, r.status, r.critic_outcome, r.revisions_used FROM llm_results r "
            "JOIN score_targets t ON t.id = r.target_id WHERE t.llm_tuple_id = :t",
            t=tuple_id,
        )
    }
    assert {
        name: (results[scored[job.job_id].published_result_id].revisions_used)
        for name, job in jobs.items()
        if name not in ("far", "parked")
    } == {"plain": 0, "t5": 0, "revised": 1, "contested": 2}
    active_scope = await scalar(engine, "SELECT scope_version_id FROM active_config WHERE id = 1")
    assert await current_instances(engine, tuple_id, active_scope) == set(results)

    # 3. A scope save (Berlin, 40 km) and its apply: the precompute writes an instance of the pending scope
    # for every published result, the final check finds none missing, and the flip moves the far job's
    # unpublished target `filtered_out`. Then Score now with `force` publishes it anyway.
    saved = await save_scope(client, radius_km=RADIUS_KM)
    (new_scope,) = await scope_ids_of(engine, saved)
    assert await pending_scope_id(engine) == new_scope
    await op_runner.run_pending()
    assert (await active_scope_id(engine), await pending_scope_id(engine)) == (new_scope, None)
    (apply,) = await rows(
        engine, "SELECT status, progress FROM operations WHERE id = :o", o=saved["apply_scope_operation_id"]
    )
    assert apply.status == "succeeded" and apply.progress["det_done"] == apply.progress["det_total"] == 4
    assert await current_instances(engine, tuple_id, new_scope) == set(results)
    moved = (await targets_of(engine, tuple_id))[far.job_id]
    assert (moved.state, moved.filter_scope_version_id) == ("filtered_out", new_scope)
    r = await client.post(
        f"/api/jobs/{far.job_id}/score-now", json={"force": True}, headers=idem(fresh_key("score-now"))
    )
    assert r.status_code == 202 and r.json()["target_id"] == moved.id, (r.status_code, r.text)
    fake_llm.answer("generate", revision["far"], draft_body(PLAIN_AD.description_text, profile))
    now_cycle = await score_graph.execute_cycle(
        runner.graphs,
        runner.deps,
        trigger="score_now",
        epoch=current_epoch,
        fake_llm=True,
        admission_stopped=lambda: False,
    )
    assert now_cycle is not None
    forced = (await targets_of(engine, tuple_id))[far.job_id]
    assert (forced.state, forced.override) == ("published_accepted", "score_anyway")
    assert forced.published_result_id in await current_instances(engine, tuple_id, new_scope)

    # 4. Feedback and its golden case; then the adversarial ten and 30 labelled cases (Ruling T28-a) and a
    # golden run on the fake: the test-retest sample is scored twice, and the gate passes.
    r = await client.post(f"/api/jobs/{plain.job_id}/feedback", json={"kind": "label", "label": "good"})
    assert r.status_code == 201, (r.status_code, r.text)
    case_id = int(r.json()["golden_case_id"])
    listed = (await client.get("/api/golden-cases")).json()
    assert [(c["id"], c["job_id"], c["label"], c["source"]) for c in listed["items"]] == [
        (case_id, plain.job_id, "good", "week1")
    ]
    # the run is the seed's: the feedback case leaves it (`PATCH`, the route that sets `active`)
    r = await client.patch(f"/api/golden-cases/{case_id}", json={"active": False})
    assert r.status_code == 200 and r.json()["active"] is False, (r.status_code, r.text)
    seeded = await seed_adversarial(engine, now=utcnow())
    assert seeded.cases == 10
    await knockout_answers(engine, fake_llm, await fake_eval_answers(engine, fake_llm))
    graded = await graded_cases(engine, fake_llm, GRADED)
    started = await start_eval(engine)
    await execute(engine, started.operation_id, eval_deps(engine, fake_llm), epoch=current_epoch)
    run = await run_row(engine, started.eval_run_id)
    assert (run.status, run.passed, run.metrics["gate"]) == ("completed", True, "passed")
    assert run.metrics["cases"]["active"] == 10 + GRADED
    assert run.metrics["observations"]["paired"] == 10  # the test-retest sample, scored twice
    assert (run.metrics["knockout_recall"], run.metrics["false_exclusions"]) == ("1.000000", 0)
    sample = run.metrics["test_retest_sample"]
    assert len(sample) == 10 and sample == sorted(sample) == run.metrics["repetitions"]["sample"]
    adversarial = {
        int(r.id) for r in await rows(engine, "SELECT id FROM golden_cases WHERE source = 'adversarial'")
    }
    assert len(adversarial) == 10 and set(sample) <= {c.id for c in graded} | adversarial
    monkeypatch.setenv("JOBHUNTER_FAKE_LLM", "1")  # the gate takes a fake run's evidence on the fake only
    async with engine.connect() as conn:
        assert await validate.eval_gate_state(conn) == "passed"

    # 5. A second confirm (one more target title): the old tuple's unpublished targets cancelled, the new
    # bulk operation, `create_targets_for_tuple` for every retained current revision, and its
    # `batch_submit`. The batch is 250 items (248 clean drafts, 2 with a required certification that go to
    # the follow-up cycle), on the fake Message Batches API.
    async with engine.begin() as conn:
        added = [
            await insert_score_job(conn, PLAIN_AD) for _ in range(BATCH_ITEMS + 1 - await retained(conn))
        ]
    second = await confirmed_draft(client, op_runner, upload, engine)
    draft = (await client.get(f"/api/profile/drafts/{second}")).json()["draft"]
    edited = {**draft, "target_titles": [*draft["target_titles"], ADDED_TITLE]}
    r = await client.put(f"/api/profile/drafts/{second}", json={"draft": edited})
    assert r.status_code == 200, (r.status_code, r.text)
    reconfirmed = await confirm(client, second)
    version_2 = int(reconfirmed["profile_version_id"])
    tuple_2 = int(await scalar(engine, "SELECT llm_tuple_id FROM active_config WHERE id = 1"))
    assert tuple_2 != tuple_id
    old = await targets_of(engine, tuple_id)
    assert {name: (old[job.job_id].state, old[job.job_id].state_reason) for name, job in jobs.items()} == {
        "plain": ("published_accepted", None),
        "t5": ("published_accepted", None),
        "revised": ("published_accepted", None),
        "contested": ("published_contested", None),
        "far": ("published_accepted", None),
        "parked": ("cancelled", "tuple_superseded"),  # the old tuple's unpublished work
    }
    profile_2 = await profile_of(engine, version_2)
    clean = draft_body(PLAIN_AD.description_text, profile_2)
    followed = draft_body(CERT_AD.description_text, profile_2, facts=(CERT_FACT,))
    every_job = [*jobs.values(), *graded_jobs(graded), *added]
    followups = added[:2]
    async with engine.begin() as conn:
        for job in followups:  # their ad states the certification their draft asserts
            await conn.execute(
                sa.text(
                    "UPDATE job_revisions SET description_text = :d, "
                    "llm_input = llm_input || CAST(:i AS jsonb) WHERE id = :r"
                ),
                {
                    "d": CERT_AD.description_text,
                    "i": json.dumps({"description_text": CERT_AD.description_text}),
                    "r": job.revision_id,
                },
            )
    for job in every_job:
        fake_llm.answer("generate", job.revision_id, clean)
    for job in followups:
        fake_llm.answer("generate", job.revision_id, followed)
        fake_llm.answer("critic", job.revision_id, synthetic_review(CONFIRMED), round=1, schema=CriticOut)
    scripted_batches(create="ok", polls_until_ended=1)
    await op_runner.run_pending()  # the targets, then the batch_submit: prepared and created on the fake
    (bulk_2,) = await rows(
        engine,
        "SELECT id, state, budget_usd, spent_usd FROM bulk_operations WHERE llm_tuple_id = :t",
        t=tuple_2,
    )
    assert bulk_2.state == "active"
    bulks = await rows(engine, "SELECT llm_tuple_id, state FROM bulk_operations ORDER BY id")
    assert [tuple(b) for b in bulks] == [(tuple_id, "cancelled"), (tuple_2, "active")]
    (creation_2,) = [
        o
        for o in await rows(
            engine,
            "SELECT status, payload FROM operations WHERE kind = 'create_targets_for_tuple' ORDER BY id",
        )
        if o.payload["llm_tuple_id"] == tuple_2
    ]
    assert creation_2.status == "succeeded" and creation_2.payload["bulk_operation_id"] == bulk_2.id
    created_2 = await targets_of(engine, tuple_2)
    assert set(created_2) == {job.job_id for job in every_job}  # from the operation: every retained revision
    assert {r.bulk_operation_id for r in created_2.values()} == {bulk_2.id}
    (submit,) = await rows(engine, "SELECT status FROM operations WHERE kind = 'batch_submit'")
    assert submit.status == "succeeded"
    (submission,) = await submission_rows(engine)
    assert (submission.state, submission.request_count) == ("submitted", BATCH_ITEMS)
    for _ in range(5):
        await poll_once(engine, fake_llm, epoch=current_epoch)
        if await submission_state(engine, submission.id) == "reconciled":
            break
    assert await submission_state(engine, submission.id) == "reconciled"
    counts = Counter(await decisions(engine))
    assert counts == {"published": BATCH_ITEMS - 2, "followup": 2}, counts
    follow_cycle = await score_graph.execute_cycle(
        runner.graphs,
        runner.deps,
        trigger="batch_followup",
        epoch=current_epoch,
        fake_llm=True,
        admission_stopped=lambda: False,
    )
    assert follow_cycle is not None
    final = await targets_of(engine, tuple_2)
    assert Counter(r.state for r in final.values()) == {"published_accepted": BATCH_ITEMS, "filtered_out": 1}
    assert final[far.job_id].state == "filtered_out"
    assert {final[job.job_id].lane for job in followups} == {"batch_followup"}

    # 6. The month's counters equal the ledger, every bulk operation spent within its budget, the retention
    # pass deletes nothing of the active tuple, and no marker word of an ad or a draft left the score lane's
    # own tables.
    now = utcnow()
    assert await run_reconciler("budget_counters", engine, now=now, epoch=current_epoch) == 0
    async with engine.connect() as conn:
        assert await spend_ledger.recompute(conn, budget.month_of(now)) is None
    for bulk in await rows(engine, "SELECT spent_usd, budget_usd FROM bulk_operations"):
        assert bulk.spent_usd <= bulk.budget_usd
    kept = await active_tuple_rows(engine, tuple_2)
    assert kept["score_targets"] == BATCH_ITEMS + 1 and kept["llm_results"] == BATCH_ITEMS
    await run_retention_daily(
        engine, queue_app, saver, cv_store=CvStore(cv_store_dir), now=now, epoch=current_epoch
    )
    assert await active_tuple_rows(engine, tuple_2) == kept
    found = await outside_the_lane(engine, log_events)
    assert found["logs"] and found["checkpoint_blobs"] and found["score_cycles.report"]
    for where, texts in found.items():
        for marker in MARKERS:
            assert [t for t in texts if marker.casefold() in t] == [], (where, marker)
