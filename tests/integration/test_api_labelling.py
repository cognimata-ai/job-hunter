"""`GET /api/labelling/queue` over HTTP, and the queue's merge (spec §15.6, §11.13; plan M4 Task 9, B16).

The queue takes the bands `[75, 100]`, `[60, 75)`, `[40, 60)`, `[0, 40)` round-robin from the top band, the
unlabelled candidates first (no golden case for the current revision and the active profile, whatever the
case's source or flag), each band by job id descending, and fills the rest with labelled candidates once
every band's unlabelled ones ran out. A candidate is a live job with its own published score of the active
tuple under the active scope that the scope's hard filters do not reject: a deferred member, a
not-a-job-ad, a merged, synthetic or fenced job, a job without a published score, a result of an older
revision or of another tuple and a filtered job are none; a fence of another job changes nothing. No key of
the answer names a score, a mid or a band, in the body or in the OpenAPI schema. The progress counts the
active `week1` cases of the active profile against 50, and a case labelled through the feedback route reads
back as the golden eval reads it; without an active profile the queue is empty. The limit is 1 to 50, default
10.

P9: on a server of its own, 2,000 cards seeded and analysed (`test_jobs_list_plans.py` says why), every band
read uses `det_rank_mid` with the scope and the band's score range in its index condition.

The merge (`labelling.queue_order`) is a pure function, tested here without a database. Every datum is
invented.
"""

import asyncio
import re
from collections.abc import Iterator
from dataclasses import replace
from decimal import Decimal
from typing import Any, Final

import httpx
import pytest
import sqlalchemy as sa
from jobhunter.api.app import create_app
from jobhunter.db.migrate import run_migrate
from jobhunter.db.session import make_null_engine
from jobhunter.db.snapshot import read_snapshot
from jobhunter.jobs.labelling import BANDS, Candidate, labelling_queue, queue_order
from jobhunter.scoring.det_results import det_facts_from_inputs
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.jobs_list import (
    BERLIN,
    Card,
    activate_scope,
    activate_tuple,
    bulk_seed_cards,
    place,
    seed_cards,
)
from tests.support.m2 import insert_profile_chain
from tests.support.m3 import Job, insert_golden_case, insert_id
from tests.support.pg import PgServer, config_for, remove_pg_server, start_pg_server

pytestmark = pytest.mark.integration

URL: Final = "/api/labelling/queue"
FORBIDDEN_KEY: Final = re.compile(r"score|mid|band")
# 15 code points, two of them outside ASCII and one outside the BMP: 50 of them are 750 code points
PARAGRAPH: Final = "Prüfe 🙂 Daten. "


def scored(mid: str, **fields: Any) -> Card:
    """A published card whose current instance's `score_mid` is `mid` (its low and high bound too)."""
    value = Decimal(mid)
    return replace(Card(state="published_accepted", score=(value, value)), **fields)


async def queue(client: httpx.AsyncClient, **params: Any) -> dict[str, Any]:
    r = await client.get(URL, params=params)
    assert r.status_code == 200, (r.status_code, r.text)
    body: dict[str, Any] = r.json()
    return body


def ids(body: dict[str, Any]) -> list[int]:
    return [item["job_id"] for item in body["items"]]


async def active_profile(engine: AsyncEngine) -> int:
    async with engine.connect() as c:
        row = await c.execute(sa.text("SELECT profile_version_id FROM active_config WHERE id = 1"))
        return int(row.scalar_one())


async def other_profile(engine: AsyncEngine) -> int:
    """A second confirmed profile, not the active one."""
    async with engine.begin() as c:
        return (await insert_profile_chain(c, 2)).profile_version_id


async def label(engine: AsyncEngine, job_id: int, *, profile: int | None = None, **columns: object) -> None:
    """A golden case of the job's current revision (`insert_golden_case`: active, `week1`, the active scope,
    unless `columns` say otherwise), of `profile` or else the active profile."""
    owner = await active_profile(engine) if profile is None else profile
    async with engine.begin() as c:
        current = await c.execute(
            sa.text("SELECT current_revision_id FROM jobs WHERE id = :j"), {"j": job_id}
        )
        await insert_golden_case(c, Job(job_id, int(current.scalar_one())), owner, **columns)


async def execute(engine: AsyncEngine, statement: str, **params: Any) -> None:
    async with engine.begin() as c:
        await c.execute(sa.text(statement), params)


def keys(value: object) -> set[str]:
    """Every key of every object in `value`, recursively."""
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in keys(v)}
    return set()


def schema_properties(document: dict[str, Any], schema: object) -> set[str]:
    """Every property name of `schema` and of every schema it refers to, recursively."""
    seen: set[str] = set()
    found: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        if isinstance(ref := node.get("$ref"), str):
            if ref not in seen:
                seen.add(ref)
                walk(document["components"]["schemas"][ref.rsplit("/", 1)[1]])
            return
        if isinstance(properties := node.get("properties"), dict):
            found.update(properties)
        for value in node.values():
            walk(value)

    walk(schema)
    return found


# --- the merge, without a database -------------------------------------------------------------------------


def unlabelled(*job_ids: int) -> list[Candidate]:
    return [Candidate(job_id, labelled=False) for job_id in job_ids]


def test_queue_order_takes_the_bands_in_turn_and_skips_an_exhausted_one() -> None:
    bands = [unlabelled(9, 8, 7), unlabelled(6), [], unlabelled(5, 4)]
    assert queue_order(bands, 10) == [9, 6, 5, 8, 4, 7]
    assert queue_order(bands, 4) == [9, 6, 5, 8]
    assert queue_order(bands, 1) == [9]
    assert queue_order([[], [], [], []], 10) == []


def test_queue_order_takes_labelled_candidates_only_once_every_band_s_unlabelled_ran_out() -> None:
    bands = [
        [Candidate(9, labelled=False), Candidate(8, labelled=True)],
        [Candidate(7, labelled=True), Candidate(6, labelled=True)],
        unlabelled(5, 4),
        [Candidate(3, labelled=True)],
    ]
    assert queue_order(bands, 10) == [9, 5, 4, 8, 7, 3, 6]
    assert queue_order(bands, 3) == [9, 5, 4]  # the unlabelled fill the limit: no labelled one
    assert queue_order(bands, 5) == [9, 5, 4, 8, 7]


# --- the queue over HTTP ------------------------------------------------------------------------------------


async def test_queue_is_round_robin_over_the_four_bands_unlabelled_first(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, active_tuple: int
) -> None:
    a1, b1, a2, c1, d1, a3, b2, d2, a4 = await seed_cards(
        engine,
        [
            scored("85.00"),
            scored("65.00"),
            scored("95.00"),
            scored("45.00"),
            scored("15.00"),
            scored("78.00"),
            scored("72.00"),
            scored("34.00"),
            scored("77.00"),
        ],
    )
    await label(engine, a3)
    # band 1: a4, a2, a1 (a3 labelled); band 2: b2, b1; band 3: c1; band 4: d2, d1 - each by job id, not score
    assert ids(await queue(logged_in_client, limit=8)) == [a4, b2, c1, d2, a2, b1, d1, a1]
    assert ids(await queue(logged_in_client, limit=3)) == [a4, b2, c1]
    assert ids(await queue(logged_in_client, limit=9)) == [a4, b2, c1, d2, a2, b1, d1, a1, a3]


async def test_band_edges(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, active_tuple: int
) -> None:
    anchors = await seed_cards(engine, [scored("90.00"), scored("70.00"), scored("50.00"), scored("20.00")])
    e75, e7499, e60, e40, e3999, e100, e0 = await seed_cards(
        engine,
        [
            scored("75.00"),
            scored("74.99"),
            scored("60.00"),
            scored("40.00"),
            scored("39.99"),
            scored("100.00"),
            scored("0.00"),
        ],
    )
    a1, a2, a3, a4 = anchors
    # band 1: e100, e75, a1; band 2: e60, e7499, a2; band 3: e40, a3; band 4: e0, e3999, a4
    assert ids(await queue(logged_in_client, limit=50)) == [
        e100,
        e60,
        e40,
        e0,
        e75,
        e7499,
        a3,
        e3999,
        a1,
        a2,
        a4,
    ]


async def test_the_queue_hides_the_score_and_skips_deferred_members_and_not_a_job_ad(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, active_tuple: int
) -> None:
    text = PARAGRAPH * 50
    rep, member, ad_with_score, ad, plain, empty = await seed_cards(
        engine,
        [
            scored("70.00"),
            Card(deferred_to=0),  # scored via its representative: no candidate of its own
            scored("55.00", not_a_job_ad=True),  # a verified not-a-job-ad, even with a score
            Card(state="published_accepted", not_a_job_ad=True),
            scored(
                "30.00",
                title="Datenanalyst",
                company_key="beispiel-gmbh",
                locations=(place(BERLIN),),
                ad_language="en",
                description=text,
            ),
            scored("80.00", description=""),
        ],
    )
    body = await queue(logged_in_client, limit=50)
    assert ids(body) == [empty, rep, plain]
    assert not {member, ad_with_score, ad} & set(ids(body))
    item = body["items"][2]
    assert item == {
        "job_id": plain,
        "title": "Datenanalyst",
        "company": "beispiel-gmbh",
        "locations": ["Testort"],
        "ad_language": "en",
        "excerpt": text[:600],
    }
    assert len(item["excerpt"]) == 600 and len(item["excerpt"].encode()) > 600  # code points, not bytes
    assert body["items"][0]["excerpt"] == ""
    assert not [k for k in keys(body) if FORBIDDEN_KEY.search(k)], keys(body)
    document = create_app().api.openapi()
    response = document["paths"][URL]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    properties = schema_properties(document, response)
    assert {"items", "progress", "job_id", "excerpt", "labelled", "target"} <= properties
    assert not [p for p in properties if FORBIDDEN_KEY.search(p)], properties


async def test_merged_synthetic_fenced_unscored_and_stale_jobs_are_no_candidates(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, active_tuple: int
) -> None:
    live, merged, synthetic, fenced, stale, contested, *_ = await seed_cards(
        engine,
        [
            scored("50.00"),
            scored("50.00"),
            scored("50.00"),
            scored("50.00"),
            scored("50.00"),
            scored("50.00", state="published_contested"),
            Card(state="pending"),
            Card(),  # no target
            Card(state="published_accepted"),  # published without an instance
        ],
    )
    await execute(engine, "UPDATE jobs SET merged_into_job_id = :live WHERE id = :j", live=live, j=merged)
    await execute(engine, "UPDATE jobs SET synthetic_eval = true WHERE id = :j", j=synthetic)
    async with engine.begin() as c:
        await insert_id(c, "purge_fences", {"kind": "jobs", "job_ids": [fenced], "manifest_id": 1})
    # a new current revision without a target: the published score is the old revision's
    await execute(
        engine,
        "WITH r AS (INSERT INTO job_revisions (job_id, revision_no, material_hash, llm_input, "
        "description_text, description_completeness, ad_language, embedding_status) "
        "VALUES (:j, 2, '\\x01', '{}', 'Erfundene Testanzeige, neu.', 'full', 'de', 'awaiting_embedding') "
        "RETURNING id) "
        "UPDATE jobs SET current_revision_id = (SELECT id FROM r) WHERE id = :j",
        j=stale,
    )
    assert ids(await queue(logged_in_client, limit=50)) == [contested, live]
    await activate_scope(engine, remote_within_germany=False)  # the instances are the old scope's
    assert (await queue(logged_in_client, limit=50))["items"] == []


async def test_a_job_the_active_scope_s_hard_filters_reject_is_no_candidate(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, active_tuple: int
) -> None:
    passed, unfiltered, rejected = await seed_cards(
        engine, [scored("50.00", passed=True), scored("50.00"), scored("50.00", passed=False)]
    )
    # the list's `filtered` bucket, not its `scored` one: a label on it would calibrate nothing the user sees
    assert ids(await queue(logged_in_client, limit=50)) == [unfiltered, passed]
    assert rejected not in ids(await queue(logged_in_client, limit=50))


async def test_an_open_fence_of_another_job_leaves_every_other_candidate(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, active_tuple: int
) -> None:
    first, second, elsewhere = await seed_cards(engine, [scored("50.00"), scored("50.00"), Card()])
    async with engine.begin() as c:
        await insert_id(c, "purge_fences", {"kind": "jobs", "job_ids": [elsewhere], "manifest_id": 1})
    assert ids(await queue(logged_in_client, limit=50)) == [second, first]
    async with engine.begin() as c:
        await insert_id(c, "purge_fences", {"kind": "jobs", "job_ids": [second], "manifest_id": 2})
    assert ids(await queue(logged_in_client, limit=50)) == [first]
    await execute(engine, "UPDATE purge_fences SET released_at = now()")  # released fences fence nothing
    assert ids(await queue(logged_in_client, limit=50)) == [second, first]
    async with engine.begin() as c:
        await insert_id(c, "purge_fences", {"kind": "global", "manifest_id": 3})
    assert (await queue(logged_in_client, limit=50))["items"] == []


async def test_a_job_published_under_another_tuple_only_is_no_candidate(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, active_tuple: int
) -> None:
    (old,) = await seed_cards(engine, [scored("50.00")])
    assert ids(await queue(logged_in_client)) == [old]
    await activate_tuple(engine, 2)  # a new profile: its tuple has no target of the job yet
    assert (await queue(logged_in_client))["items"] == []
    (new,) = await seed_cards(engine, [scored("50.00")])
    assert ids(await queue(logged_in_client)) == [new]


async def test_progress_counts_active_week1_cases_of_the_active_profile(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, active_tuple: int
) -> None:
    other = await other_profile(engine)
    jobs = await seed_cards(engine, [Card()] * 6)
    week1, week1_too, inactive, feedback, adversarial, of_other = jobs
    await label(engine, week1)
    await label(engine, week1_too, label="poor")
    await label(engine, inactive, active=False)
    await label(engine, feedback, source="feedback")
    await label(engine, adversarial, source="adversarial")
    await label(engine, of_other, profile=other)
    assert (await queue(logged_in_client))["progress"] == {"labelled": 2, "target": 50}
    # through the feedback route: a label is an active `week1` case, a "wrong score" a `feedback` one
    labelled, wrong = await seed_cards(engine, [scored("65.00"), scored("62.00")])
    assert ids(await queue(logged_in_client)) == [wrong, labelled]
    for job_id, kind in ((labelled, "label"), (wrong, "wrong_score")):
        r = await logged_in_client.post(f"/api/jobs/{job_id}/feedback", json={"kind": kind, "label": "good"})
        assert r.status_code == 201, r.text
    body = await queue(logged_in_client)
    assert body["progress"] == {"labelled": 3, "target": 50}
    assert ids(body) == [wrong, labelled]  # both labelled now, so still listed: they fill the rest
    async with engine.connect() as c:
        snapshots = await c.execute(
            sa.text(
                "SELECT g.det_inputs_snapshot FROM golden_cases g JOIN jobs j "
                "ON j.current_revision_id = g.job_revision_id WHERE j.id = ANY(CAST(:j AS bigint[]))"
            ),
            {"j": [labelled, wrong]},
        )
        facts = [det_facts_from_inputs(snapshot) for snapshot in snapshots.scalars()]
    assert len(facts) == 2  # the cases' frozen det-facts read back, as the golden eval reads them


async def test_labelled_cases_fill_the_rest(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, active_tuple: int
) -> None:
    other = await other_profile(engine)
    x1, y1, z1, x2, y2 = await seed_cards(
        engine, [scored("90.00"), scored("70.00"), scored("20.00"), scored("80.00"), scored("65.00")]
    )
    await label(engine, x1)
    await label(engine, y1, source="feedback", active=False)  # labelled all the same
    await label(engine, z1, profile=other)  # not labelled for the active profile
    await label(engine, y2)
    # unlabelled: band 1 x2, band 4 z1; labelled: band 1 x1, band 2 y2, y1
    assert ids(await queue(logged_in_client, limit=10)) == [x2, z1, x1, y2, y1]
    assert ids(await queue(logged_in_client, limit=4)) == [x2, z1, x1, y2]
    assert ids(await queue(logged_in_client, limit=2)) == [x2, z1]


async def test_limit_bounds(
    logged_in_client: httpx.AsyncClient,
    api_client: httpx.AsyncClient,
    engine: AsyncEngine,
    active_tuple: int,
) -> None:
    await seed_cards(engine, [scored("50.00")] * 12)
    assert len((await queue(logged_in_client))["items"]) == 10  # default 10
    assert len((await queue(logged_in_client, limit=50))["items"]) == 12
    assert len((await queue(logged_in_client, limit=1))["items"]) == 1
    for limit in (0, 51, "x"):
        r = await logged_in_client.get(URL, params={"limit": limit})
        assert r.status_code == 422, (limit, r.text)
        assert r.json()["code"] == "validation_failed"
        assert [e["field"] for e in r.json()["errors"]] == ["query.limit"]
    r = await api_client.get(URL)
    assert (r.status_code, r.json()["code"]) == (401, "unauthenticated")


async def test_without_an_active_profile_the_queue_is_empty(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, active_tuple: int
) -> None:
    candidate, labelled = await seed_cards(engine, [scored("50.00"), scored("40.00")])
    await label(engine, labelled)
    body = await queue(logged_in_client)
    assert (ids(body), body["progress"]) == ([candidate, labelled], {"labelled": 1, "target": 50})
    # no active profile, and so no active tuple (one CHECK ties them): the same rows are no queue
    await execute(engine, "UPDATE active_config SET profile_version_id = NULL, llm_tuple_id = NULL")
    assert await queue(logged_in_client) == {"items": [], "progress": {"labelled": 0, "target": 50}}


# --- P9: the band reads' plans ------------------------------------------------------------------------------

CARDS: Final = 2_000
_BAND_TAG: Final = "/* labelling.band */"


@pytest.fixture(scope="module")
def seeded(tmp_path_factory: pytest.TempPathFactory) -> Iterator[PgServer]:
    server = start_pg_server(tmp_path_factory.mktemp("labelling-plans"))
    try:
        run_migrate(config_for(server, "migrate"))
        asyncio.run(_seed(server))
        yield server
    finally:
        remove_pg_server(server)


async def _seed(server: PgServer) -> None:
    engine = make_null_engine(server.dsn("migrator", driver="sqlalchemy"))
    try:
        await bulk_seed_cards(engine, CARDS, scored_share=0.8, groups=40, langs=("de", "en"))
        async with engine.connect() as c:
            await c.execute(sa.text("ANALYZE"))  # as the owner
            await c.commit()
    finally:
        await engine.dispose()


async def test_p9_a_band_read_uses_det_rank_mid_with_its_score_range(seeded: PgServer) -> None:
    engine = make_null_engine(seeded.dsn("migrator", driver="sqlalchemy"))
    statements: list[tuple[str, Any]] = []

    def record(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool) -> None:
        statements.append((statement, parameters))

    try:
        async with read_snapshot(engine) as conn:
            await conn.execute(sa.text("SET LOCAL enable_seqscan = off"))
            event.listen(engine.sync_engine, "before_cursor_execute", record)
            try:
                answer = await labelling_queue(conn, limit=10)
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", record)
            assert len(answer.items) == 10, "a short queue: the plan of an empty band proves nothing"
            bands = [(s, p) for s, p in statements if s.startswith(_BAND_TAG)]
            assert len(bands) == len(BANDS)
            for statement, parameters in bands:
                rows = await conn.exec_driver_sql("EXPLAIN " + statement, parameters)
                plan = "\n".join(str(line) for line in rows.scalars())
                scan = re.search(r"(?:Index Scan using|Bitmap Index Scan on) det_rank_mid\b", plan)
                assert scan is not None, plan
                cond = re.search(r"Index Cond: (.*)", plan[scan.start() :])
                assert cond is not None, plan
                assert re.search(r"\bscope_version_id = ", cond.group(1)), plan
                assert re.search(r"\bscore_mid >= ", cond.group(1)), plan
                assert re.search(r"\bscore_mid <=? ", cond.group(1)), plan
    finally:
        await engine.dispose()
