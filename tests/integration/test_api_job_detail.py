"""`GET /api/jobs/{job_id}` and `GET /api/jobs/{job_id}/history` over HTTP (spec §14.2 L4989-4990, §15.4,
§15.5, §11.10, §8.8, §8.10; plan M4 Task 6, B12, B14, B22, B25, B26, B28).

The detail of a scored job carries its criteria with their evidence, its deterministic criteria, knockouts,
caps and versions; evidence spans are code-point offsets of the description it returns; a contested result
has its critic rounds, its rejected drafts' level diffs and its nulled criteria; a deferred member shows its
representative's result and description; postings and links follow the link precedence; corrections and
publication cuts are told apart. A merged job is 404 with its survivor, an unknown, synthetic or fenced one
404 without. The history pages the job's results by publication with every instance of each, and refuses a
cursor it did not sign for that job. The clock is injected (`jobhunter.api.routes.jobs.utcnow`). Every datum
is invented.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

import httpx
import pytest
import sqlalchemy as sa
from jobhunter.api.app import JobHunterApp
from jobhunter.api.pagination import JOB_HISTORY_SORT, cursor_key, encode_list_cursor
from jobhunter.api.routes import jobs as jobs_route
from jobhunter.db.sql import q
from jobhunter.jobs import detail as detail_module
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.job_detail import (
    LEVELS,
    Note,
    add_attempt,
    add_draft,
    add_evidence,
    add_note,
    criteria_doc,
    published_of,
    set_final,
    set_weights,
    write_chain,
)
from tests.support.jobs_list import SEEN, Card, add_posting, seed_cards
from tests.support.m2 import digest, insert_profile_chain
from tests.support.m3 import (
    Job,
    active_ids,
    insert_det_result,
    insert_id,
    insert_job,
    insert_published,
    insert_row,
)

pytestmark = pytest.mark.integration

NOW: Final = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
GREENHOUSE_LINK: Final = "https://boards.greenhouse.example/acme/jobs/4711"
PUBLISHER_LINK: Final = "https://jobs.publisher.example/ad/4711"
ADZUNA_REDIRECT: Final = "https://www.adzuna.example/land/ad/4711?se=invented&v=1"
TEXT: Final = "Wir bauen erfundene Dienste.\nDu entwickelst Backend-Services in Python für unser Testteam."


def scored(low: int, high: int, **fields: Any) -> Card:
    card = Card(state="published_accepted", score=(Decimal(low), Decimal(high)), passed=True)
    return replace(card, **fields)


def z(moment: datetime) -> str:
    """A time as the api writes it (UTC, `Z`)."""
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jobs_route, "utcnow", lambda: NOW)


async def detail(client: httpx.AsyncClient, job_id: int) -> dict[str, Any]:
    r = await client.get(f"/api/jobs/{job_id}")
    assert r.status_code == 200, (r.status_code, r.text)
    body: dict[str, Any] = r.json()
    return body


async def execute(engine: AsyncEngine, statement: str, *, jsonb: tuple[str, ...] = (), **params: Any) -> None:
    async with engine.begin() as c:
        await c.execute(q(statement, jsonb=list(jsonb)), params)


async def scalar(engine: AsyncEngine, statement: str, **params: Any) -> Any:
    async with engine.connect() as c:
        return (await c.execute(sa.text(statement), params)).scalar_one()


# --- a scored job -----------------------------------------------------------------------------------------


async def test_detail_of_a_scored_job_has_criteria_evidence_det_criteria_and_versions(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, active_tuple: int, clock: None
) -> None:
    (job_id,) = await seed_cards(
        engine,
        [scored(60, 80, title="Backend Engineer", company_key="acme", description=TEXT, seen=True)],
    )
    posting = await add_posting(engine, job_id, "greenhouse", primary_link=GREENHOUSE_LINK)
    instance_of = await scalar(engine, "SELECT source_instance_id FROM postings WHERE id = :p", p=posting)
    observation = await scalar(engine, "SELECT latest_observation_id FROM postings WHERE id = :p", p=posting)
    quote = "Backend-Services in Python"
    start = TEXT.index(quote)
    provenance = {
        "source_instance_id": instance_of,
        "posting_id": posting,
        "observation_id": observation,
        "observed_at": SEEN.isoformat(),
    }
    det_criteria = {
        "location_work_mode": {"level": 4, "basis": "observed", "inputs": {"distance_km": 0}},
        "employment_salary": {"level": None, "basis": "unknown", "inputs": {}},
    }
    knockout = {
        "code": "work_permit",
        "fact_id": "work_permit:de",
        "comparison": "unmet",
        "severity": "hard",
        "binding": True,
        "source": "structured",
    }
    async with engine.begin() as c:
        p = await published_of(c, job_id)
        weights = await set_weights(c, active_tuple)
        await write_chain(c, p, drafts=[LEVELS], generator=("claude-sonnet-5-5", "medium"))
        await c.execute(
            q(
                "UPDATE llm_results SET criteria = :c, knockout_candidates = :k, summary_en = 'A fit.', "
                "summary_de = 'Passt.' WHERE id = :r",
                jsonb=["c", "k"],
            ),
            {
                "c": criteria_doc(),
                "k": [{"fact_id": "work_permit:de", "inputs": {"work_authorization_de": "needs_visa"}}],
                "r": p.result_id,
            },
        )
        await c.execute(
            q(
                "UPDATE det_results SET det_criteria = :d, knockouts = :k, caps_applied = :caps, "
                "industry_match = 'outside_include' WHERE llm_result_id = :r",
                jsonb=["d", "k", "caps"],
            ),
            {
                "d": det_criteria,
                "k": [knockout],
                "caps": [{"code": "industry", "kind": "soft"}],
                "r": p.result_id,
            },
        )
        evidence = await add_evidence(
            c,
            p.result_id,
            [
                {
                    "subject": "role_fit",
                    "source_kind": "description",
                    "quote": quote,
                    "span_start": start,
                    "span_end": start + len(quote),
                },
                {
                    "subject": "role_fit",
                    "source_kind": "structured_field",
                    "field_name": "title",
                    "quote": "Backend",
                },
                {
                    "subject": "skills",
                    "source_kind": "profile",
                    "field_name": "skills",
                    "quote": "Python, SQL",
                },
            ],
        )
        await c.execute(
            q(
                "UPDATE jobs SET salary = :s, employment_types = ARRAY['full_time'], work_mode_source = "
                "'structured', work_mode_possible = ARRAY['hybrid'], field_provenance = :fp WHERE id = :j",
                jsonb=["s", "fp"],
            ),
            {
                "s": {
                    "min": 60000.0,
                    "max": 75000.5,
                    "currency": "EUR",
                    "period": "year",
                    "kind": "predicted",
                    "raw": None,
                    "annual_eur_min": 60000.0,
                    "annual_eur_max": 75000.5,
                },
                "fp": {
                    "work_mode_possible": {**provenance, "rule": "structured_intersection"},
                    "salary": {**provenance, "rule": "predicted"},
                    "industry_structured": {**provenance, "rule": "none"},
                },
                "j": job_id,
            },
        )
        await c.execute(
            sa.text("UPDATE user_job_actions SET note = 'Meine erfundene Notiz.' WHERE job_id = :j"),
            {"j": job_id},
        )
        feedback = await insert_id(
            c,
            "score_feedback",
            {
                "job_id": job_id,
                "llm_result_id": p.result_id,
                "kind": "label",
                "label": "good",
                "comment": "Passt.",
            },
        )
        version_no = (
            await c.execute(
                sa.text(
                    "SELECT v.version_no FROM llm_tuples t "
                    "JOIN profile_versions v ON v.id = t.profile_version_id WHERE t.id = :t"
                ),
                {"t": active_tuple},
            )
        ).scalar_one()
        instance = (
            await c.execute(
                sa.text("SELECT id FROM det_results WHERE llm_result_id = :r"), {"r": p.result_id}
            )
        ).scalar_one()

    body = await detail(logged_in_client, job_id)

    job = body["job"]
    assert (job["job_id"], job["title"], job["company"], job["company_key"]) == (
        job_id,
        "Backend Engineer",
        "acme",
        "acme",
    )
    assert (job["work_modes"], job["work_mode_known"], job["employment_types"]) == (
        ["hybrid"],
        True,
        ["full_time"],
    )
    assert job["salary"] == {
        "min": "60000",
        "max": "75000.5",
        "currency": "EUR",
        "period": "year",
        "kind": "estimate",
    }
    assert job["work_mode_source_id"] == "greenhouse"
    assert job["field_provenance"]["work_mode_possible"] == {
        "source_id": "greenhouse",
        "source_instance_id": instance_of,
        "posting_id": posting,
        "observation_id": observation,
        "observed_at": z(SEEN),
        "rule": "structured_intersection",
    }
    assert set(job["field_provenance"]) == {"work_mode_possible", "salary", "industry_structured"}
    assert body["description"] == {"text": TEXT, "lang": "de", "completeness": "full", "from_job_id": job_id}

    score = body["score"]
    assert (score["mid"], score["low"], score["high"], score["coverage"], score["confidence"]) == (
        "70.00",
        "60.00",
        "80.00",
        "0.900",
        "high",
    )
    assert (score["result_status"], score["result_id"], score["det_result_id"]) == (
        "accepted",
        p.result_id,
        instance,
    )
    assert (score["critic_outcome"], score["injection_suspected"], score["scored_via_job_id"]) == (
        "not_run",
        False,
        None,
    )
    assert score["versions"] == {
        "profile_version_no": version_no,
        "rubric_version": "test-1",
        "prompt_schema_hash": digest(1).hex(),
        "generator": {"model": "claude-sonnet-5-5", "effort": "medium"},
        "critic": None,
    }
    assert (score["summary_en"], score["summary_de"]) == ("A fit.", "Passt.")
    assert [c["criterion"] for c in score["criteria"]] == list(LEVELS)
    by_key = {c["criterion"]: c for c in score["criteria"]}
    assert by_key["role_fit"] == {
        "criterion": "role_fit",
        "weight": weights["role_fit"],
        "level": 4,
        "basis": "observed",
        "rationale": "Erfundene Begründung role_fit.",
        "evidence": [
            {
                "subject": "role_fit",
                "source_kind": "description",
                "field_name": None,
                "quote": quote,
                "span_start": start,
                "span_end": start + len(quote),
            },
            {
                "subject": "role_fit",
                "source_kind": "structured_field",
                "field_name": "title",
                "quote": "Backend",
                "span_start": None,
                "span_end": None,
            },
        ],
    }
    assert [e["source_kind"] for e in by_key["skills"]["evidence"]] == ["profile"]
    assert (by_key["domain"]["level"], by_key["domain"]["basis"], by_key["domain"]["evidence"]) == (
        None,
        "unknown",
        [],
    )
    assert len(evidence) == 3
    assert score["det_criteria"] == [
        {
            "criterion": "location_work_mode",
            "weight": weights["location_work_mode"],
            "level": 4,
            "basis": "observed",
            "inputs": {"distance_km": 0},
        },
        {
            "criterion": "employment_salary",
            "weight": weights["employment_salary"],
            "level": None,
            "basis": "unknown",
            "inputs": {},
        },
    ]
    assert score["knockouts"] == [
        {
            "code": "work_permit",
            "binding": True,
            "severity": "hard",
            "status": "unmet",
            "requirement": None,
            "profile_value": "needs_visa",
        }
    ]
    assert (score["caps_applied"], score["industry_match"]) == (
        [{"code": "industry", "kind": "soft"}],
        "outside_include",
    )
    assert (score["shortened_fields"], score["corrected_fields"]) == ([], [])
    assert body["contested"] is None

    assert body["target"]["state"] == "published_accepted"
    user = dict(body["user"])
    assert datetime.fromisoformat(user.pop("updated_at")).utcoffset() == timedelta(0)
    assert user == {
        "job_id": job_id,
        "seen_at": z(SEEN),
        "saved_at": None,
        "applied_at": None,
        "hidden_at": None,
        "note": "Meine erfundene Notiz.",
    }
    assert [(f["id"], f["kind"], f["label"], f["comment"]) for f in body["feedback"]] == [
        (feedback, "label", "good", "Passt.")
    ]
    assert body["links"] == {"open_ad": GREENHOUSE_LINK, "adzuna": None}
    assert [x["id"] for x in body["postings"]] == [posting]
    assert (body["group"], body["merge_events"]) == (None, [])
    history = body["history"]
    assert history["next_cursor"] is None
    assert [(i["result_id"], [x["det_result_id"] for x in i["instances"]]) for i in history["items"]] == [
        (p.result_id, [instance])
    ]


async def test_evidence_spans_are_code_point_offsets(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, active_tuple: int, clock: None
) -> None:
    """The emoji in front is one code point, two UTF-16 units and four UTF-8 bytes: the spans the api gives
    cut the quotes out of the text it gives with Python's (code-point) indexing, over a line break too."""
    text = (
        "🚀 Wir suchen Python-Entwickler (m/w/d) für unser erfundenes Team.\n"
        "Du arbeitest mit FastAPI und PostgreSQL."
    )
    quotes = ["Wir suchen Python-Entwickler", "FastAPI und PostgreSQL"]
    (job_id,) = await seed_cards(engine, [scored(50, 70, description=text)])
    async with engine.begin() as c:
        p = await published_of(c, job_id)
        await c.execute(
            q("UPDATE llm_results SET criteria = :c WHERE id = :r", jsonb=["c"]),
            {"c": criteria_doc(), "r": p.result_id},
        )
        await add_evidence(
            c,
            p.result_id,
            [
                {
                    "subject": "skills",
                    "source_kind": "description",
                    "quote": quote,
                    "span_start": text.index(quote),
                    "span_end": text.index(quote) + len(quote),
                }
                for quote in quotes
            ],
        )
    body = await detail(logged_in_client, job_id)
    served = body["description"]["text"]
    assert served == text
    (skills,) = [c for c in body["score"]["criteria"] if c["criterion"] == "skills"]
    spans = [(e["span_start"], e["span_end"], e["quote"]) for e in skills["evidence"]]
    assert [served[start:end] for start, end, _ in spans] == [quote for _, _, quote in spans] == quotes
    assert spans[0][0] == 2  # after the emoji and the space: 3 in UTF-16 units, 5 in UTF-8 bytes


# --- a contested result -----------------------------------------------------------------------------------


async def test_contested_detail_has_critic_rounds_and_the_rejected_drafts_level_diff(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, active_tuple: int, clock: None
) -> None:
    (job_id,) = await seed_cards(engine, [scored(40, 60, state="published_contested")])
    async with engine.begin() as c:
        p = await published_of(c, job_id)
        drafts = await write_chain(
            c,
            p,
            drafts=[{"role_fit": 4, "languages": 3}, {"role_fit": 3, "languages": 2}],
            notes=[
                Note(0, ("T1",), "revise", "revise", disputes=(("role_fit", "level_too_high", 3),)),
                Note(
                    1,
                    ("T0", "T3"),
                    "revise",
                    "revise",
                    disputes=(("languages", "evidence_ungrounded", None),),
                    facts=(("mandatory_language:de", "fact_uncertain"),),
                    effort="max",
                ),
            ],
        )
        await c.execute(
            q(
                "UPDATE llm_results SET criteria = :c, nulled_criteria = ARRAY['languages'] WHERE id = :r",
                jsonb=["c"],
            ),
            {"c": criteria_doc({"role_fit": 3, "languages": None}), "r": p.result_id},
        )
    body = await detail(logged_in_client, job_id)
    score = body["score"]
    assert (score["result_status"], score["critic_outcome"]) == ("contested", "contested")
    assert score["versions"]["generator"] == {"model": "claude-sonnet-5-5", "effort": "medium"}
    assert score["versions"]["critic"] == {"model": "claude-opus-5-5", "effort": "max"}  # the last round's
    (languages,) = [c for c in score["criteria"] if c["criterion"] == "languages"]
    assert (languages["level"], languages["basis"]) == (None, "unknown")
    assert body["contested"] == {
        "rounds": [
            {
                "round": 1,
                "triggers": ["T1"],
                "verdict": "revise",
                "outcome": "revise",
                "issues": [{"criterion": "role_fit", "issue": "level_too_high", "suggested_level": 3}],
            },
            {
                "round": 2,
                "triggers": ["T0", "T3"],
                "verdict": "revise",
                "outcome": "revise",
                "issues": [
                    {"criterion": "languages", "issue": "evidence_ungrounded", "suggested_level": None},
                    {
                        "criterion": "mandatory_language:de",
                        "issue": "fact_uncertain",
                        "suggested_level": None,
                    },
                ],
            },
        ],
        "rejected_drafts": [
            {
                "round": 0,
                "level_diff": [
                    {"criterion": "role_fit", "from_level": 4, "to_level": 3},
                    {"criterion": "languages", "from_level": 3, "to_level": 2},
                ],
            }
        ],
        "nulled_criteria": ["languages"],
    }
    assert len(drafts) == 2


async def test_the_rounds_issues_are_the_validated_rounds_disputes_and_required_facts(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, active_tuple: int, clock: None, log_events: Any
) -> None:
    """Ruling T6-d: a round's issues are those M3's validation of it kept (`steps.note_issues` but the
    injection): its grounded disputes, then each required fact the review did not confirm (rejected,
    uncertain, unreviewed). A discarded review, a review of no valid target and a confirmed fact are no issue,
    though the critic's raw output holds them; a suggested level that is no plain integer is null, and a
    stored dispute that cannot be read is left out with the content-free warning."""
    (job_id,) = await seed_cards(engine, [scored(40, 60, state="published_contested")])
    note = Note(
        0,
        ("T1",),
        "revise",
        "revise",
        disputes=(("role_fit", "level_too_high", 3),),
        facts=(
            ("mandatory_language:de", "fact_rejected"),
            ("degree:master", "fact_confirmed"),
            ("certification:erfunden", "fact_uncertain"),
            ("work_permit:de", None),
        ),
        discarded=(("skills", "level_too_low", 2),),
        invalid=(("Ein erfundenes Freitextziel", "level_too_high"),),
    )
    async with engine.begin() as c:
        p = await published_of(c, job_id)
        await write_chain(c, p, drafts=[LEVELS], notes=[note])
        await c.execute(
            q(
                "UPDATE critic_notes SET validation = jsonb_set(validation, '{disputes}', "
                "(validation -> 'disputes') || :more) WHERE attempt_id = :a",
                jsonb=["more"],
            ),
            {
                "more": [
                    {"target": "domain", "assessment": "should_be_unknown", "suggested_level": "3"},
                    {"unreadable": True},
                ],
                "a": p.attempt_id,
            },
        )
    (round_,) = (await detail(logged_in_client, job_id))["contested"]["rounds"]
    assert round_["issues"] == [
        {"criterion": "role_fit", "issue": "level_too_high", "suggested_level": 3},
        {"criterion": "domain", "issue": "should_be_unknown", "suggested_level": None},
        {"criterion": "mandatory_language:de", "issue": "fact_rejected", "suggested_level": None},
        {"criterion": "certification:erfunden", "issue": "fact_uncertain", "suggested_level": None},
        {"criterion": "work_permit:de", "issue": "fact_unreviewed", "suggested_level": None},
    ]
    skipped = [e for e in log_events() if e.get("event") == "job_detail_records_skipped"]
    assert [(e["kind"], e["job_id"], e["skipped"]) for e in skipped] == [("critic_disputes", job_id, 1)]
    assert "unreadable" not in repr(log_events())


async def test_a_resumed_chain_over_two_attempts_gives_the_rounds_and_drafts_of_both(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, active_tuple: int, clock: None
) -> None:
    """The result's attempt was resumed from the first one, past an attempt under another evaluation policy
    (as the claim links them): the chain's rounds and rejected drafts are those of the first and the last
    attempt, the final draft the last one's, and the attempt left out adds nothing, as `load_context` keeps
    them."""
    (job_id,) = await seed_cards(engine, [scored(40, 60, state="published_contested")])
    async with engine.begin() as c:
        p = await published_of(c, job_id)
        first = p.attempt_id
        await write_chain(
            c,
            p,
            drafts=[{"role_fit": 4}],
            notes=[Note(0, ("T1",), "revise", "revise", disputes=(("role_fit", "level_too_high", 3),))],
        )
        policy = (
            await c.execute(
                sa.text(
                    "INSERT INTO evaluation_policies (version_no, policy_hash, payload, "
                    "projected_critic_rate) SELECT max(version_no) + 1, '\\x0f0f', '{}', 0.3 "
                    "FROM evaluation_policies RETURNING id"
                )
            )
        ).scalar_one()
        stray = await add_attempt(
            c, p, resumed_from=None, evaluation_policy_id=policy, outcome="failed_request"
        )
        stray_draft = await add_draft(c, p, stray, 0, {"role_fit": 1})
        await add_note(c, p, stray, 1, stray_draft, Note(0, ("T9",), "accept", "accept"))
        last = await add_attempt(c, p, resumed_from=first)
        revision = await add_draft(c, p, last, 1, {"role_fit": 3}, generator=("claude-sonnet-5-5", "high"))
        await add_note(
            c,
            p,
            last,
            2,
            revision,
            Note(
                1, ("T0",), "revise", "incomplete", disputes=(("skills", "level_too_low", 4),), effort="max"
            ),
        )
        await set_final(c, p, revision, 1)
        await c.execute(
            sa.text("UPDATE llm_results SET attempt_id = :a WHERE id = :r"), {"a": last, "r": p.result_id}
        )
    body = await detail(logged_in_client, job_id)
    contested = body["contested"]
    assert [
        (r["round"], r["triggers"], r["outcome"], r["issues"][0]["criterion"]) for r in contested["rounds"]
    ] == [
        (1, ["T1"], "revise", "role_fit"),
        (2, ["T0"], "incomplete", "skills"),
    ]
    assert contested["rejected_drafts"] == [
        {"round": 0, "level_diff": [{"criterion": "role_fit", "from_level": 4, "to_level": 3}]}
    ]
    assert body["score"]["versions"]["generator"] == {"model": "claude-sonnet-5-5", "effort": "high"}
    assert body["score"]["versions"]["critic"] == {"model": "claude-opus-5-5", "effort": "max"}


async def test_an_accepted_result_the_critic_reviewed_has_its_critic_and_no_contested_data(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, active_tuple: int, clock: None
) -> None:
    (job_id,) = await seed_cards(engine, [scored(60, 80)])
    async with engine.begin() as c:
        p = await published_of(c, job_id)
        await write_chain(
            c,
            p,
            drafts=[LEVELS],
            notes=[
                Note(
                    0,
                    ("T2",),
                    "accept",
                    "accept",
                    facts=(("work_permit:de", "fact_confirmed"),),
                    effort="medium",
                )
            ],
        )
        await c.execute(
            sa.text(
                "UPDATE llm_results SET critic_outcome = 'accepted', critic_injection = 'none' WHERE id = :r"
            ),
            {"r": p.result_id},
        )
    body = await detail(logged_in_client, job_id)
    assert (body["score"]["result_status"], body["score"]["critic_outcome"]) == ("accepted", "accepted")
    assert body["score"]["versions"]["critic"] == {"model": "claude-opus-5-5", "effort": "medium"}
    assert body["contested"] is None


async def test_a_contested_result_without_a_current_instance_has_neither_score_nor_contested_data(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, active_tuple: int, clock: None
) -> None:
    """Between a scope flip and its recompute a published result has no current instance under the active
    scope: the drawer has no score, and so no contested data either (as on the card)."""
    (job_id,) = await seed_cards(engine, [scored(40, 60, state="published_contested")])
    async with engine.begin() as c:
        p = await published_of(c, job_id)
        await write_chain(
            c,
            p,
            drafts=[LEVELS],
            notes=[Note(0, ("T1",), "revise", "revise", disputes=(("role_fit", "level_too_high", 3),))],
        )
        await c.execute(
            sa.text(
                "UPDATE det_results SET is_current = false, superseded_at = now() WHERE llm_result_id = :r"
            ),
            {"r": p.result_id},
        )
    body = await detail(logged_in_client, job_id)
    assert (body["score"], body["contested"]) == (None, None)
    assert body["target"]["state"] == "published_contested"


# --- fact evidence ----------------------------------------------------------------------------------------


async def test_fact_evidence_holds_the_evidence_of_facts(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, active_tuple: int, clock: None
) -> None:
    """Ruling T6-a: the evidence rows of a fact (a subject that is no criterion key) are the score's
    `fact_evidence`, in row order, with the criteria evidence's span rule; no criterion holds them."""
    text = "Wir suchen erfundene Entwickler.\nDeutschkenntnisse auf C1-Niveau sind erforderlich."
    quotes = {"role_fit": "erfundene Entwickler", "mandatory_language:de": "Deutschkenntnisse auf C1-Niveau"}
    (job_id,) = await seed_cards(engine, [scored(50, 70, description=text)])
    async with engine.begin() as c:
        p = await published_of(c, job_id)
        await c.execute(
            q("UPDATE llm_results SET criteria = :c WHERE id = :r", jsonb=["c"]),
            {"c": criteria_doc(), "r": p.result_id},
        )
        await add_evidence(
            c,
            p.result_id,
            [
                *(
                    {
                        "subject": subject,
                        "source_kind": "description",
                        "quote": quote,
                        "span_start": text.index(quote),
                        "span_end": text.index(quote) + len(quote),
                    }
                    for subject, quote in quotes.items()
                ),
                {
                    "subject": "work_permit:de",
                    "source_kind": "structured_field",
                    "field_name": "locations",
                    "quote": "Berlin",
                },
            ],
        )
    body = await detail(logged_in_client, job_id)
    score = body["score"]
    start = text.index(quotes["mandatory_language:de"])
    assert score["fact_evidence"] == [
        {
            "subject": "mandatory_language:de",
            "source_kind": "description",
            "field_name": None,
            "quote": quotes["mandatory_language:de"],
            "span_start": start,
            "span_end": start + len(quotes["mandatory_language:de"]),
        },
        {
            "subject": "work_permit:de",
            "source_kind": "structured_field",
            "field_name": "locations",
            "quote": "Berlin",
            "span_start": None,
            "span_end": None,
        },
    ]
    served = body["description"]["text"]
    first = score["fact_evidence"][0]
    assert served[first["span_start"] : first["span_end"]] == first["quote"]
    held = [e["subject"] for c in score["criteria"] for e in c["evidence"]]
    assert held == ["role_fit"]


# --- a deferred member ------------------------------------------------------------------------------------


async def test_a_deferred_member_shows_the_representatives_result_and_description(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, active_tuple: int, clock: None
) -> None:
    """The deferred member's drawer shows the representative's result (scored via it) and the description its
    evidence indexes; its own target stays deferred. A member split out of the deferral, scored on its own,
    keeps its own; the group lists all three, the representative marked."""
    representative, member, split = await seed_cards(
        engine,
        [
            scored(60, 80, description="Beschreibung des Repräsentanten.", company_key="acme"),
            Card(
                description="Beschreibung des Mitglieds.",
                deferred_to=0,
                availability="stale",
                company_key="acme",
            ),
            scored(30, 40, description="Beschreibung des Abgespaltenen.", company_key="acme"),
        ],
    )
    async with engine.begin() as c:
        rep_result = (await published_of(c, representative)).result_id
        split_result = (await published_of(c, split)).result_id
        group = (
            await c.execute(
                sa.text("SELECT duplicate_group_id FROM jobs WHERE id = :j"), {"j": representative}
            )
        ).scalar_one()
        await c.execute(
            sa.text("UPDATE jobs SET duplicate_group_id = :g WHERE id = :j"), {"g": group, "j": split}
        )
        await insert_row(
            c,
            "duplicate_group_members",
            {"group_id": group, "job_id": split, "evidence": {}, "score_individually": True},
        )

    body = await detail(logged_in_client, member)
    assert (body["score"]["result_id"], body["score"]["scored_via_job_id"]) == (rep_result, representative)
    assert body["description"]["text"] == "Beschreibung des Repräsentanten."
    assert body["description"]["from_job_id"] == representative
    assert body["target"]["state"] == "deferred_duplicate"
    assert body["job"]["availability"] == "stale"  # its own fields
    assert body["group"] == {
        "id": group,
        "status": "active",
        "representative_job_id": representative,
        "unscorable_reason": None,
        "election_reason": "initial",
        "members": [
            {
                "job_id": job_id,
                "title": "Testrolle",
                "company": "acme",
                "source_ids": ["jsearch"],
                "locations": [],
                "completeness": "full",
                "availability": availability,
                "target_state": state,
                "score_individually": individually,
                "is_representative": job_id == representative,
            }
            for job_id, availability, state, individually in (
                (representative, "active", "published_accepted", False),
                (member, "stale", "deferred_duplicate", False),
                (split, "active", "published_accepted", True),
            )
        ],
    }

    own = await detail(logged_in_client, representative)
    assert (own["score"]["result_id"], own["score"]["scored_via_job_id"]) == (rep_result, None)
    assert own["description"]["from_job_id"] == representative
    assert own["group"] == body["group"]
    alone = await detail(logged_in_client, split)
    assert (alone["score"]["result_id"], alone["score"]["scored_via_job_id"]) == (split_result, None)
    assert alone["description"] == {
        "text": "Beschreibung des Abgespaltenen.",
        "lang": "de",
        "completeness": "full",
        "from_job_id": split,
    }


async def test_a_deferred_members_warnings_name_the_representative(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, active_tuple: int, clock: None, log_events: Any
) -> None:
    """The drawer of a deferred member reads its representative's result: a record of it that cannot be read
    is warned about under the representative, whose documents it is."""
    representative, member = await seed_cards(engine, [scored(60, 80), Card(deferred_to=0)])
    criteria = criteria_doc()
    criteria["skills"] = {"level": "unreadable"}
    async with engine.begin() as c:
        p = await published_of(c, representative)
        await c.execute(
            q("UPDATE llm_results SET criteria = :c WHERE id = :r", jsonb=["c"]),
            {"c": criteria, "r": p.result_id},
        )
    assert (await detail(logged_in_client, member))["score"]["scored_via_job_id"] == representative
    skipped = [e for e in log_events() if e.get("event") == "job_detail_records_skipped"]
    assert [(e["kind"], e["job_id"], e["skipped"]) for e in skipped] == [("criteria", representative, 1)]


# --- gone jobs --------------------------------------------------------------------------------------------


async def test_a_merged_job_is_404_with_its_survivor(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, clock: None
) -> None:
    """A merged job, and one merged into a job merged in turn, answer 404 with the survivor at the end of the
    chain, on both routes; the survivor's drawer lists its merge events, a reversed one and the unmerge not
    reversible."""
    async with engine.begin() as c:
        survivor = (await insert_job(c)).job_id
        middle = (await insert_job(c, merged_into_job_id=survivor)).job_id
        first = (await insert_job(c, merged_into_job_id=middle)).job_id
        undone = (await insert_job(c)).job_id
        merged = await insert_id(
            c,
            "merge_events",
            {
                "kind": "strong_key_merge",
                "from_job_id": middle,
                "into_job_id": survivor,
                "posting_ids": [1],
                "evidence": {},
            },
        )
        confirmed = await insert_id(
            c,
            "merge_events",
            {
                "kind": "user_confirmed_merge",
                "from_job_id": undone,
                "into_job_id": survivor,
                "posting_ids": [2],
                "evidence": {},
            },
        )
        unmerge = await insert_id(
            c,
            "merge_events",
            {
                "kind": "unmerge",
                "from_job_id": undone,
                "into_job_id": survivor,
                "posting_ids": [2],
                "evidence": {},
            },
        )
        await c.execute(
            sa.text("UPDATE merge_events SET reversed_by_event_id = :u WHERE id = :e"),
            {"u": unmerge, "e": confirmed},
        )
    for gone in (first, middle):
        for url in (f"/api/jobs/{gone}", f"/api/jobs/{gone}/history"):
            r = await logged_in_client.get(url)
            assert r.status_code == 404, (url, r.text)
            problem = r.json()
            assert (problem["code"], problem["merged_into_job_id"]) == ("not_found", survivor), url
            assert r.headers["content-type"].startswith("application/problem+json")
            assert str(gone) not in problem["title"]
    body = await detail(logged_in_client, survivor)
    assert [
        (e["id"], e["kind"], e["from_job_id"], e["into_job_id"], e["reversible"])
        for e in body["merge_events"]
    ] == [
        (merged, "strong_key_merge", middle, survivor, True),
        (confirmed, "user_confirmed_merge", undone, survivor, False),
        (unmerge, "unmerge", undone, survivor, False),
    ]
    assert [e["id"] for e in (await detail(logged_in_client, undone))["merge_events"]] == [confirmed, unmerge]


async def test_a_synthetic_or_unknown_job_is_404_without_survivor(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, clock: None
) -> None:
    async with engine.begin() as c:
        synthetic = (await insert_job(c, synthetic_eval=True)).job_id
        fenced = (await insert_job(c)).job_id
        await insert_id(c, "purge_fences", {"kind": "jobs", "job_ids": [fenced], "manifest_id": 1})
        live = (await insert_job(c)).job_id
    unknown = live + 1_000_000
    for job_id in (unknown, synthetic, fenced):
        for url in (f"/api/jobs/{job_id}", f"/api/jobs/{job_id}/history"):
            r = await logged_in_client.get(url)
            assert r.status_code == 404, (url, r.text)
            problem = r.json()
            assert problem["code"] == "not_found"
            assert "merged_into_job_id" in problem and problem["merged_into_job_id"] is None, url
    assert (await detail(logged_in_client, live))["job"]["job_id"] == live
    for bad in ("0", "-1", "abc", str(2**63)):
        r = await logged_in_client.get(f"/api/jobs/{bad}")
        assert (r.status_code, r.json()["code"]) == (422, "validation_failed"), bad


async def test_a_job_without_a_current_revision_is_404_without_survivor(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, clock: None
) -> None:
    async with engine.begin() as c:
        job_id = (await insert_job(c)).job_id
        await c.execute(sa.text("UPDATE jobs SET current_revision_id = NULL WHERE id = :j"), {"j": job_id})
    for url in (f"/api/jobs/{job_id}", f"/api/jobs/{job_id}/history"):
        r = await logged_in_client.get(url)
        assert r.status_code == 404, (url, r.text)
        assert (r.json()["code"], r.json()["merged_into_job_id"]) == ("not_found", None), url


# --- stored documents -------------------------------------------------------------------------------------


async def test_an_unreadable_stored_document_is_left_out_and_the_warning_holds_no_content(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, active_tuple: int, clock: None, log_events: Any
) -> None:
    """Each stored criterion, deterministic criterion, cap, provenance entry and the salary is read on its
    own: an unreadable one is left out, the others stay, and the warning names the job, the kind and how many
    were left out, never what they hold."""
    (job_id,) = await seed_cards(engine, [scored(50, 70)])
    criteria = criteria_doc()
    criteria["skills"] = {"level": "unreadable-level", "basis": "observed"}
    criteria["domain"] = {"level": True, "basis": "observed"}  # a bool is no level
    det = {
        "location_work_mode": {"level": 9, "basis": "unreadable"},
        "employment_salary": {"level": 2, "basis": "observed", "inputs": {}},
    }
    entry = {"source_instance_id": 1, "posting_id": 1, "observation_id": 1, "observed_at": SEEN.isoformat()}
    async with engine.begin() as c:
        p = await published_of(c, job_id)
        await c.execute(
            q("UPDATE llm_results SET criteria = :c WHERE id = :r", jsonb=["c"]),
            {"c": criteria, "r": p.result_id},
        )
        await c.execute(
            q(
                "UPDATE det_results SET det_criteria = :d, caps_applied = :caps WHERE llm_result_id = :r",
                jsonb=["d", "caps"],
            ),
            {"d": det, "caps": [{"code": "industry", "kind": "soft"}, {"unreadable": 1}], "r": p.result_id},
        )
        await c.execute(
            q("UPDATE jobs SET salary = :s, field_provenance = :fp WHERE id = :j", jsonb=["s", "fp"]),
            {
                "s": {"min": "unreadable", "currency": "EUR"},
                "fp": {"title": {**entry, "rule": "source_rank"}, "salary": {"rule": "unreadable"}},
                "j": job_id,
            },
        )
    body = await detail(logged_in_client, job_id)
    score = body["score"]
    assert [c["criterion"] for c in score["criteria"]] == [k for k in LEVELS if k not in ("skills", "domain")]
    assert [c["criterion"] for c in score["det_criteria"]] == ["employment_salary"]
    assert score["caps_applied"] == [{"code": "industry", "kind": "soft"}]
    assert (body["job"]["salary"], list(body["job"]["field_provenance"])) == (None, ["title"])
    skipped = [e for e in log_events() if e.get("event") == "job_detail_records_skipped"]
    assert {e["kind"]: (e["job_id"], e["skipped"]) for e in skipped} == {
        "criteria": (job_id, 2),
        "det_criteria": (job_id, 1),
        "caps_applied": (job_id, 1),
        "salary": (job_id, 1),
        "field_provenance": (job_id, 1),
    }
    assert all(
        set(e) <= {"event", "job_id", "kind", "skipped", "request_id", "level", "ts", "logger"}
        for e in skipped
    )
    assert "unreadable" not in repr(log_events())


# --- postings and links -----------------------------------------------------------------------------------


async def test_postings_and_open_ad_follow_the_link_precedence(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, active_tuple: int, clock: None
) -> None:
    """Each posting offers its own link (an Adzuna posting its redirect; a `javascript:` URL is none); the ad
    opens at the ATS link first, the publisher's next once the ATS posting offers none."""
    (job_id,) = await seed_cards(
        engine, [scored(50, 70, source_ids=("adzuna", "arbeitnow", "greenhouse", "jsearch"))]
    )
    publisher = await add_posting(
        engine, job_id, "jsearch", observed_at=SEEN + timedelta(hours=3), primary_link=PUBLISHER_LINK
    )
    ats = await add_posting(engine, job_id, "greenhouse", observed_at=SEEN, primary_link=GREENHOUSE_LINK)
    adzuna = await add_posting(
        engine, job_id, "adzuna", observed_at=SEEN + timedelta(hours=1), redirect=ADZUNA_REDIRECT
    )
    script = await add_posting(
        engine, job_id, "arbeitnow", observed_at=SEEN + timedelta(hours=2), primary_link="javascript:alert(1)"
    )
    body = await detail(logged_in_client, job_id)
    assert body["postings"] == [
        {
            "id": posting,
            "source_id": source,
            "native_id": f"{source}-{job_id}-{at.timestamp():.0f}",
            "first_seen_at": z(at),
            "last_seen_at": z(at),
            "availability": "active",
            "link": link,
        }
        for posting, source, at, link in (
            (publisher, "jsearch", SEEN + timedelta(hours=3), PUBLISHER_LINK),
            (ats, "greenhouse", SEEN, GREENHOUSE_LINK),
            (adzuna, "adzuna", SEEN + timedelta(hours=1), ADZUNA_REDIRECT),
            (script, "arbeitnow", SEEN + timedelta(hours=2), None),
        )
    ]
    assert body["links"] == {"open_ad": GREENHOUSE_LINK, "adzuna": ADZUNA_REDIRECT}
    await execute(
        engine,
        "UPDATE posting_observations SET normalized = normalized - 'primary_link' "
        "WHERE id = (SELECT latest_observation_id FROM postings WHERE id = :p)",
        p=ats,
    )
    again = await detail(logged_in_client, job_id)
    assert [x["link"] for x in again["postings"]] == [PUBLISHER_LINK, None, ADZUNA_REDIRECT, None]
    assert again["links"] == {"open_ad": PUBLISHER_LINK, "adzuna": ADZUNA_REDIRECT}


# --- shortened and corrected ------------------------------------------------------------------------------


async def test_shortened_and_corrected_fields_are_told_apart(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, active_tuple: int, clock: None
) -> None:
    """B22: `truncated_fields` holds the final draft's corrections (the normaliser's and the validation's, in
    its stored history) and the publication's cuts; the corrected ones are those of the history, in its
    order, the shortened ones the rest."""
    (job_id,) = await seed_cards(engine, [scored(50, 70)])
    validation = {
        "normalisation": {
            "issues": [],
            "failures": [],
            "repairs": [
                {
                    "path": "job_facts.min_years_experience.value",
                    "code": "out_of_range",
                    "replaced": 55,
                    "exempt": [],
                }
            ],
        },
        "corrections": [
            {
                "path": "job_facts.work_mode.value",
                "code": "work_mode_structured",
                "replaced": "onsite",
                "exempt": [],
            }
        ],
        "current": {"issues": [], "failures": [], "groundings": [], "required": []},
    }
    async with engine.begin() as c:
        p = await published_of(c, job_id)
        await c.execute(
            q("UPDATE score_drafts SET validation = :v WHERE id = :d", jsonb=["v"]),
            {"v": validation, "d": p.final_draft_id},
        )
        await c.execute(
            sa.text(
                "UPDATE llm_results SET truncated_fields = ARRAY['job_facts.min_years_experience.value', "
                "'job_facts.work_mode.value', 'criteria.skills.rationale', 'summary_en'] WHERE id = :r"
            ),
            {"r": p.result_id},
        )
    score = (await detail(logged_in_client, job_id))["score"]
    assert score["corrected_fields"] == ["job_facts.min_years_experience.value", "job_facts.work_mode.value"]
    assert score["shortened_fields"] == ["criteria.skills.rationale", "summary_en"]


# --- the history ------------------------------------------------------------------------------------------


async def test_history_pages_by_published_at_and_lists_every_instance(
    logged_in_client: httpx.AsyncClient,
    engine: AsyncEngine,
    active_tuple: int,
    clock: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B25: the job's results (the active tuple's current one, another tuple's, an older revision's) newest
    first by publication, each with every instance (current or superseded, under any scope), newest first; a
    page of one walks them by cursor, and the drawer holds the first page."""
    (job_id,) = await seed_cards(engine, [scored(60, 80)])
    t1, t2, t3 = NOW - timedelta(days=9), NOW - timedelta(days=5), NOW - timedelta(days=1)
    async with engine.begin() as c:
        current = await published_of(c, job_id)
        revision = (
            await c.execute(sa.text("SELECT current_revision_id FROM jobs WHERE id = :j"), {"j": job_id})
        ).scalar_one()
        other_tuple = (await insert_profile_chain(c, 2)).llm_tuple_id
        scope = (await active_ids(c)).scope_version_id
        other_scope = await insert_id(c, "scope_versions", {"scope_hash": b"\x09\x09", "payload": {}})
        old_revision = await insert_id(
            c,
            "job_revisions",
            {
                "job_id": job_id,
                "revision_no": 2,
                "material_hash": b"\x02",
                "llm_input": {},
                "description_text": "Eine ältere erfundene Fassung.",
                "description_completeness": "full",
                "ad_language": "de",
                "embedding_status": "awaiting_embedding",
            },
        )
        older = await insert_published(c, Job(job_id, revision), other_tuple)
        oldest = await insert_published(c, Job(job_id, old_revision), active_tuple)
        for result, at in ((current.result_id, t3), (older.result_id, t2), (oldest.result_id, t1)):
            await c.execute(
                sa.text("UPDATE llm_results SET published_at = :at WHERE id = :r"), {"at": at, "r": result}
            )
        seeded = (
            await c.execute(
                sa.text("SELECT id FROM det_results WHERE llm_result_id = :r"), {"r": current.result_id}
            )
        ).scalar_one()
        superseded = await insert_det_result(
            c, current.result_id, job_id, scope, low=Decimal(40), high=Decimal(60), current=False
        )
        elsewhere = await insert_det_result(
            c, current.result_id, job_id, other_scope, low=Decimal(10), high=Decimal(30)
        )
        older_instance = await insert_det_result(
            c, older.result_id, job_id, scope, low=Decimal(20), high=Decimal(30)
        )
        for instance, at in (
            (seeded, t3),
            (superseded, t3 - timedelta(hours=2)),
            (elsewhere, t3 + timedelta(hours=1)),
            (older_instance, t2),
        ):
            await c.execute(
                sa.text("UPDATE det_results SET computed_at = :at WHERE id = :i"), {"at": at, "i": instance}
            )
        rubric_2 = (
            await c.execute(
                sa.text(
                    "SELECT r.version FROM llm_tuples t "
                    "JOIN rubric_versions r ON r.id = t.rubric_version_id WHERE t.id = :t"
                ),
                {"t": other_tuple},
            )
        ).scalar_one()

    url = f"/api/jobs/{job_id}/history"
    pages: list[dict[str, Any]] = []
    params: dict[str, Any] = {"limit": 1}
    while True:
        r = await logged_in_client.get(url, params=params)
        assert r.status_code == 200, r.text
        pages.append(r.json())
        if pages[-1]["next_cursor"] is None:
            break
        params = {"limit": 1, "cursor": pages[-1]["next_cursor"]}
    items = [item for page in pages for item in page["items"]]
    assert [len(page["items"]) for page in pages] == [1, 1, 1]
    assert [
        (i["result_id"], i["published_at"], i["llm_tuple_id"], i["rubric_version"], i["status"])
        for i in items
    ] == [
        (current.result_id, z(t3), active_tuple, "test-1", "accepted"),
        (older.result_id, z(t2), other_tuple, rubric_2, "accepted"),
        (oldest.result_id, z(t1), active_tuple, "test-1", "accepted"),
    ]
    assert items[1]["profile_version_no"] == items[0]["profile_version_no"] + 1
    assert [(x["det_result_id"], x["scope_version_id"], x["is_current"]) for x in items[0]["instances"]] == [
        (elsewhere, other_scope, True),
        (seeded, scope, True),
        (superseded, scope, False),
    ]
    assert items[0]["instances"][0] == {
        "det_result_id": elsewhere,
        "scope_version_id": other_scope,
        "is_current": True,
        "mid": "20.00",
        "low": "10.00",
        "high": "30.00",
        "coverage": "0.900",
        "confidence": "high",
        "computed_at": z(t3 + timedelta(hours=1)),
    }
    assert [x["det_result_id"] for x in items[1]["instances"]] == [older_instance]
    assert items[2]["instances"] == []
    drawer = (await detail(logged_in_client, job_id))["history"]
    assert (drawer["items"], drawer["next_cursor"]) == (items, None)
    two = (await logged_in_client.get(url, params={"limit": 2})).json()
    assert [i["result_id"] for i in two["items"]] == [current.result_id, older.result_id]
    rest = (await logged_in_client.get(url, params={"limit": 2, "cursor": two["next_cursor"]})).json()
    assert ([i["result_id"] for i in rest["items"]], rest["next_cursor"]) == ([oldest.result_id], None)
    monkeypatch.setattr(detail_module, "HISTORY_LIMIT", 2)  # a drawer whose first page is not the last
    short = (await detail(logged_in_client, job_id))["history"]
    assert [i["result_id"] for i in short["items"]] == [current.result_id, older.result_id]
    after = (await logged_in_client.get(url, params={"cursor": short["next_cursor"]})).json()
    assert ([i["result_id"] for i in after["items"]], after["next_cursor"]) == ([oldest.result_id], None)


async def test_a_bad_history_cursor_is_400(
    logged_in_client: httpx.AsyncClient,
    api_app: JobHunterApp,
    engine: AsyncEngine,
    active_tuple: int,
    clock: None,
) -> None:
    """Only a cursor this history signed for this job pages it: another job's, another list's, a tampered
    one, a malformed one and one whose position has the wrong shape are 400 `invalid_cursor`; the limit is
    1 to 50."""
    one, other = await seed_cards(engine, [scored(60, 80), scored(40, 50)])
    key = cursor_key(api_app.api.state.cfg)

    def signed(job_id: int, last_key: list[Any], sort: str = JOB_HISTORY_SORT) -> str:
        return encode_list_cursor(
            sort=sort, dir="desc", last_key=last_key, filters={"job_id": job_id}, key=key
        )

    position = [z(datetime(2100, 1, 1, tzinfo=UTC)), 10**9]  # after every result
    ok = await logged_in_client.get(f"/api/jobs/{one}/history", params={"cursor": signed(one, position)})
    assert ok.status_code == 200, ok.text
    assert [i["result_id"] for i in ok.json()["items"]] == [(await published_of_engine(engine, one))]
    good = signed(one, position)
    bad = [
        signed(other, position),  # another job's
        signed(one, position, sort="golden_cases.id"),  # another list's
        good[:-2] + ("AA" if not good.endswith("AA") else "BB"),  # tampered
        "not-a-cursor",
        signed(one, [z(NOW)]),
        signed(one, ["yesterday", 5]),
        signed(one, [NOW.replace(tzinfo=None).isoformat(), 5]),  # not a UTC time
        signed(one, [z(NOW), "5"]),
        signed(one, [z(NOW), True]),
        signed(one, [z(NOW), 0]),
    ]
    for cursor in bad:
        r = await logged_in_client.get(f"/api/jobs/{one}/history", params={"cursor": cursor})
        assert (r.status_code, r.json()["code"]) == (400, "invalid_cursor"), cursor
    for limit in (0, 51):
        r = await logged_in_client.get(f"/api/jobs/{one}/history", params={"limit": limit})
        assert (r.status_code, r.json()["code"]) == (422, "validation_failed"), limit
    r = await logged_in_client.get(f"/api/jobs/{one}/history", params={"cursor": "x" * 2049})
    assert r.status_code == 422


async def test_a_bad_cursor_is_400_before_a_gone_job_is_404(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, clock: None
) -> None:
    """The history route reads its cursor before the snapshot: a merged job's history with a cursor this
    route did not sign is 400 `invalid_cursor`, without one 404 with the survivor (the intended order)."""
    async with engine.begin() as c:
        survivor = (await insert_job(c)).job_id
        merged = (await insert_job(c, merged_into_job_id=survivor)).job_id
    url = f"/api/jobs/{merged}/history"
    r = await logged_in_client.get(url, params={"cursor": "not-a-cursor"})
    assert (r.status_code, r.json()["code"]) == (400, "invalid_cursor")
    r = await logged_in_client.get(url)
    assert (r.status_code, r.json()["merged_into_job_id"]) == (404, survivor)


async def published_of_engine(engine: AsyncEngine, job_id: int) -> int:
    async with engine.connect() as c:
        return (await published_of(c, job_id)).result_id
