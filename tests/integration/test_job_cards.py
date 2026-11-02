"""The cards of the Jobs list (spec §15.3 L5192-5212, §15.5, §7.18, §8.8 L3362; plan M4 Task 5, B2, B10, B17,
B21-B24, B27).

Every card is built by `jobs.cards.build_cards` in a read snapshot over cards that
`tests/support/jobs_list.py` seeds, from the rows `select_page` lists or from rows named directly: its
score, badges and links, its knockouts' texts, its keyword chip, its previous score while it is re-scored,
the group card with its deferred members, a member with its own score, an unscorable group's badge, the
outbound links' precedence, and one statement per kind of data however many cards a page holds. Every datum
is invented.
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, replace
from datetime import timedelta
from decimal import Decimal
from typing import Any, Final

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.snapshot import read_snapshot
from jobhunter.db.sql import q
from jobhunter.jobs.cards import JobCardOut, MemberBriefOut, RefOut, build_cards
from jobhunter.jobs.links import OutboundLinks, outbound_links
from jobhunter.jobs.listing import SelectedRow, select_page
from jobhunter.jobs.query import JobsQuery
from jobhunter.scoring.knockouts import derive_knockouts
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.facts import facts, seeded_scope
from tests.support.jobs_list import SEEN, Card, activate_scope, activate_tuple, add_posting, seed_cards
from tests.support.m3 import Job, insert_target
from tests.support.scoring import job_facts, profile_with_language, required_language

pytestmark = pytest.mark.integration

ANY_WINDOW: Final = (None, None)
EVERY_CARD: Final = JobsQuery(
    posted="any",
    status=["unseen", "seen", "saved", "applied", "hidden"],
    availability=["active", "unknown", "stale", "closed"],
    scoring_state=["scored", "unscored", "filtered", "failed", "expired", "not_a_job_ad"],
)
GREENHOUSE_LINK: Final = "https://boards.greenhouse.example/acme/jobs/4711"
PUBLISHER_LINK: Final = "https://jobs.publisher.example/ad/4711"
ADZUNA_REDIRECT: Final = "https://www.adzuna.example/land/ad/4711?se=invented&v=1"
_TAG: Final = "/* jobs_cards."


def scored(low: int, high: int, **fields: Any) -> Card:
    """A published card with a current score instance `[low, high]` and a passed hard filter."""
    return replace(
        Card(state="published_accepted", score=(Decimal(low), Decimal(high)), passed=True), **fields
    )


async def cards(engine: AsyncEngine, job_ids: Sequence[int]) -> list[JobCardOut]:
    """The cards of `job_ids` (rows named directly, in that order) in one read snapshot."""
    rows = [SelectedRow(job_id, "all", None) for job_id in job_ids]
    async with read_snapshot(engine) as conn:
        return await build_cards(conn, rows, now=utcnow())


async def listed(engine: AsyncEngine, query: JobsQuery = EVERY_CARD) -> dict[int, JobCardOut]:
    """The cards of the first page of up to 100 rows `select_page` lists for `query`, by job id."""
    async with read_snapshot(engine) as conn:
        page = await select_page(conn, query, window=ANY_WINDOW, after=None, limit=100, with_counts=False)
        built = await build_cards(conn, page.rows, now=utcnow())
    assert [card.job_id for card in built] == [row.job_id for row in page.rows]
    return {card.job_id: card for card in built}


async def execute(engine: AsyncEngine, statement: str, **params: Any) -> None:
    async with engine.begin() as c:
        await c.execute(sa.text(statement), params)


async def result_of(engine: AsyncEngine, job_id: int) -> int:
    """The id of the published result of the job's current-revision target."""
    async with engine.connect() as c:
        row = await c.execute(
            sa.text(
                "SELECT t.published_result_id FROM score_targets t JOIN jobs j "
                "ON j.current_revision_id = t.job_revision_id "
                "WHERE j.id = :j AND t.published_result_id IS NOT NULL"
            ),
            {"j": job_id},
        )
        return int(row.scalar_one())


# --- a scored card ----------------------------------------------------------------------------------------


async def test_a_scored_card_has_its_score_badges_and_links(engine: AsyncEngine, active_tuple: int) -> None:
    (job_id,) = await seed_cards(
        engine,
        [
            scored(
                64,
                80,
                title="Senior Backend Engineer",
                company_key="acme",
                state="published_contested",
                completeness="partial",
                availability="stale",
                source_ids=("adzuna", "greenhouse"),
                coverage=Decimal("0.85"),
                confidence="medium",
                seen=True,
                saved=True,
                locations=({"label": "Berlin", "resolution": "unique", "candidates": []},),
                location_kind="city",
                work_mode_possible=("hybrid", "remote"),
                work_mode_source="structured",
            )
        ],
    )
    await execute(engine, "UPDATE user_job_actions SET note = 'Erfundene Notiz' WHERE job_id = :j", j=job_id)
    await add_posting(engine, job_id, "greenhouse", primary_link=GREENHOUSE_LINK)
    await add_posting(engine, job_id, "adzuna", redirect=ADZUNA_REDIRECT)
    (card,) = (await listed(engine)).values()
    assert (card.job_id, card.kind, card.title, card.company_key) == (
        job_id,
        "job",
        "Senior Backend Engineer",
        "acme",
    )
    assert (card.locations, card.location_kind, card.work_modes, card.work_mode_known) == (
        ["Berlin"],
        "city",
        ["remote", "hybrid"],
        True,
    )
    assert (card.posted_at, card.posted_precision, card.first_seen_at) == (None, "unknown", SEEN)
    assert card.score is not None
    body = card.model_dump(mode="json")
    assert body["score"] == {
        "mid": "72.00",
        "low": "64.00",
        "high": "80.00",
        "coverage": "0.850",
        "confidence": "medium",
        "result_status": "contested",
    }
    assert card.target is not None and card.target.state == "published_contested"
    assert card.bucket == "scored"
    assert card.badges == ["contested", "partial", "stale", "adzuna"]
    assert card.user is not None and card.user.saved_at == SEEN and card.user.has_note
    assert card.user.applied_at is None and card.user.hidden_at is None
    assert "note" not in body["user"]
    assert (card.links.open_ad, card.links.adzuna) == (GREENHOUSE_LINK, ADZUNA_REDIRECT)
    assert (card.knockouts, card.filter_reasons, card.filter_keyword, card.previous) == ([], [], None, None)
    assert (card.group, card.group_badge, card.possible_duplicate_of) == (None, None, None)


# --- the knockouts --------------------------------------------------------------------------------------


async def test_knockout_text_comes_from_job_facts_and_candidate_inputs(
    engine: AsyncEngine, active_tuple: int
) -> None:
    """German C1 required, the profile at B2: an unmet, critic-confirmed, binding hard knockout whose card
    reads `de C1` from the job facts and `B2` from the candidate's inputs; once the profile version is purged
    the profile's side is gone, the requirement is not."""
    (job_id,) = await seed_cards(engine, [scored(20, 25)])
    facts_doc = job_facts(required_languages=[required_language("de", level="C1")])
    (candidate,) = derive_knockouts(facts_doc, facts(), profile_with_language("de", "B2"), seeded_scope())
    assert (candidate.fact_id, candidate.comparison) == ("mandatory_language:de", "unmet")
    record = {  # `scoring.aggregate`'s record of a binding knockout
        "code": candidate.code,
        "fact_id": candidate.fact_id,
        "comparison": candidate.comparison,
        "severity": candidate.severity,
        "binding": True,
        "source": "critic_confirmed",
    }
    result = await result_of(engine, job_id)
    async with engine.begin() as c:
        await c.execute(
            q(
                "UPDATE llm_results SET job_facts = :f, knockout_candidates = :k, fact_confirmations = :c "
                "WHERE id = :r",
                jsonb=["f", "k", "c"],
            ),
            {
                "f": facts_doc.model_dump(mode="json"),
                "k": [asdict(candidate)],
                "c": {"mandatory_language:de": "critic_confirmed"},
                "r": result,
            },
        )
        await c.execute(
            q("UPDATE det_results SET knockouts = :k WHERE llm_result_id = :r", jsonb=["k"]),
            {"k": [record], "r": result},
        )
    (card,) = await cards(engine, [job_id])
    assert [k.model_dump() for k in card.knockouts] == [
        {
            "code": "mandatory_language",
            "binding": True,
            "severity": "hard",
            "status": "unmet",
            "requirement": "de C1",
            "profile_value": "B2",
        }
    ]
    await execute(
        engine,
        "UPDATE profile_versions SET purged_at = now(), profile_json = NULL "
        "WHERE id = (SELECT profile_version_id FROM llm_results WHERE id = :r)",
        r=result,
    )
    (purged,) = await cards(engine, [job_id])
    assert [(k.requirement, k.profile_value) for k in purged.knockouts] == [("de C1", None)]


# --- the badges -------------------------------------------------------------------------------------------


async def test_shortened_possibly_not_a_job_ad_and_injection_badges(
    engine: AsyncEngine, active_tuple: int
) -> None:
    shortened, reviewed, injected, not_an_ad = await seed_cards(
        engine,
        [
            scored(50, 70),
            scored(50, 70),
            scored(50, 70),
            Card(state="published_accepted", not_a_job_ad=True),
        ],
    )
    updates = (
        (shortened, "truncated_fields = ARRAY['summary_en']"),
        (reviewed, 'fact_confirmations = \'{"not_a_job_ad:ad": "critic_rejected"}\''),
        (injected, "injection_suspected = true"),
        (not_an_ad, 'fact_confirmations = \'{"not_a_job_ad:ad": "critic_confirmed"}\''),
    )
    for job_id, assignment in updates:
        statement = f"UPDATE llm_results SET {assignment} WHERE id = :r"  # noqa: S608 (this test's constants)
        await execute(engine, statement, r=await result_of(engine, job_id))
    by_id = await listed(engine)
    assert by_id[shortened].badges == ["shortened"]
    assert by_id[reviewed].badges == ["possibly_not_a_job_ad"]
    assert by_id[injected].badges == ["injection_suspected"]
    assert (by_id[not_an_ad].badges, by_id[not_an_ad].bucket, by_id[not_an_ad].score) == (
        [],
        "not_a_job_ad",
        None,
    )


# --- the keyword chip -------------------------------------------------------------------------------------


async def test_the_filtered_chip_names_the_excluded_keyword(engine: AsyncEngine, active_tuple: int) -> None:
    await activate_scope(
        engine, excluded_keywords=["Praktikum", "Werkstudent"], exclude_keywords_in_description=True
    )
    in_title, first_listed, in_text, scored_out, passed = await seed_cards(
        engine,
        [
            Card(title="Werkstudent Vertrieb", state="filtered_out", passed=False),
            Card(title="Werkstudent im Praktikum", state="filtered_out", passed=False),
            Card(
                title="Data Engineer",
                description="Ein erfundenes Praktikum.",
                state="filtered_out",
                passed=False,
            ),
            scored(40, 60, title="Werkstudent Data", passed=False),
            scored(40, 60, title="Platform Engineer"),
        ],
    )
    by_id = await listed(engine)
    assert by_id[in_title].filter_keyword == "Werkstudent"
    assert by_id[first_listed].filter_keyword == "Praktikum"  # the scope's first keyword the title holds
    assert (by_id[in_text].filter_keyword, by_id[in_text].bucket) == ("Praktikum", "filtered")
    assert by_id[in_text].filter_reasons == ["excluded_keyword"]
    # a scored card that fails the filter is filtered too, with its chip and its score
    assert (by_id[scored_out].filter_keyword, by_id[scored_out].bucket) == ("Werkstudent", "filtered")
    assert by_id[scored_out].score is not None
    assert (by_id[passed].filter_keyword, by_id[passed].filter_reasons) == (None, [])
    await activate_scope(engine, exclude_keywords_in_description=False)
    async with engine.begin() as c:  # the new scope's filter results, as a recomputation writes them
        await c.execute(
            sa.text(
                "INSERT INTO hard_filter_results (job_id, scope_version_id, passed, reasons, inputs_hash, "
                "det_algorithm_version) SELECT job_id, a.scope_version_id, passed, reasons, inputs_hash, "
                "det_algorithm_version FROM hard_filter_results h CROSS JOIN active_config a "
                "WHERE h.scope_version_id <> a.scope_version_id"
            )
        )
    (title_only,) = await cards(engine, [in_text])
    assert title_only.filter_keyword is None  # the description is not matched under this scope


# --- the previous score -----------------------------------------------------------------------------------


async def test_previous_score_during_a_rescore_names_the_old_profile_version(
    engine: AsyncEngine, active_tuple: int
) -> None:
    rescored, untargeted = await seed_cards(engine, [scored(60, 80), scored(30, 50)])
    async with engine.connect() as c:
        old_version = int(
            (
                await c.execute(
                    sa.text(
                        "SELECT p.version_no FROM llm_tuples u "
                        "JOIN profile_versions p ON p.id = u.profile_version_id "
                        "WHERE u.id = :t"
                    ),
                    {"t": active_tuple},
                )
            ).scalar_one()
        )
        revision = int(
            (
                await c.execute(
                    sa.text("SELECT current_revision_id FROM jobs WHERE id = :j"), {"j": rescored}
                )
            ).scalar_one()
        )
    new_tuple = await activate_tuple(engine, n=2)
    async with engine.begin() as c:
        await insert_target(c, Job(rescored, revision), new_tuple, state="pending")
    (published,) = await seed_cards(engine, [scored(70, 90)])
    by_id = await listed(engine)
    pending = by_id[rescored]
    assert pending.target is not None and (pending.target.state, pending.score) == ("pending", None)
    assert pending.previous is not None
    assert pending.previous.model_dump(mode="json") == {"mid": "70.00", "profile_version_no": old_version}
    other = by_id[untargeted]  # no target of the new tuple yet: re-scored too
    assert other.previous is not None and other.previous.mid == Decimal("40.00")
    assert (other.target, other.bucket) == (None, "unscored")
    assert by_id[published].previous is None  # published under the active tuple: nothing is re-scored


# --- groups -----------------------------------------------------------------------------------------------


async def test_a_group_card_lists_its_deferred_members_and_a_split_member_is_its_own_card_with_possible_duplicate_of(  # noqa: E501 (the plan's name)
    engine: AsyncEngine, active_tuple: int
) -> None:
    rep, first, second, split = await seed_cards(
        engine,
        [
            scored(60, 80, title="Backend Engineer", availability="stale", source_ids=("greenhouse",)),
            Card(title="Backend Engineer Hamburg", deferred_to=0, source_ids=("jsearch",)),
            Card(
                title="Backend Engineer Berlin", deferred_to=0, availability="closed", completeness="partial"
            ),
            scored(30, 50, title="Backend Engineer Remote", source_ids=("adzuna",)),
        ],
    )
    await execute(
        engine,
        "UPDATE jobs SET duplicate_group_id = (SELECT duplicate_group_id FROM jobs WHERE id = :r) "
        "WHERE id = :s",
        r=rep,
        s=split,
    )
    by_id = await listed(engine)
    assert set(by_id) == {rep, split}  # the deferred members are no cards
    group_card = by_id[rep]
    assert group_card.kind == "group" and group_card.group is not None
    assert group_card.group.size == 4
    assert group_card.group.representative == RefOut(
        job_id=rep, title="Backend Engineer", source_ids=["greenhouse"]
    )
    assert group_card.group.members == [
        MemberBriefOut(
            job_id=first,
            title="Backend Engineer Hamburg",
            source_ids=["jsearch"],
            availability="active",
            completeness="full",
            target_state="deferred_duplicate",
        ),
        MemberBriefOut(
            job_id=second,
            title="Backend Engineer Berlin",
            source_ids=["jsearch"],
            availability="closed",
            completeness="partial",
            target_state="deferred_duplicate",
        ),
    ]
    # §8.7 L3342: the group is active while a member is, so the stale representative shows no stale badge
    assert (group_card.availability, group_card.badges) == ("active", [])
    assert group_card.possible_duplicate_of is None
    own = by_id[split]
    assert (own.kind, own.group, own.group_badge) == ("job", None, None)
    assert own.possible_duplicate_of == RefOut(
        job_id=rep, title="Backend Engineer", source_ids=["greenhouse"]
    )
    assert own.score is not None and own.score.mid == Decimal("40.00")  # ranked by its own score


async def test_an_unscorable_group_member_carries_the_group_badge(
    engine: AsyncEngine, active_tuple: int
) -> None:
    failed, refused = await seed_cards(engine, [Card(state="failed_request"), Card(state="refused")])
    async with engine.begin() as c:
        group = int(
            (
                await c.execute(
                    sa.text(
                        "INSERT INTO duplicate_groups (representative_job_id, status, unscorable_reason, "
                        "election_reason) VALUES (NULL, 'active', 'all_failed', 'initial') RETURNING id"
                    )
                )
            ).scalar_one()
        )
        await c.execute(
            sa.text("UPDATE jobs SET duplicate_group_id = :g WHERE id IN (:a, :b)"),
            {"g": group, "a": failed, "b": refused},
        )
    by_id = await listed(engine)
    for job_id in (failed, refused):
        card = by_id[job_id]
        assert card.group_badge is not None
        assert card.group_badge.model_dump() == {"group_id": group, "unscorable_reason": "all_failed"}
        assert (card.kind, card.group, card.possible_duplicate_of, card.bucket) == (
            "job",
            None,
            None,
            "failed",
        )


# --- the outbound links -----------------------------------------------------------------------------------


async def test_open_ad_follows_the_link_precedence_and_adzuna_is_separate(
    engine: AsyncEngine, active_tuple: int
) -> None:
    ats, publishers, adzuna_only, unsafe, none = await seed_cards(engine, [Card()] * 5)
    later = SEEN + timedelta(hours=5)
    # the employer's ATS link outranks a publisher's newer one and Adzuna's redirect
    await add_posting(engine, ats, "jsearch", observed_at=later, primary_link=PUBLISHER_LINK)
    await add_posting(engine, ats, "greenhouse", primary_link=GREENHOUSE_LINK)
    await add_posting(engine, ats, "adzuna", observed_at=later, redirect=ADZUNA_REDIRECT)
    # two publishers of one rank: the newer observation; Arbeitnow (rank 3) after JSearch (rank 2)
    await add_posting(engine, publishers, "jsearch", primary_link="https://old.publisher.example/ad/1")
    await add_posting(engine, publishers, "serpapi", observed_at=later, primary_link=PUBLISHER_LINK)
    await add_posting(
        engine,
        publishers,
        "arbeitnow",
        observed_at=later + timedelta(hours=1),
        primary_link="https://arbeitnow.example/ad/1",
    )
    # Adzuna alone: its redirect opens the ad and is its attribution link
    await add_posting(engine, adzuna_only, "adzuna", redirect=ADZUNA_REDIRECT)
    # a link that is no http(s) URL is passed over
    await add_posting(engine, unsafe, "greenhouse", primary_link="javascript:alert(1)")
    await add_posting(engine, unsafe, "jsearch", primary_link=PUBLISHER_LINK)
    async with read_snapshot(engine) as conn:
        links = await outbound_links(conn, [ats, publishers, adzuna_only, unsafe, none])
    assert links == {
        ats: OutboundLinks(open_ad=GREENHOUSE_LINK, adzuna=ADZUNA_REDIRECT),
        publishers: OutboundLinks(open_ad=PUBLISHER_LINK, adzuna=None),
        adzuna_only: OutboundLinks(open_ad=ADZUNA_REDIRECT, adzuna=ADZUNA_REDIRECT),
        unsafe: OutboundLinks(open_ad=PUBLISHER_LINK, adzuna=None),
        none: OutboundLinks(open_ad=None, adzuna=None),
    }
    by_id = {card.job_id: card for card in await cards(engine, [ats, none])}
    assert (by_id[ats].links.open_ad, by_id[ats].links.adzuna) == (GREENHOUSE_LINK, ADZUNA_REDIRECT)
    assert (by_id[none].links.open_ad, by_id[none].links.adzuna) == (None, None)


# --- no N+1 -----------------------------------------------------------------------------------------------


async def _seed_every_kind(engine: AsyncEngine, n: int) -> list[int]:
    """`n` card ids whose every block of five holds each kind of data a card can need: a card re-scored under
    a new tuple (its previous score), a group card with a deferred member, a card with a knockout, a card
    filtered by a keyword of its description, and a card with an outbound link."""
    await activate_scope(engine, excluded_keywords=["Praktikum"], exclude_keywords_in_description=True)
    blocks = n // 5
    old = await seed_cards(engine, [scored(60, 80)] * blocks)
    await activate_tuple(engine, n=2)
    specs: list[Card] = []
    for _ in range(blocks):
        specs += [
            Card(state="pending"),
            Card(state="pending", deferred_to=len(specs)),  # a deferred member of the card before it: no card
            scored(40, 60),
            scored(40, 60, description="Ein erfundenes Praktikum.", passed=False),
            Card(state="pending"),
        ]
    new = await seed_cards(engine, specs)
    groups, members, knocked, filtered, linked = (new[k::5] for k in range(5))
    del members
    record = {
        "code": "degree",
        "fact_id": "degree:degree",
        "comparison": "uncomparable",
        "severity": "soft",
        "binding": False,
        "source": "unconfirmed",
    }
    async with engine.begin() as c:
        for job_id in knocked:
            await c.execute(
                q("UPDATE det_results SET knockouts = :k WHERE job_id = :j", jsonb=["k"]),
                {"k": [record], "j": job_id},
            )
    for job_id in linked:
        await add_posting(engine, job_id, "jsearch", primary_link=PUBLISHER_LINK)
    return [card for block in zip(old, groups, knocked, filtered, linked, strict=True) for card in block]


async def test_one_statement_per_data_kind(engine: AsyncEngine, active_tuple: int) -> None:
    ids = await _seed_every_kind(engine, 50)
    statements: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool) -> None:
        statements.append(statement)

    counts: dict[int, Counter[str]] = {}
    for n in (5, 50):
        statements.clear()
        event.listen(engine.sync_engine, "before_cursor_execute", record)
        try:
            built = await cards(engine, ids[:n])
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", record)
        assert len(built) == n
        assert {card.kind for card in built} == {"job", "group"}
        assert all(card.previous is not None for card in built[::5])
        assert all(card.knockouts and card.filter_keyword is None for card in built[2::5])
        assert all(card.filter_keyword == "Praktikum" for card in built[3::5])
        assert all(card.links.open_ad == PUBLISHER_LINK for card in built[4::5])
        kinds = [s.split("*/", 1)[0].removeprefix(_TAG).strip() for s in statements if s.startswith(_TAG)]
        assert len(kinds) == len(statements) - 1  # and the snapshot's SET TRANSACTION
        counts[n] = Counter(kinds)
    assert counts[5] == counts[50]
    assert counts[50] == Counter(
        {kind: 1 for kind in ("active", "cards", "members", "knockouts", "previous", "descriptions", "links")}
    )
