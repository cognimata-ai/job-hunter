"""The p95 of the job detail over HTTP (spec §14.2 L4989, §15.4; plan M4 Task 6, B29).

50,000 cards seeded set-based as the list's p95 test seeds them (`bulk_seed_cards`: 30 % scored, 500 groups,
German and English ads) and analysed, on a server of their own, behind the whole api (`create_app`, edge and
session included) at the lifespan's pool size. 50 of the scored cards, spread over the seed, are made
contested drawers (`_contest`): their result contested with a nulled criterion, six criteria at the v1
weights, a knockout candidate and its instance's record, deterministic criteria and a cap, a review chain of
two drafts and two critic rounds (their validated disputes and required facts), and six evidence rows (two
of them description spans). Every fifth of them (`ENRICHED`) also has two postings with their links, merge
events on both sides (two merged jobs into it, and a merge of it into another card reversed by an unmerge)
and a feedback row (`_enrich`), so those reads are measured with rows. Three of them are requested once to
warm up, then each of the 50 once; every answer is a 200 with a score and contested data, the enriched ones
with their postings, merge events and feedback.
The p95 (the nearest rank) is recorded (`record_property`) and printed, and must be below `P95_LIMIT_S`: the
budget is never relaxed here.
"""

import asyncio
import math
import time
from collections.abc import Iterator
from typing import Any, Final

import httpx
import pytest
import sqlalchemy as sa
from jobhunter.api.app import ENGINE_POOL_SIZE, create_app
from jobhunter.auth import users
from jobhunter.clock import utcnow
from jobhunter.db.migrate import run_migrate
from jobhunter.db.session import make_engine, make_null_engine
from jobhunter.db.sql import q
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.fixtures import TEST_PASSWORD, TEST_USERNAME, ConfigFactory
from tests.support.job_detail import Note, add_evidence, criteria_doc, published_of, set_weights, write_chain
from tests.support.jobs_list import BulkSeed, add_posting, bulk_seed_cards
from tests.support.m3 import insert_id, insert_job
from tests.support.pg import PgServer, config_for, remove_pg_server, start_pg_server

pytestmark = [pytest.mark.perf, pytest.mark.integration]

CARDS: Final = 50_000
DETAILS: Final = 50
WARM_UP: Final = 3
ENRICHED: Final = 5  # every fifth drawer has postings, merge events and feedback (`_enrich`)
P95_LIMIT_S: Final = 0.200  # B29
_CONTEST: Final = q(
    "UPDATE llm_results SET status = 'contested', critic_outcome = 'contested', critic_injection = 'none', "
    "criteria = :criteria, nulled_criteria = ARRAY['domain'], knockout_candidates = :candidates, "
    "truncated_fields = ARRAY['criteria.skills.rationale'] WHERE id = :r",
    jsonb=["criteria", "candidates"],
)
_INSTANCE: Final = q(
    "UPDATE det_results SET det_criteria = :det, knockouts = :knockouts, caps_applied = :caps "
    "WHERE llm_result_id = :r AND is_current",
    jsonb=["det", "knockouts", "caps"],
)
_TEXT: Final = q(
    "SELECT v.description_text FROM jobs j JOIN job_revisions v ON v.id = j.current_revision_id "
    "WHERE j.id = :j"
)
_DET: Final = {
    "location_work_mode": {"level": 4, "basis": "observed", "inputs": {"distance_km": 0}},
    "employment_salary": {"level": None, "basis": "unknown", "inputs": {}},
}
_KNOCKOUT: Final = {
    "code": "work_permit",
    "fact_id": "work_permit:de",
    "comparison": "unmet",
    "severity": "hard",
    "binding": False,
    "source": "unconfirmed",
}
_NOTES: Final = (
    Note(
        0,
        ("T1",),
        "revise",
        "revise",
        disputes=(("role_fit", "level_too_high", 3), ("skills", "evidence_overlooked", 4)),
        facts=(("work_permit:de", "fact_uncertain"),),
        discarded=(("languages", "level_too_low", 2),),
    ),
    Note(
        1,
        ("T0",),
        "revise",
        "revise",
        disputes=(("domain", "evidence_ungrounded", None),),
        facts=(("work_permit:de", "fact_rejected"),),
        effort="max",
    ),
)


@pytest.fixture(scope="module")
def seeded(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[PgServer, list[int]]]:
    server = start_pg_server(tmp_path_factory.mktemp("job-detail-perf"))
    try:
        run_migrate(config_for(server, "migrate"))
        yield server, asyncio.run(_seed(server))
    finally:
        remove_pg_server(server)


async def _seed(server: PgServer) -> list[int]:
    engine = make_null_engine(server.dsn("migrator", driver="sqlalchemy"))
    try:
        seed = await bulk_seed_cards(engine, CARDS, scored_share=0.3, groups=500, langs=("de", "en"))
        details = await _contest(engine, seed)
        await _enrich(engine, details[::ENRICHED], other=seed.untargeted_ids[0])  # no drawer
        async with engine.connect() as c:
            await c.execute(sa.text("ANALYZE"))  # as the owner
            await c.commit()
    finally:
        await engine.dispose()
    return details


async def _contest(engine: AsyncEngine, seed: BulkSeed) -> list[int]:
    """`DETAILS` scored cards spread over the seed, each made a contested drawer (module docstring)."""
    step = len(seed.scored_ids) // DETAILS
    chosen = list(seed.scored_ids[::step][:DETAILS])
    async with engine.begin() as c:
        await set_weights(c, seed.llm_tuple_id)
        for job_id in chosen:
            await _contest_one(c, job_id)
    return chosen


async def _contest_one(c: AsyncConnection, job_id: int) -> None:
    p = await published_of(c, job_id)
    candidates = [{"fact_id": "work_permit:de", "inputs": {"work_authorization_de": "needs_visa"}}]
    await c.execute(
        _CONTEST, {"criteria": criteria_doc({"domain": None}), "candidates": candidates, "r": p.result_id}
    )
    caps = [{"code": "industry", "kind": "soft"}]
    await c.execute(_INSTANCE, {"det": _DET, "knockouts": [_KNOCKOUT], "caps": caps, "r": p.result_id})
    await write_chain(c, p, drafts=[{"role_fit": 4, "domain": 2}, {"role_fit": 3, "domain": 1}], notes=_NOTES)
    text = str((await c.execute(_TEXT, {"j": job_id})).scalar_one())
    spans = [(0, 12), (len(text) - 16, len(text))]
    await add_evidence(
        c,
        p.result_id,
        [
            *(
                {
                    "subject": key,
                    "source_kind": "description",
                    "quote": text[a:b],
                    "span_start": a,
                    "span_end": b,
                }
                for key, (a, b) in zip(("role_fit", "skills"), spans, strict=True)
            ),
            {
                "subject": "role_fit",
                "source_kind": "structured_field",
                "field_name": "title",
                "quote": "Erfunden",
            },
            {"subject": "skills", "source_kind": "profile", "field_name": "skills", "quote": "Python, SQL"},
            {
                "subject": "languages",
                "source_kind": "profile",
                "field_name": "languages",
                "quote": "Deutsch C1",
            },
            {
                "subject": "work_permit:de",
                "source_kind": "structured_field",
                "field_name": "locations",
                "quote": "Berlin",
            },
        ],
    )


async def _enrich(engine: AsyncEngine, job_ids: list[int], *, other: int) -> None:
    """Postings, merge events on both sides and a feedback row for each of `job_ids` (module docstring)."""
    for job_id in job_ids:
        await add_posting(
            engine, job_id, "jsearch", primary_link=f"https://jobs.publisher.example/ad/{job_id}"
        )
        await add_posting(engine, job_id, "adzuna", redirect=f"https://www.adzuna.example/land/ad/{job_id}")
    async with engine.begin() as c:
        for job_id in job_ids:
            for _ in range(2):
                merged = (await insert_job(c, merged_into_job_id=job_id)).job_id
                await _event(c, "strong_key_merge", merged, job_id)
            confirmed = await _event(c, "user_confirmed_merge", job_id, other)
            unmerge = await _event(c, "unmerge", job_id, other)
            await c.execute(_REVERSED, {"u": unmerge, "e": confirmed})
            await insert_id(c, "score_feedback", {"job_id": job_id, "kind": "label", "label": "ok"})


_REVERSED: Final = q("UPDATE merge_events SET reversed_by_event_id = :u WHERE id = :e")


async def _event(c: AsyncConnection, kind: str, from_job: int, into_job: int) -> int:
    values = {
        "kind": kind,
        "from_job_id": from_job,
        "into_job_id": into_job,
        "posting_ids": [],
        "evidence": {},
    }
    return await insert_id(c, "merge_events", values)


def p95(samples: list[float]) -> float:
    """The nearest-rank 95th percentile."""
    ordered = sorted(samples)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


async def _get(client: httpx.AsyncClient, job_id: int, *, enriched: bool) -> float:
    started = time.perf_counter()
    r = await client.get(f"/api/jobs/{job_id}")
    elapsed = time.perf_counter() - started
    assert r.status_code == 200, (job_id, r.status_code, r.text[:500])
    body = r.json()
    assert body["score"] is not None and body["contested"] is not None, job_id
    assert len(body["contested"]["rounds"]) == 2 and body["score"]["criteria"], job_id
    rows = (len(body["postings"]), len(body["merge_events"]), len(body["feedback"]))
    assert rows == ((2, 4, 1) if enriched else (0, 0, 0)), (job_id, rows)
    return elapsed


async def test_the_detail_p95_is_under_200_ms(
    seeded: tuple[PgServer, list[int]], make_config: ConfigFactory, record_property: Any
) -> None:
    server, details = seeded
    assert len(details) == DETAILS
    cfg = make_config("api", server)
    engine = make_engine(cfg, pool_size=ENGINE_POOL_SIZE)
    try:
        async with engine.begin() as c:
            await users.create_user(c, TEST_USERNAME, TEST_PASSWORD, now=utcnow())
        transport = httpx.ASGITransport(
            app=create_app(cfg=cfg, engine=engine), client=("198.51.100.31", 50000)
        )
        headers = {"Origin": "https://testserver", "X-JH-Request": "1"}
        async with httpx.AsyncClient(
            transport=transport, base_url="https://testserver", headers=headers
        ) as client:
            login = await client.post(
                "/api/auth/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD}
            )
            assert login.status_code == 204, login.text
            enriched = set(details[::ENRICHED])
            for job_id in details[:WARM_UP]:
                await _get(client, job_id, enriched=job_id in enriched)
            samples = [await _get(client, job_id, enriched=job_id in enriched) for job_id in details]
    finally:
        await engine.dispose()
    measured = p95(samples)
    record_property("job_detail_p95_ms", round(measured * 1000, 1))
    print(f"job_detail: p95 {measured * 1000:.1f} ms (max {max(samples) * 1000:.1f} ms)")
    assert measured < P95_LIMIT_S, samples
