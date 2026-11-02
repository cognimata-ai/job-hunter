"""`GET /api/jobs` and `GET /api/jobs/facets` over HTTP (spec §14.2, §14.3, §15.3; plan M4 Task 5, B7, B13,
B18, B20, B31).

A first page carries the snapshot's ranking generation, the capped counts and a cursor stamped with that
generation and the window's cut-off; a later page continues from its cursor under the same filters in any
order of their repeated parameters, is refused as `invalid_cursor` under other filters, raises
`ranking_changed` after a bump while its next cursor keeps the first page's generation, and keeps the first
page's posted cut-off an hour later. The limit is bounded, both routes need a session, the facets count the
listed cards' companies and sources, and a place of `GET /api/geo/places` filters the list as it is. The
clock is injected (`jobhunter.api.routes.jobs.utcnow`). Every datum is invented.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

import httpx
import pytest
import sqlalchemy as sa
from jobhunter.api.app import JobHunterApp
from jobhunter.api.pagination import JobCursor, cursor_key, decode_job_cursor
from jobhunter.api.routes import jobs as jobs_route
from jobhunter.jobs.query import JobsQuery, jobs_filters_hash
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.jobs_list import BERLIN, MUNICH, Card, add_posting, place, seed_cards

pytestmark = pytest.mark.integration

URL: Final = "/api/jobs"
FACETS_URL: Final = "/api/jobs/facets"
NOW: Final = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
SEEN: Final = NOW - timedelta(days=2)


def scored(low: int, high: int, **fields: Any) -> Card:
    card = Card(
        state="published_accepted", score=(Decimal(low), Decimal(high)), passed=True, first_seen_at=SEEN
    )
    return replace(card, **fields)


def pending(similarity: float | None, **fields: Any) -> Card:
    return replace(Card(state="pending", similarity=similarity, first_seen_at=SEEN), **fields)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[datetime]:
    """The routes' clock: `clock[0]`, which a test may move."""
    now = [NOW]
    monkeypatch.setattr(jobs_route, "utcnow", lambda: now[0])
    return now


async def get(client: httpx.AsyncClient, url: str = URL, **params: Any) -> dict[str, Any]:
    r = await client.get(url, params=params)
    assert r.status_code == 200, (r.status_code, r.text)
    body: dict[str, Any] = r.json()
    return body


def decoded(api_app: JobHunterApp, cursor: str, query: JobsQuery) -> JobCursor:
    """The position of a cursor the list issued for `query`."""

    def hashed(cut_off: str | None) -> str:
        return jobs_filters_hash(
            query, posted_after=None if cut_off is None else datetime.fromisoformat(cut_off)
        )

    return decode_job_cursor(
        cursor, key=cursor_key(api_app.api.state.cfg), sort=query.sort, filters_hash=hashed
    )


async def set_generation(engine: AsyncEngine, value: int) -> None:
    async with engine.begin() as c:
        await c.execute(sa.text("UPDATE ranking_state SET generation = :g"), {"g": value})


def ids(page: dict[str, Any]) -> list[int]:
    return [item["job_id"] for item in page["items"]]


async def test_first_page_has_generation_counts_and_a_cursor(
    logged_in_client: httpx.AsyncClient,
    api_app: JobHunterApp,
    engine: AsyncEngine,
    active_tuple: int,
    clock: Any,
) -> None:
    high, middle, low, waiting = await seed_cards(
        engine, [scored(70, 90), scored(50, 70), scored(30, 50), pending(0.4)]
    )
    await set_generation(engine, 5)
    page = await get(logged_in_client, limit=2)
    assert ids(page) == [high, middle]
    assert (page["ranking_generation"], page["ranking_changed"]) == (5, False)
    assert page["counts"] == {"total_estimate": 4, "unscored": 1, "capped": False}
    first = page["items"][0]
    assert first["score"]["mid"] == "80.00" and first["bucket"] == "scored" and first["kind"] == "job"
    assert first["first_seen_at"] == SEEN.isoformat().replace("+00:00", "Z")
    position = decoded(api_app, page["next_cursor"], JobsQuery())
    assert (position.section, position.last_value, position.job_id) == ("scored", "60.00", middle)
    assert (position.gen, position.posted_after) == (
        5,
        (NOW - timedelta(days=30)).isoformat(timespec="microseconds"),
    )
    rest = await get(logged_in_client, limit=2, cursor=page["next_cursor"])
    assert ids(rest) == [low, waiting]  # the scored section, then the unscored one
    assert (rest["next_cursor"], rest["counts"], rest["ranking_changed"]) == (None, None, False)


async def test_a_cursor_pages_on_under_reordered_multiselects(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, active_tuple: int, clock: Any
) -> None:
    every = await seed_cards(engine, [scored(10 * k, 10 * k + 20) for k in range(6)])
    one = await logged_in_client.get(
        URL,
        params=[
            ("limit", 3),
            ("work_mode", "remote"),
            ("work_mode", "hybrid"),
            ("work_mode", "unknown"),
            ("lang", "de"),
            ("lang", "en"),
        ],
    )
    assert one.status_code == 200, one.text
    cursor = one.json()["next_cursor"]
    two = await logged_in_client.get(
        URL,
        params=[
            ("limit", 3),
            ("lang", "en"),
            ("work_mode", "unknown"),
            ("lang", "de"),
            ("work_mode", "hybrid"),
            ("work_mode", "remote"),
            ("work_mode", "hybrid"),  # a repeat changes nothing
            ("cursor", cursor),
        ],
    )
    assert two.status_code == 200, two.text
    assert ids(one.json()) + ids(two.json()) == list(reversed(every))
    assert two.json()["next_cursor"] is None


async def test_a_changed_filter_is_an_invalid_cursor(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, active_tuple: int, clock: Any
) -> None:
    await seed_cards(engine, [scored(40, 60), scored(20, 40), scored(10, 20)])
    cursor = (await get(logged_in_client, limit=1, score_min=5))["next_cursor"]
    assert (
        await logged_in_client.get(URL, params={"limit": 1, "score_min": 5, "cursor": cursor})
    ).status_code == 200
    changes: list[dict[str, str | int]] = [
        {"score_min": 6},
        {},
        {"score_min": 5, "q": "Testrolle"},
        {"score_min": 5, "sort": "score_low"},
    ]
    for changed in changes:
        r = await logged_in_client.get(URL, params={"limit": 1, "cursor": cursor, **changed})
        assert r.status_code == 400, (changed, r.text)
        assert r.json()["code"] == "invalid_cursor"
    for broken in ("not-a-cursor", cursor[:-2] + "AA", cursor.split(".")[0] + ".x"):
        r = await logged_in_client.get(URL, params={"limit": 1, "score_min": 5, "cursor": broken})
        assert (r.status_code, r.json()["code"]) == (400, "invalid_cursor")


async def test_a_bump_between_pages_sets_ranking_changed_and_keeps_the_cursor_gen(
    logged_in_client: httpx.AsyncClient,
    api_app: JobHunterApp,
    engine: AsyncEngine,
    active_tuple: int,
    clock: Any,
) -> None:
    first, second, third = await seed_cards(engine, [scored(70, 90), scored(50, 70), scored(30, 50)])
    await set_generation(engine, 7)
    one = await get(logged_in_client, limit=1)
    await set_generation(engine, 8)
    two = await get(logged_in_client, limit=1, cursor=one["next_cursor"])
    assert (ids(two), two["ranking_generation"], two["ranking_changed"]) == ([second], 8, True)
    assert decoded(api_app, two["next_cursor"], JobsQuery()).gen == 7  # the first page's generation
    three = await get(logged_in_client, limit=1, cursor=two["next_cursor"])
    assert (ids(three), three["ranking_changed"]) == ([third], True)
    assert ids(one) == [first] and one["ranking_changed"] is False


async def test_posted_after_is_carried_by_the_cursor(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, active_tuple: int, clock: list[datetime]
) -> None:
    """Page 1 at `NOW` cuts `posted=24h` off at `NOW - 24 h`; page 2 an hour later still does, so the card
    posted 23.5 hours before page 1 (23.5 + 1 hours before page 2) is on it."""
    newest, edge = await seed_cards(
        engine,
        [
            pending(None, posted_at=NOW - timedelta(hours=1), first_seen_at=NOW - timedelta(hours=1)),
            pending(
                None, posted_at=NOW - timedelta(hours=23, minutes=30), first_seen_at=NOW - timedelta(hours=23)
            ),
        ],
    )
    one = await get(logged_in_client, sort="posted", posted="24h", limit=1)
    assert ids(one) == [newest]
    clock[0] = NOW + timedelta(hours=1)
    two = await get(logged_in_client, sort="posted", posted="24h", limit=1, cursor=one["next_cursor"])
    assert ids(two) == [edge]
    fresh = await get(logged_in_client, sort="posted", posted="24h", limit=10)
    assert ids(fresh) == [newest]  # a first page an hour later cuts it off


async def test_limit_bounds_and_401_without_session(
    logged_in_client: httpx.AsyncClient,
    api_client: httpx.AsyncClient,
    engine: AsyncEngine,
    active_tuple: int,
    clock: Any,
) -> None:
    await seed_cards(engine, [scored(40, 60)] * 3)
    assert len((await get(logged_in_client, limit=100))["items"]) == 3
    assert len((await get(logged_in_client))["items"]) == 3  # default 50
    for url in (URL, FACETS_URL):
        for limit in (0, 101, "x"):
            r = await logged_in_client.get(url, params={"limit": limit})
            assert r.status_code == 422, (url, limit, r.text)
            assert r.json()["code"] == "validation_failed"
            assert [e["field"] for e in r.json()["errors"]] == ["query.limit"]
    too_long = await logged_in_client.get(URL, params={"cursor": "a" * 2049})
    assert (too_long.status_code, [e["field"] for e in too_long.json()["errors"]]) == (422, ["query.cursor"])
    refused = await logged_in_client.get(URL, params={"score_min": 60, "score_max": 40})
    assert (refused.status_code, [e["field"] for e in refused.json()["errors"]]) == (422, ["query.score_max"])
    for url in (URL, FACETS_URL):
        r = await api_client.get(url)
        assert (r.status_code, r.json()["code"]) == (401, "unauthenticated")


async def test_facets_count_companies_and_sources_and_filter_by_prefix(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, active_tuple: int, clock: Any
) -> None:
    specs = [
        *[scored(40, 60, company_key="acme", source_ids=("greenhouse", "jsearch"))] * 3,
        scored(40, 60, company_key="acme", hidden=True),  # hidden: not listed, not counted
        *[scored(40, 60, company_key="beta", source_ids=("adzuna",))] * 2,
        scored(40, 60, company_key="arzte plus", source_ids=("jsearch",)),
    ]
    cards = await seed_cards(engine, specs)
    async with engine.begin() as c:
        names = {"acme": "Acme GmbH", "beta": "Beta AG", "arzte plus": "Ärzte Plus"}
        for key, name in names.items():
            await c.execute(
                sa.text("UPDATE jobs SET company_display = :n WHERE company_key = :k"), {"n": name, "k": key}
            )
        # one Acme card under another spelling: the name shown is the most frequent one
        await c.execute(sa.text("UPDATE jobs SET company_display = 'ACME' WHERE id = :j"), {"j": cards[0]})
    every = await get(logged_in_client, FACETS_URL)
    assert every["companies"] == [
        {"company_key": "acme", "display_name": "Acme GmbH", "count": 3},
        {"company_key": "beta", "display_name": "Beta AG", "count": 2},
        {"company_key": "arzte plus", "display_name": "Ärzte Plus", "count": 1},
    ]
    assert every["sources"] == [
        {"source_id": "jsearch", "count": 4},
        {"source_id": "greenhouse", "count": 3},
        {"source_id": "adzuna", "count": 2},
    ]
    folded = await get(logged_in_client, FACETS_URL, company_q="Ä")
    assert [c["company_key"] for c in folded["companies"]] == ["acme", "arzte plus"]  # name or key, folded
    assert [
        c["company_key"] for c in (await get(logged_in_client, FACETS_URL, company_q="bet"))["companies"]
    ] == ["beta"]
    assert (await get(logged_in_client, FACETS_URL, company_q="gmbh"))["companies"] == []  # a prefix only
    # the prefix picks companies, not cards: the `ACME` card counts for the company `Acme GmbH` matched
    by_spelling = await get(logged_in_client, FACETS_URL, company_q="acme g")
    assert by_spelling["companies"] == [{"company_key": "acme", "display_name": "Acme GmbH", "count": 3}]
    # each facet under every filter but its own: a chosen company still offers the others
    chosen = await get(logged_in_client, FACETS_URL, company="beta")
    assert [c["company_key"] for c in chosen["companies"]] == ["acme", "beta", "arzte plus"]
    assert chosen["sources"] == [{"source_id": "adzuna", "count": 2}]
    by_source = await get(logged_in_client, FACETS_URL, source="adzuna")
    assert [c["company_key"] for c in by_source["companies"]] == ["beta"]
    assert [s["source_id"] for s in by_source["sources"]] == ["jsearch", "greenhouse", "adzuna"]
    hidden_too = await logged_in_client.get(
        FACETS_URL, params=[("status", s) for s in ("unseen", "seen", "saved", "applied", "hidden")]
    )
    assert hidden_too.json()["companies"][0] == {
        "company_key": "acme",
        "display_name": "Acme GmbH",
        "count": 4,
    }
    assert len((await get(logged_in_client, FACETS_URL, limit=1))["companies"]) == 1
    nul = await logged_in_client.get(FACETS_URL, params={"company_q": "a\x00"})
    assert (nul.status_code, [e["field"] for e in nul.json()["errors"]]) == (422, ["query.company_q"])


async def test_a_geo_places_result_filters_the_list(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, active_tuple: int, clock: Any
) -> None:
    berlin, munich, berlin_too = await seed_cards(
        engine,
        [
            scored(60, 80, locations=(place(BERLIN),), location_kind="city"),
            scored(50, 70, locations=(place(MUNICH),), location_kind="city"),
            scored(40, 60, locations=(place(BERLIN),), location_kind="city"),
        ],
    )
    places = await logged_in_client.get("/api/geo/places", params={"q": "Berlin"})
    assert places.status_code == 200, places.text
    first = places.json()[0]
    assert first["label"] == "Berlin" and isinstance(first["gazetteer_id"], str)
    near = {
        "loc_id": first["gazetteer_id"],
        "loc_lat": first["lat"],
        "loc_lon": first["lon"],
        "radius_km": 30,
    }
    page = await get(logged_in_client, **near)
    assert ids(page) == [berlin, berlin_too] and munich not in ids(page)
    one = await get(logged_in_client, limit=1, **near)
    two = await get(
        logged_in_client, limit=1, cursor=one["next_cursor"], **near
    )  # the same place hashes equal
    assert ids(one) + ids(two) == [berlin, berlin_too]
    as_integer = await logged_in_client.get(URL, params={**near, "loc_id": 2950159})
    assert (as_integer.status_code, [e["field"] for e in as_integer.json()["errors"]]) == (
        422,
        ["query.loc_id"],
    )
    partial = await logged_in_client.get(URL, params={"loc_id": first["gazetteer_id"], "radius_km": 30})
    assert partial.status_code == 422
    assert sorted(e["field"] for e in partial.json()["errors"]) == ["query.loc_lat", "query.loc_lon"]


async def test_the_outbound_links_reach_the_page_and_never_a_log_line(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, active_tuple: int, clock: Any, log_events: Any
) -> None:
    (job_id,) = await seed_cards(engine, [scored(40, 60, source_ids=("adzuna",))])
    redirect = "https://www.adzuna.example/land/ad/99?se=invented-tracking"
    await add_posting(engine, job_id, "adzuna", redirect=redirect)
    page = await get(logged_in_client)
    assert page["items"][0]["links"] == {"open_ad": redirect, "adzuna": redirect}
    assert page["items"][0]["badges"] == ["adzuna"]
    logged = repr(log_events())
    assert "adzuna.example" not in logged and "invented-tracking" not in logged


async def test_a_foreign_position_is_an_invalid_cursor_but_a_first_page_value_error_is_internal(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, active_tuple: int, clock: Any, monkeypatch: Any
) -> None:
    """`select_page` refuses a position no page of the sort ends at with `ValueError`: 400 `invalid_cursor` on
    a page with a cursor; on a first page the same error is a bug, the last-resort 500 `internal`."""
    await seed_cards(engine, [scored(40, 60), scored(20, 40)])
    cursor = (await get(logged_in_client, limit=1))["next_cursor"]

    async def refused(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("not a position of the score_mid sort")

    monkeypatch.setattr(jobs_route, "select_page", refused)
    later = await logged_in_client.get(URL, params={"limit": 1, "cursor": cursor})
    assert (later.status_code, later.json()["code"]) == (400, "invalid_cursor")
    first = await logged_in_client.get(URL, params={"limit": 1})
    assert (first.status_code, first.json()["code"]) == (500, "internal")
