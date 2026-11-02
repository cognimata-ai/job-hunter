"""The Jobs list engine (spec §15.3, §14.3; plan M4 Task 4, R8, B1, B2, B4, B5, B8, B15, B18, A1 #33).

Every page is selected in a read snapshot (`db.snapshot.read_snapshot`) over cards that
`tests/support/jobs_list.py` seeds. The score sorts list the scored section (by the selected score variant,
then job id, both descending) and then the unscored one (by similarity, NULLS LAST, then job id); the date
sorts one section. A page never skips or repeats a row across the NULLS LAST boundary, a deferred group member
is never a row but its fields match its representative's card, and the counts of the first page stop at the
cap.
"""

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Any, Final

import pytest
import sqlalchemy as sa
from jobhunter.api.pagination import JobSection, JobSort
from jobhunter.db.snapshot import read_snapshot
from jobhunter.jobs import listing
from jobhunter.jobs._sql import TRGM_WORD_THRESHOLD
from jobhunter.jobs.listing import Counts, PageSelection, Position, SelectedRow, select_page
from jobhunter.jobs.query import JobsQuery, LocationFilter
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.jobs_list import (
    BERLIN,
    HAMBURG,
    MUNICH,
    NORTH_OF_BERLIN,
    SEEN,
    Card,
    activate_tuple,
    place,
    seed_cards,
)
from tests.support.m2 import insert_profile_chain
from tests.support.m3 import active_ids, insert_det_result, insert_job, insert_published

pytestmark = pytest.mark.integration

Window = tuple[datetime | None, datetime | None]
ANY: Final[Window] = (None, None)
ALL_STATES: Final = ["scored", "unscored", "filtered", "failed", "expired", "not_a_job_ad"]
NEAR_BERLIN: Final = LocationFilter(
    gazetteer_id="geonames:2950159", lat=BERLIN[0], lon=BERLIN[1], radius_km=30
)


def scored(low: int, high: int, **fields: Any) -> Card:
    """A published card with a current score instance `[low, high]` and a passed hard filter."""
    card = Card(state="published_accepted", score=(Decimal(low), Decimal(high)), passed=True)
    return replace(card, **fields)


def pending(similarity: float | None, **fields: Any) -> Card:
    return replace(Card(state="pending", similarity=similarity), **fields)


async def page(
    engine: AsyncEngine,
    query: JobsQuery,
    *,
    after: Position | None = None,
    limit: int = 20,
    window: Window = ANY,
    with_counts: bool = False,
) -> PageSelection:
    async with read_snapshot(engine) as conn:
        return await select_page(
            conn, query, window=window, after=after, limit=limit, with_counts=with_counts
        )


async def ids(engine: AsyncEngine, query: JobsQuery, *, window: Window = ANY) -> list[int]:
    """The job ids of the first page of up to 100 rows."""
    return [row.job_id for row in (await page(engine, query, limit=100, window=window)).rows]


async def walk(
    engine: AsyncEngine, query: JobsQuery, *, limit: int, window: Window = ANY
) -> list[list[SelectedRow]]:
    """Every page of the list, from the first to the one without a next position."""
    pages: list[list[SelectedRow]] = []
    after: Position | None = None
    while True:
        selected = await page(engine, query, after=after, limit=limit, window=window)
        pages.append(selected.rows)
        assert len(selected.rows) <= limit
        if selected.next is None:
            return pages
        assert len(selected.rows) == limit
        last = selected.rows[-1]
        assert selected.next == Position(last.section, last.sort_value, last.job_id)
        after = selected.next
        assert len(pages) < 50, "the walk does not end"


def flat(pages: list[list[SelectedRow]]) -> list[int]:
    return [row.job_id for rows in pages for row in rows]


async def test_scored_section_orders_by_mid_then_job_id_and_continues_into_unscored(
    engine: AsyncEngine, active_tuple: int
) -> None:
    high, low, first, second = await seed_cards(
        engine, [scored(60, 80), scored(40, 60), pending(0.70), pending(0.40)]
    )
    one = await page(engine, JobsQuery(), limit=3)
    assert [(r.job_id, r.section, r.sort_value) for r in one.rows] == [
        (high, "scored", Decimal("70.00")),
        (low, "scored", Decimal("50.00")),
        (first, "unscored", pytest.approx(0.70)),
    ]
    assert one.next is not None and one.next.section == "unscored" and one.next.job_id == first
    two = await page(engine, JobsQuery(), after=one.next, limit=3)
    assert [(r.job_id, r.section) for r in two.rows] == [(second, "unscored")]
    assert two.next is None
    # one row per page walks the same order, across the section boundary
    pages = await walk(engine, JobsQuery(), limit=1)
    assert flat(pages) == [high, low, first, second]
    assert [rows[0].section for rows in pages] == ["scored", "scored", "unscored", "unscored"]

    # equal mids order by job id, descending; a score is exact to the cent
    tied = await seed_cards(engine, [scored(50, 60), scored(54, 56)])
    assert (await ids(engine, JobsQuery()))[:4] == [high, tied[1], tied[0], low]
    assert isinstance(one.rows[0].sort_value, Decimal)


async def test_score_low_sort_orders_by_the_lower_bound(engine: AsyncEngine, active_tuple: int) -> None:
    narrow, wide, middle = await seed_cards(engine, [scored(60, 62), scored(50, 90), scored(60, 70)])
    assert await ids(engine, JobsQuery(sort="score_mid")) == [wide, middle, narrow]  # mids 70, 65, 61
    selected = await page(engine, JobsQuery(sort="score_low"))
    assert [(r.job_id, r.sort_value) for r in selected.rows] == [
        (middle, Decimal("60.00")),  # the tie at 60 by job id, descending
        (narrow, Decimal("60.00")),
        (wide, Decimal("50.00")),
    ]
    # the keyset continues on the lower bound
    first = await page(engine, JobsQuery(sort="score_low"), limit=1)
    assert first.next == Position("scored", Decimal("60.00"), middle)
    rest = await page(engine, JobsQuery(sort="score_low"), after=first.next, limit=5)
    assert [r.job_id for r in rest.rows] == [narrow, wide]


async def test_unscored_section_orders_by_similarity_nulls_last_then_job_id(
    engine: AsyncEngine, active_tuple: int
) -> None:
    low, top, unknown, untargeted, tied, failed, not_an_ad = await seed_cards(
        engine,
        [
            pending(0.3),
            pending(0.9),
            pending(None),
            Card(),  # no target of the active tuple
            pending(0.9),
            Card(state="failed_request", similarity=0.5),
            Card(state="published_accepted", similarity=0.6, not_a_job_ad=True),
        ],
    )
    selected = await page(engine, JobsQuery())
    assert [r.job_id for r in selected.rows] == [tied, top, failed, low, untargeted, unknown]
    assert {r.section for r in selected.rows} == {"unscored"}
    assert [r.sort_value for r in selected.rows][-2:] == [None, None]
    # a not-a-job-ad card is listed only when its bucket is selected, by its similarity
    every = JobsQuery(scoring_state=ALL_STATES)
    assert await ids(engine, every) == [tied, top, not_an_ad, failed, low, untargeted, unknown]
    assert flat(await walk(engine, every, limit=2)) == [
        tied,
        top,
        not_an_ad,
        failed,
        low,
        untargeted,
        unknown,
    ]


@pytest.mark.parametrize("sort", ["posted", "score_mid"])
@pytest.mark.parametrize("limit", [1, 2, 3, 4])
async def test_nulls_last_keyset_never_skips_a_row_across_the_branch_boundary(
    engine: AsyncEngine, active_tuple: int, sort: JobSort, limit: int
) -> None:
    """Values on job ids 10, 30 and 50 and NULL on 20, 40 and 100 (as offsets: the first card is id 10): the
    sort value of the `posted` sort is the posting date, that of the unscored section the similarity."""
    valued = {10: 0.3, 30: 0.6, 50: 0.9}  # 50 ranks first
    section: JobSection
    if sort == "posted":
        cards = {k: Card(posted_at=SEEN - (1 - v) * timedelta(days=10)) for k, v in valued.items()}
        cards |= {k: Card() for k in (20, 40, 100)}
        section = "all"
    else:
        cards = {k: pending(v) for k, v in valued.items()}
        cards |= {20: pending(None), 40: Card(), 100: pending(None)}  # no similarity yet, or no target
        section = "unscored"
    offsets = sorted(cards)
    gaps = [0, *(b - a - 1 for a, b in pairwise(offsets))]
    made = await seed_cards(
        engine, [replace(cards[k], skip_ids=gap) for k, gap in zip(offsets, gaps, strict=True)]
    )
    assert [job - made[0] + 10 for job in made] == offsets  # the cards sit on the chosen ids
    on = dict(zip(offsets, made, strict=True))
    expected = [on[k] for k in (50, 30, 10, 100, 40, 20)]

    pages = await walk(engine, JobsQuery(sort=sort), limit=limit)
    assert flat(pages) == expected
    assert {row.section for rows in pages for row in rows} == {section}
    assert [row.sort_value is None for rows in pages for row in rows] == [False] * 3 + [True] * 3
    if limit == 3:  # the first page ends at the last valued row, the second starts with the first NULL one
        assert flat(pages[:1]) == expected[:3]
        assert pages[1][0].job_id == on[100]
    # a position inside the NULL branch continues below its job id
    inside = await page(engine, JobsQuery(sort=sort), after=Position(section, None, on[40]), limit=10)
    assert [r.job_id for r in inside.rows] == [on[20]]


async def test_without_a_tuple_every_card_is_unscored_by_job_id(engine: AsyncEngine) -> None:
    """No active tuple (A1 #33): every card is unscored, even one scored under a tuple that is not active."""
    async with engine.begin() as c:
        chain = await insert_profile_chain(c, 2)  # never activated
        job = await insert_job(c)
        result = await insert_published(c, job, chain.llm_tuple_id)
        scope = (await active_ids(c)).scope_version_id
        await insert_det_result(c, result.result_id, job.job_id, scope, low=Decimal(80), high=Decimal(90))
    cards = [job.job_id, *await seed_cards(engine, [Card(), Card(), Card()])]
    for sort in ("score_mid", "score_low"):
        selected = await page(engine, JobsQuery(sort=sort), with_counts=True)
        assert [(r.job_id, r.section, r.sort_value) for r in selected.rows] == [
            (card, "unscored", None) for card in sorted(cards, reverse=True)
        ]
        assert selected.counts == Counts(total=4, unscored=4, capped=False)
    assert await ids(engine, JobsQuery(scoring_state=["scored"])) == []
    pages = await walk(engine, JobsQuery(), limit=3)
    assert flat(pages) == sorted(cards, reverse=True)


async def test_a_deferred_member_is_never_a_row_and_its_filters_match_the_card(
    engine: AsyncEngine, active_tuple: int
) -> None:
    rep, member, other = await seed_cards(
        engine,
        [
            scored(60, 80, locations=(place(MUNICH),), location_kind="city"),
            Card(locations=(place(BERLIN),), location_kind="city", deferred_to=0, similarity=0.4),
            scored(40, 50, locations=(place(HAMBURG),), location_kind="city"),
        ],
    )
    assert await ids(engine, JobsQuery()) == [rep, other]
    in_berlin = JobsQuery(loc=NEAR_BERLIN, include_remote=False)
    for sort in ("score_mid", "posted", "first_seen"):
        assert await ids(engine, in_berlin.model_copy(update={"sort": sort})) == [rep]
    near_hamburg = NEAR_BERLIN.model_copy(update={"lat": HAMBURG[0], "lon": HAMBURG[1]})
    assert await ids(engine, JobsQuery(loc=near_hamburg, include_remote=False)) == [other]
    assert member not in await ids(
        engine, JobsQuery(scoring_state=ALL_STATES, status=["unseen", "seen", "hidden"])
    )
    counted = await page(engine, in_berlin, with_counts=True)
    assert counted.counts == Counts(total=1, unscored=0, capped=False)


async def test_keyword_finds_softwareentwickler_by_entwickler(engine: AsyncEngine, active_tuple: int) -> None:
    senior, plain, marketing, english = await seed_cards(
        engine,
        [
            Card(title="Senior Softwareentwickler Java Backend (m/w/d)"),
            Card(title="Softwareentwickler"),
            Card(title="Marketing Manager", description="Wir suchen Verstärkung im Marketing."),
            Card(
                title="Platform Lead",
                ad_language="en",
                description="We are hiring engineers for an invented platform team.",
            ),
        ],
    )
    assert sorted(await ids(engine, JobsQuery(q="Entwickler"))) == sorted([senior, plain])
    assert await ids(engine, JobsQuery(q="engineer")) == [english]
    assert await ids(engine, JobsQuery(q="Marketing")) == [marketing]
    assert await ids(engine, JobsQuery(q="Zahnarzt")) == []
    for wordless in ("   ", "&|!"):  # no lexeme and no trigram: nothing matches, nothing fails
        assert await ids(engine, JobsQuery(q=wordless)) == []
    for sort in ("score_mid", "posted", "first_seen"):
        found = await ids(engine, JobsQuery(q="Entwickler", sort=sort))
        assert sorted(found) == sorted([senior, plain])


async def test_location_matches_any_resolved_candidate_and_never_unresolved_or_country_wide(
    engine: AsyncEngine, active_tuple: int
) -> None:
    unique, ambiguous, multi, unresolved, country_wide, munich = await seed_cards(
        engine,
        [
            Card(locations=(place(BERLIN),), location_kind="city"),
            Card(locations=(place(MUNICH, NORTH_OF_BERLIN, resolution="ambiguous"),), location_kind="city"),
            Card(locations=(place(MUNICH), place(BERLIN)), location_kind="multi"),
            Card(locations=(place(BERLIN, resolution="unresolved"),), location_kind="unknown"),
            Card(locations=(place(BERLIN),), location_kind="country_wide"),
            Card(locations=(place(MUNICH),), location_kind="city"),
        ],
    )
    found = await ids(engine, JobsQuery(loc=NEAR_BERLIN, include_remote=False))
    assert sorted(found) == sorted([unique, ambiguous, multi])
    narrow = NEAR_BERLIN.model_copy(update={"radius_km": 10})  # the ambiguous candidate is 20 km north
    assert sorted(await ids(engine, JobsQuery(loc=narrow, include_remote=False))) == sorted([unique, multi])
    assert unresolved not in found and country_wide not in found and munich not in found


async def test_include_remote_needs_a_known_remote_mode(engine: AsyncEngine, active_tuple: int) -> None:
    in_munich = Card(locations=(place(MUNICH),), location_kind="city")
    structured, remote_kind, unknown_mode, onsite = await seed_cards(
        engine,
        [
            replace(in_munich, work_mode_possible=("remote",), work_mode_source="structured"),
            Card(location_kind="remote"),
            in_munich,  # work_mode_source 'none': all three modes possible
            replace(in_munich, work_mode_possible=("onsite",), work_mode_source="text"),
        ],
    )
    found = await ids(engine, JobsQuery(loc=NEAR_BERLIN, include_remote=True))
    assert sorted(found) == sorted([structured, remote_kind])
    assert await ids(engine, JobsQuery(loc=NEAR_BERLIN, include_remote=False)) == []
    assert unknown_mode not in found and onsite not in found


async def test_work_mode_unknown_is_the_jobs_without_a_known_mode(
    engine: AsyncEngine, active_tuple: int
) -> None:
    remote, onsite, unknown = await seed_cards(
        engine,
        [
            Card(work_mode_possible=("remote", "hybrid"), work_mode_source="text"),
            Card(work_mode_possible=("onsite",), work_mode_source="structured"),
            Card(),
        ],
    )
    assert sorted(await ids(engine, JobsQuery())) == sorted([remote, onsite, unknown])
    assert await ids(engine, JobsQuery(work_mode=["remote"])) == [remote]
    assert await ids(engine, JobsQuery(work_mode=["unknown"])) == [unknown]
    assert sorted(await ids(engine, JobsQuery(work_mode=["hybrid", "onsite"]))) == sorted([remote, onsite])


async def test_the_field_filters_match_their_columns(engine: AsyncEngine, active_tuple: int) -> None:
    a, b, c = await seed_cards(
        engine,
        [
            Card(
                company_key="alpha",
                source_ids=("jsearch", "greenhouse"),
                ad_language="en",
                employment_types=("full_time",),
                title="Senior Data Engineer",
                completeness="partial",
            ),
            Card(
                company_key="beta", source_ids=("adzuna",), employment_types=(), structured_seniority="junior"
            ),
            Card(company_key="gamma", ad_language="other", availability="stale", title="Data Engineer"),
        ],
    )
    assert await ids(engine, JobsQuery(company=["alpha", "beta"])) == [b, a]
    assert await ids(engine, JobsQuery(source=["greenhouse"])) == [a]
    assert await ids(engine, JobsQuery(lang=["de"])) == [b]
    assert await ids(engine, JobsQuery(employment=["full_time"])) == [a]
    assert await ids(engine, JobsQuery(employment=["unknown"])) == [b]
    assert await ids(engine, JobsQuery(seniority=["senior"])) == [a]
    assert await ids(engine, JobsQuery(seniority=["junior"])) == [b]
    assert await ids(engine, JobsQuery(seniority=["mid"], availability=["active", "stale"])) == [c]
    assert await ids(engine, JobsQuery(partial="only")) == [a]
    assert await ids(engine, JobsQuery(partial="exclude")) == [b]
    assert await ids(engine, JobsQuery(availability=["stale"])) == [c]
    assert c not in await ids(engine, JobsQuery())  # stale is not re-confirmed: not listed by default


async def test_scoring_state_buckets_are_exclusive_and_the_default_excludes_not_a_job_ad(
    engine: AsyncEngine, active_tuple: int
) -> None:
    made = await seed_cards(
        engine,
        [
            scored(60, 80),
            pending(0.5),
            Card(),
            Card(state="budget_deferred", similarity=0.2),
            Card(state="filtered_out", similarity=0.6),
            scored(70, 90, passed=False),  # published, now failing a hard filter
            Card(state="refused", similarity=0.1),
            Card(state="failed_attempts_exhausted"),
            Card(state="expired_unscored", similarity=0.3),
            Card(state="published_accepted", not_a_job_ad=True),
            Card(state="cancelled", similarity=0.9),  # never listed
        ],
    )
    s, p, none, deferred, filtered, failing, refused, exhausted, expired, not_an_ad, cancelled = made
    buckets = {
        "scored": {s},
        "unscored": {p, none, deferred},
        "filtered": {filtered, failing},
        "failed": {refused, exhausted},
        "expired": {expired},
        "not_a_job_ad": {not_an_ad},
    }
    for sort in ("score_mid", "score_low", "posted", "first_seen"):
        for bucket, expected in buckets.items():
            query = JobsQuery(sort=sort, scoring_state=[bucket])
            assert set(await ids(engine, query)) == expected, (sort, bucket)
        default = await ids(engine, JobsQuery(sort=sort))
        assert sorted(default) == sorted(set(made) - {not_an_ad, cancelled}), sort
        every = await ids(engine, JobsQuery(sort=sort, scoring_state=ALL_STATES))
        assert sorted(every) == sorted(set(made) - {cancelled}), sort
    sections = {r.job_id: r.section for r in (await page(engine, JobsQuery(scoring_state=ALL_STATES))).rows}
    assert {job for job, section in sections.items() if section == "scored"} == {s, failing}


async def test_an_active_score_filter_lists_scored_rows_only(engine: AsyncEngine, active_tuple: int) -> None:
    high, low, medium, wide, _, _ = await seed_cards(
        engine,
        [
            scored(60, 80),  # mid 70
            scored(20, 40),  # mid 30
            scored(70, 90, coverage=Decimal("0.5"), confidence="medium"),  # mid 80
            scored(50, 90, confidence="low"),  # mid 70, low 50
            pending(0.9),
            Card(state="published_accepted", not_a_job_ad=True),
        ],
    )
    every = {"scoring_state": ALL_STATES}
    for sort in ("score_mid", "posted", "first_seen"):
        query = JobsQuery(sort=sort, score_min=40, **every)
        assert sorted(await ids(engine, query)) == sorted([high, medium, wide]), sort
    assert await ids(engine, JobsQuery(score_max=50, **every)) == [low]
    assert await ids(engine, JobsQuery(coverage_min=60, **every)) == [wide, high, low]  # the tie by job id
    assert await ids(engine, JobsQuery(confidence="high", **every)) == [high, low]
    assert await ids(engine, JobsQuery(confidence="medium", **every)) == [medium, high, low]
    # the range is on the sort's variant: the lower bound under score_low
    assert await ids(engine, JobsQuery(score_min=65, **every)) == [medium, wide, high]
    low_sort = JobsQuery(sort="score_low", score_min=55, **every)
    assert await ids(engine, low_sort) == [medium, high]
    counted = await page(engine, JobsQuery(score_min=1, **every), with_counts=True)
    assert counted.counts == Counts(total=4, unscored=0, capped=False)


async def test_contested_only_and_exclude(engine: AsyncEngine, active_tuple: int) -> None:
    accepted, contested, waiting = await seed_cards(
        engine, [scored(60, 80), scored(50, 70, state="published_contested"), pending(0.4)]
    )
    assert await ids(engine, JobsQuery(contested="only")) == [contested]
    assert await ids(engine, JobsQuery(contested="exclude")) == [accepted, waiting]
    assert await ids(engine, JobsQuery(contested="only", sort="posted")) == [contested]


async def test_posted_within_uses_first_seen_when_the_date_is_unknown(
    engine: AsyncEngine, active_tuple: int
) -> None:
    now = SEEN
    day = timedelta(days=1)
    recent, old_post, unknown_recent, unknown_old = await seed_cards(
        engine,
        [
            Card(posted_at=now - 3 * day, first_seen_at=now - 2 * day),
            Card(posted_at=now - 10 * day, first_seen_at=now - day),  # a known date decides, not the sighting
            Card(first_seen_at=now - 2 * day),
            Card(first_seen_at=now - 10 * day),
        ],
    )
    week = (now - 7 * day, None)
    for sort in ("score_mid", "posted", "first_seen"):
        found = await ids(engine, JobsQuery(sort=sort), window=week)
        assert sorted(found) == sorted([recent, unknown_recent]), sort
    custom = (now - 12 * day, now - 5 * day)  # the upper bound is exclusive
    assert sorted(await ids(engine, JobsQuery(), window=custom)) == sorted([old_post, unknown_old])
    assert await ids(engine, JobsQuery(), window=(now - 10 * day, now - 3 * day)) == [unknown_old, old_post]


async def test_user_status_or_semantics_with_hidden_excluded_by_default(
    engine: AsyncEngine, active_tuple: int
) -> None:
    unseen, seen, saved, applied, hidden, hidden_saved, hidden_unseen = await seed_cards(
        engine,
        [
            Card(),
            Card(seen=True),
            Card(seen=True, saved=True),
            Card(seen=True, applied=True),
            Card(seen=True, hidden=True),
            Card(seen=True, saved=True, hidden=True),
            Card(hidden=True),
        ],
    )

    async def having(*status: str) -> set[int]:
        return set(await ids(engine, JobsQuery(status=list(status))))

    assert set(await ids(engine, JobsQuery())) == {unseen, seen, saved, applied}
    assert await having("unseen") == {unseen}
    assert await having("seen") == {seen, saved, applied}
    assert await having("saved") == {saved}
    assert await having("saved", "applied") == {saved, applied}
    assert await having("hidden") == {hidden, hidden_saved, hidden_unseen}
    assert await having("saved", "hidden") == {saved, hidden, hidden_saved, hidden_unseen}
    assert await having("unseen", "seen", "saved", "applied", "hidden") == {
        unseen,
        seen,
        saved,
        applied,
        hidden,
        hidden_saved,
        hidden_unseen,
    }


async def test_counts_count_cards_on_the_first_page_and_cap(
    engine: AsyncEngine, active_tuple: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    cards = [
        scored(60, 80),
        scored(40, 60),
        scored(30, 50),
        scored(20, 30),
        pending(0.5),
        pending(None),
        Card(),
    ]
    made = await seed_cards(engine, [*cards, Card(deferred_to=0), Card(hidden=True), Card(state="cancelled")])
    uncapped = await page(engine, JobsQuery(), limit=2, with_counts=True)
    assert uncapped.counts == Counts(total=7, unscored=3, capped=False)
    assert (await page(engine, JobsQuery(), limit=2)).counts is None  # not asked for
    filtered = await page(engine, JobsQuery(scoring_state=["unscored"]), with_counts=True)
    assert filtered.counts == Counts(total=3, unscored=3, capped=False)
    dated = await page(engine, JobsQuery(sort="first_seen"), with_counts=True)
    assert dated.counts == Counts(total=7, unscored=3, capped=False)

    monkeypatch.setattr(listing, "COUNT_CAP", 7)  # exactly the cap: counted, not capped
    at_cap = await page(engine, JobsQuery(), limit=2, with_counts=True)
    assert at_cap.counts == Counts(total=7, unscored=3, capped=False)

    monkeypatch.setattr(listing, "COUNT_CAP", 5)
    first = await page(engine, JobsQuery(), limit=2, with_counts=True)
    assert first.counts == Counts(total=5, unscored=3, capped=True)
    assert [r.job_id for r in first.rows] == [made[0], made[1]]
    second = await page(engine, JobsQuery(), after=first.next, limit=2, with_counts=True)
    assert second.counts is None


async def test_the_word_similarity_threshold_is_b4s(engine: AsyncEngine, active_tuple: int) -> None:
    """The title branch of the keyword (`<%`) runs at pg_trgm's `word_similarity_threshold`, which the list
    never sets: as the app's role, after a keyword page has loaded pg_trgm, it is B4's 0.6."""
    await seed_cards(engine, [Card(title="Softwareentwickler")])
    async with read_snapshot(engine) as conn:
        selected = await select_page(
            conn, JobsQuery(q="Entwickler"), window=ANY, after=None, limit=5, with_counts=False
        )
        shown = (await conn.execute(sa.text("SHOW pg_trgm.word_similarity_threshold"))).scalar_one()
    assert len(selected.rows) == 1  # found by the title alone: the threshold took part
    assert float(shown) == TRGM_WORD_THRESHOLD


async def test_a_group_card_sorts_by_its_representatives_dates(
    engine: AsyncEngine, active_tuple: int
) -> None:
    day = timedelta(days=1)
    rep, member, other = await seed_cards(
        engine,
        [
            scored(60, 80, posted_at=SEEN - 5 * day, first_seen_at=SEEN - 5 * day),
            Card(posted_at=SEEN - 1 * day, first_seen_at=SEEN - 1 * day, deferred_to=0),
            scored(40, 50, posted_at=SEEN - 3 * day, first_seen_at=SEEN - 3 * day),
        ],
    )
    for sort in ("posted", "first_seen"):
        selected = await page(engine, JobsQuery(sort=sort))
        assert [(r.job_id, r.section, r.sort_value) for r in selected.rows] == [
            (other, "all", SEEN - 3 * day),
            (rep, "all", SEEN - 5 * day),
        ], sort
        # the member's date matches the posted window, the card keeps its representative's place
        recent = await page(engine, JobsQuery(sort=sort), window=(SEEN - 2 * day, None))
        assert [(r.job_id, r.sort_value) for r in recent.rows] == [(rep, SEEN - 5 * day)], sort
    assert member not in await ids(engine, JobsQuery(sort="posted"))


async def test_a_date_position_is_utc_and_a_page_ends_where_the_list_does(
    engine: AsyncEngine, active_tuple: int
) -> None:
    day = timedelta(days=1)
    first, second = await seed_cards(engine, [Card(posted_at=SEEN - day), Card(posted_at=SEEN - 2 * day)])
    one = await page(engine, JobsQuery(sort="posted"), limit=1)
    assert one.next is not None and isinstance(one.next.value, datetime)
    assert one.next.value.utcoffset() == timedelta(0) and one.next.value == SEEN - day
    two = await page(engine, JobsQuery(sort="posted"), after=one.next, limit=1)
    assert [r.job_id for r in two.rows] == [second] and two.next is None  # exactly `limit` rows left
    assert [r.job_id for r in (await page(engine, JobsQuery(sort="posted"), limit=2)).rows] == [first, second]


async def test_a_position_of_another_sort_is_refused(engine: AsyncEngine, active_tuple: int) -> None:
    await seed_cards(engine, [Card()])
    with pytest.raises(ValueError, match="position"):
        await page(engine, JobsQuery(sort="posted"), after=Position("scored", Decimal("50.00"), 1))
    with pytest.raises(ValueError, match="position"):
        await page(engine, JobsQuery(), after=Position("scored", 0.5, 1))
    with pytest.raises(ValueError, match="position"):
        await page(engine, JobsQuery(sort="first_seen"), after=Position("all", None, 1))
    with pytest.raises(ValueError, match="limit"):
        await page(engine, JobsQuery(), limit=0)


async def test_a_second_tuple_activated_later_is_the_one_read(engine: AsyncEngine, active_tuple: int) -> None:
    """The engine reads the tuple `active_config` names in the snapshot: cards scored under the old tuple are
    unscored under the new one."""
    old, waiting = await seed_cards(engine, [scored(60, 80), pending(0.5)])
    assert [r.section for r in (await page(engine, JobsQuery())).rows] == ["scored", "unscored"]
    await activate_tuple(engine, n=2)
    assert [(r.job_id, r.section) for r in (await page(engine, JobsQuery())).rows] == [
        (waiting, "unscored"),
        (old, "unscored"),
    ]
