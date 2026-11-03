"""`GET /api/queries` and `GET /api/queries/stats` (spec §14.2, §15.7; plan M4 Task 13, D1, M2 Carry-forward
2, M1 Carry-forward 3's per-query gap counts; Rulings T13-a, T13-b, T13-d and T13-e).

The query list is the live queries by id, each with its flag in the active settings (false for a query they
do not list), judged on the current scope whether enabled or not: its planned cells are one per enabled search
source and scope location (`cells` counts them whether a run has created them yet or not). Over them: the
coverage of its planned locations (a location is covered as of the newest frontier of its JSearch cell and
its SerpApi fallback cell; the query as of the oldest location, null when one is covered by neither, its cells
missing included, or without a planned paid source), the open gaps, the mean yield over the cells a run has
searched, whether one is too broad (a paid cell left incomplete by 3 runs in a row) and the dry-run's JSearch
refresh cadence when JSearch is planned. The stats list every existing planned cell of a live query with its
own figures. The queries are created and enabled through the api (a settings save creates the enabled ones'
cells); the other cells and every interval are inserted directly. The routes' clock is injected
(`jobhunter.api.routes.queries.utcnow`). Every query text is an invented job title.
"""

import copy
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Final

import httpx
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.queries import listing
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.collect import free_cell
from tests.support.settings import DRY_RUN_URL, berlin, save, settings_body

pytestmark = pytest.mark.integration

URL: Final = "/api/queries"
STATS: Final = "/api/queries/stats"
QUERIES_UTCNOW: Final = "jobhunter.api.routes.queries.utcnow"
HAMBURG: Final[dict[str, Any]] = {
    "label": "Hamburg",
    "gazetteer_id": "geonames:2911298",
    "lat": 53.55,
    "lon": 9.99,
    "radius_km": 25,
}
SEARCH_SOURCES: Final = ("adzuna", "jsearch", "serpapi")
# The cells a settings save makes for an enabled query with a Berlin location: (source, location key).
SAVED_CELLS: Final = (
    ("adzuna", "berlin"),
    ("adzuna", "remote-de"),
    ("jsearch", "berlin"),
    ("jsearch", "remote-de"),
    ("serpapi", "berlin"),
    ("serpapi", "remote-de"),
)
CELL_OF: Final = sa.text(
    "SELECT c.id FROM coverage_cells c JOIN source_instances i ON i.id = c.source_instance_id "
    "WHERE c.query_id = :q AND i.source_id = :s AND i.instance_key = 'default' AND c.location_key = :k"
)
INSERT_CELL: Final = sa.text(
    "INSERT INTO coverage_cells (source_instance_id, query_id, location_key) SELECT id, :q, :k "
    "FROM source_instances WHERE source_id = :s AND instance_key = 'default' RETURNING id"
)
SET_CELL: Final = sa.text(
    "UPDATE coverage_cells SET last_attempted_at = :attempted, last_covered_at = :covered, "
    "last_success_at = :success, yield_ema = :ema, zero_yield_streak = :streak, "
    "consecutive_incomplete = :incomplete WHERE id = :c"
)
INTERVAL: Final = sa.text(
    "INSERT INTO coverage_intervals (cell_id, kind, span, gap_status, reason) VALUES (:c, :kind, "
    "tstzrange(:lower, :upper, '[)'), :status, CASE WHEN :kind = 'gap' THEN 'page_cap' END)"
)
RETIRED_QUERY: Final = sa.text(
    "INSERT INTO queries (text, lang, origin, retired_at) VALUES (:t, 'en', 'user', :at) RETURNING id"
)

Cells = dict[tuple[str, str], int]


@pytest.fixture
def now(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """The routes' clock, fixed for the test."""
    moment = utcnow()
    monkeypatch.setattr(QUERIES_UTCNOW, lambda: moment)
    return moment


def at(text: str | None) -> datetime | None:
    return None if text is None else datetime.fromisoformat(text)


async def listed(client: httpx.AsyncClient, url: str = URL) -> list[dict[str, Any]]:
    r = await client.get(url)
    assert r.status_code == 200, (r.status_code, r.text)
    body: list[dict[str, Any]] = r.json()
    return body


async def created(client: httpx.AsyncClient, text: str) -> int:
    r = await client.post(URL, json={"text": text, "lang": "en"})
    assert r.status_code == 201, (r.status_code, r.text)
    return int(r.json()["id"])


async def in_settings(
    client: httpx.AsyncClient,
    flags: Mapping[int, bool],
    *,
    locations: Sequence[dict[str, Any]] = (berlin(30),),
    remote: bool = True,
    disabled_sources: Iterable[str] = (),
) -> dict[str, Any]:
    """The active payload with `queries[]` = `flags`, the scope's `locations` and remote work within Germany
    as `remote`, and `disabled_sources` switched off, saved; the saved payload."""
    payload = copy.deepcopy((await settings_body(client))["payload"])
    payload["scope"]["locations"] = list(locations)
    payload["scope"]["remote_within_germany"] = remote
    payload["queries"] = [{"query_id": query_id, "enabled": enabled} for query_id, enabled in flags.items()]
    for source_id in disabled_sources:
        payload["sources"]["enabled"][source_id] = False
    await save(client, payload)
    saved: dict[str, Any] = (await settings_body(client))["payload"]
    return saved


async def dry_run_cadence(client: httpx.AsyncClient, payload: Mapping[str, Any]) -> float:
    """The settings dry-run's `projections.jsearch.refresh_cadence_days` for `payload`."""
    r = await client.post(DRY_RUN_URL, json={"payload": payload})
    assert r.status_code == 200, (r.status_code, r.text)
    cadence = r.json()["projections"]["jsearch"]["refresh_cadence_days"]
    assert isinstance(cadence, float) and cadence > 0
    return cadence


async def cell(engine: AsyncEngine, query_id: int, source_id: str, location_key: str) -> int:
    """The default instance's cell of `source_id` for the query and place, inserted when absent."""
    params = {"q": query_id, "s": source_id, "k": location_key}
    async with engine.begin() as c:
        found = (await c.execute(CELL_OF, params)).scalar_one_or_none()
        return int(found if found is not None else (await c.execute(INSERT_CELL, params)).scalar_one())


async def cells_of(
    engine: AsyncEngine, query_id: int, keys: Iterable[tuple[str, str]] = SAVED_CELLS
) -> Cells:
    return {key: await cell(engine, query_id, *key) for key in keys}


async def set_cell(
    engine: AsyncEngine,
    cell_id: int,
    *,
    attempted: datetime | None = None,
    covered: datetime | None = None,
    success: datetime | None = None,
    ema: float = 0.5,
    streak: int = 0,
    incomplete: int = 0,
) -> None:
    """The cell's statistics; a covered cell was attempted when it was covered, unless `attempted` says
    otherwise, and a cell with neither an attempt nor a success was never searched."""
    params = {
        "c": cell_id,
        "attempted": covered if attempted is None else attempted,
        "covered": covered,
        "success": success,
        "ema": ema,
        "streak": streak,
        "incomplete": incomplete,
    }
    async with engine.begin() as c:
        await c.execute(SET_CELL, params)


async def covered_at(
    engine: AsyncEngine, cells: Cells, frontiers: Mapping[tuple[str, str], datetime]
) -> None:
    """Each cell of `frontiers` covered up to its time; the others keep theirs (never covered)."""
    for key, frontier in frontiers.items():
        await set_cell(engine, cells[key], covered=frontier)


async def intervals(engine: AsyncEngine, cell_id: int, *kinds: tuple[str, str | None], at: datetime) -> None:
    """One interval per `(kind, gap_status)`, each an hour of its own before `at`."""
    async with engine.begin() as c:
        for n, (kind, status) in enumerate(kinds, start=1):
            lower, upper = at - timedelta(hours=n + 1), at - timedelta(hours=n)
            params = {"c": cell_id, "kind": kind, "lower": lower, "upper": upper, "status": status}
            await c.execute(INTERVAL, params)


def by_id(items: Sequence[Mapping[str, Any]]) -> dict[int, Mapping[str, Any]]:
    return {int(item["id"]): item for item in items}


def figures(query: Mapping[str, Any]) -> dict[str, Any]:
    """A listed query's settings flag and cell aggregates, its times parsed."""
    return {
        "enabled": query["enabled"],
        "cells": query["cells"],
        "last_covered_at": at(query["last_covered_at"]),
        "open_gaps": query["open_gaps"],
        "yield_ema": query["yield_ema"],
        "too_broad": query["too_broad"],
        "refresh_cadence_days": query["refresh_cadence_days"],
    }


async def figures_of(client: httpx.AsyncClient, query_id: int) -> dict[str, Any]:
    return figures(by_id(await listed(client))[query_id])


async def stats_keys(client: httpx.AsyncClient, query_id: int) -> list[tuple[str, str]]:
    """The `(source, location key)` of the query's rows in the stats, in their order."""
    return [
        (x["source_id"], x["location_key"]) for x in await listed(client, STATS) if x["query_id"] == query_id
    ]


NO_CELLS: Final = {
    "cells": 0,
    "last_covered_at": None,
    "open_gaps": 0,
    "yield_ema": None,
    "too_broad": False,
    "refresh_cadence_days": None,
}


# --- GET /api/queries -------------------------------------------------------------------------------------


async def test_queries_list_the_live_rows_with_enabled_and_their_cell_aggregates(
    engine: AsyncEngine, logged_in_client: httpx.AsyncClient, now: datetime
) -> None:
    a = await created(logged_in_client, "Platform Engineer")
    b = await created(logged_in_client, "Data Engineer")
    c = await created(logged_in_client, "Site Reliability Engineer")
    d = await created(logged_in_client, "Backend Developer")
    # a enabled (the save makes its six cells), b listed but disabled; c and d live, not listed. Each is
    # planned six cells: three search sources at Berlin and remote-de.
    payload = await in_settings(logged_in_client, {a: True, b: False})
    cadence = await dry_run_cadence(logged_in_client, payload)
    hours = [now - timedelta(hours=n) for n in range(11)]
    a_cells = await cells_of(engine, a)
    stats: dict[tuple[str, str], dict[str, Any]] = {
        # Berlin is covered as of JSearch's frontier (its SerpApi cell never was), remote-de as of the newer
        # of its two; the query as of the older location, remote-de. Adzuna's frontiers are no paid ones. The
        # yield is the mean of the four searched cells: the never-searched two keep the prior 0.5.
        ("jsearch", "berlin"): {"covered": hours[1], "success": hours[1], "ema": 0.25, "incomplete": 2},
        ("jsearch", "remote-de"): {"covered": hours[5], "success": hours[5], "ema": 0.5},
        ("serpapi", "berlin"): {},
        ("serpapi", "remote-de"): {"covered": hours[3], "ema": 1.0},
        ("adzuna", "berlin"): {"covered": hours[10], "ema": 0.125, "streak": 4},
        ("adzuna", "remote-de"): {},
    }
    for key, values in stats.items():
        await set_cell(engine, a_cells[key], **values)
    await intervals(
        engine, a_cells["jsearch", "berlin"], ("gap", "open"), ("gap", "open"), ("gap", "closed"), at=now
    )
    await intervals(engine, a_cells["adzuna", "berlin"], ("gap", "open"), ("gap", "abandoned"), at=now)
    await intervals(engine, a_cells["serpapi", "berlin"], ("covered", None), at=now)
    # b and c, though not enabled, have cells at the scope's Berlin, each with an open gap; at remote-de none
    b_cells = await cells_of(engine, b, [("jsearch", "berlin"), ("serpapi", "berlin")])
    await set_cell(engine, b_cells["serpapi", "berlin"], covered=hours[1], ema=0.75)
    await intervals(engine, b_cells["jsearch", "berlin"], ("gap", "open"), at=now)
    c_cell = await cell(engine, c, "adzuna", "berlin")
    await set_cell(engine, c_cell, covered=hours[1], ema=0.375)
    await intervals(engine, c_cell, ("gap", "open"), at=now)

    body = await listed(logged_in_client)

    assert [q["id"] for q in body] == [a, b, c, d]
    first = body[0]
    assert {k: v for k, v in first.items() if k not in figures(first)} == {
        "id": a,
        "text": "Platform Engineer",
        "lang": "en",
        "origin": "user",
        "exploration": False,
        "proposal_id": None,
        "created_at": first["created_at"],
    }
    assert at(first["created_at"]) == now
    got = {query_id: figures(query) for query_id, query in by_id(body).items()}
    assert got[a] == {
        "enabled": True,
        "cells": 6,
        "last_covered_at": hours[3],
        "open_gaps": 3,
        "yield_ema": pytest.approx((0.25 + 0.5 + 1.0 + 0.125) / 4),
        "too_broad": False,
        "refresh_cadence_days": cadence,
    }
    assert got[b] == {
        "enabled": False,
        "cells": 6,  # its planned cells, two of which exist
        "last_covered_at": None,  # remote-de has no cell of it, so it is covered by neither
        "open_gaps": 1,
        "yield_ema": pytest.approx(0.75),  # its SerpApi cell, the one searched
        "too_broad": False,
        "refresh_cadence_days": cadence,
    }
    assert got[c] == {
        "enabled": False,
        "cells": 6,
        "last_covered_at": None,  # no paid cell covered
        "open_gaps": 1,
        "yield_ema": pytest.approx(0.375),
        "too_broad": False,
        "refresh_cadence_days": cadence,  # JSearch is planned for it, as for every query
    }
    assert got[d] == {
        "enabled": False,
        "cells": 6,  # none exists yet
        "last_covered_at": None,
        "open_gaps": 0,
        "yield_ema": None,  # none searched
        "too_broad": False,
        "refresh_cadence_days": cadence,
    }
    # each query's gaps are those of its cells in the stats, which list its existing cells
    cells = await listed(logged_in_client, STATS)
    existing = {a: 6, b: 2, c: 1, d: 0}
    for query_id in (a, b, c, d):
        own = [x for x in cells if x["query_id"] == query_id]
        counted = (len(own), sum(x["open_gaps"] for x in own))
        assert counted == (existing[query_id], got[query_id]["open_gaps"])


async def test_a_query_with_a_paid_cell_three_times_incomplete_is_too_broad(
    engine: AsyncEngine, logged_in_client: httpx.AsyncClient, now: datetime
) -> None:
    jsearch_broad = await created(logged_in_client, "Software Engineer")
    serpapi_broad = await created(logged_in_client, "Software Developer")
    twice = await created(logged_in_client, "Frontend Developer")
    free = await created(logged_in_client, "Fullstack Developer")
    queries = (jsearch_broad, serpapi_broad, twice, free)
    await in_settings(logged_in_client, dict.fromkeys(queries, True))
    incomplete = {
        (jsearch_broad, "jsearch"): 3,
        (serpapi_broad, "serpapi"): 4,
        (twice, "jsearch"): 2,  # one run short
        (free, "adzuna"): 5,  # Adzuna is free
    }
    for (query_id, source_id), n in incomplete.items():
        await set_cell(engine, await cell(engine, query_id, source_id, "berlin"), incomplete=n)

    listing_ = by_id(await listed(logged_in_client))
    assert {query_id: listing_[query_id]["too_broad"] for query_id in queries} == {
        jsearch_broad: True,
        serpapi_broad: True,
        twice: False,
        free: False,
    }
    cells = await listed(logged_in_client, STATS)
    assert len(cells) == 4 * len(SAVED_CELLS)
    assert {
        (x["query_id"], x["source_id"]): (x["consecutive_incomplete"], x["too_broad"])
        for x in cells
        if x["consecutive_incomplete"] or x["too_broad"]
    } == {
        (jsearch_broad, "jsearch"): (3, True),
        (serpapi_broad, "serpapi"): (4, True),
        (twice, "jsearch"): (2, False),
        (free, "adzuna"): (5, False),
    }


async def test_a_retired_query_is_not_listed(
    engine: AsyncEngine, logged_in_client: httpx.AsyncClient, now: datetime
) -> None:
    old = await created(logged_in_client, "Data Engineer")
    await in_settings(logged_in_client, {old: True})
    old_cell = await cell(engine, old, "jsearch", "berlin")  # one of the six the save made
    await intervals(engine, old_cell, ("gap", "open"), at=now)
    r = await logged_in_client.patch(f"{URL}/{old}", json={"text": "Data Engineer Kafka"})  # retire + create
    assert r.status_code == 200, r.text
    new = int(r.json()["id"])

    body = await listed(logged_in_client)
    assert [(q["id"], q["text"], q["enabled"], q["cells"], q["open_gaps"]) for q in body] == [
        (new, "Data Engineer Kafka", True, 6, 0)  # the entry moved to it, and its own cells were made
    ]
    assert {x["query_id"] for x in await listed(logged_in_client, STATS)} == {new}


async def test_a_location_covered_by_jsearch_whose_serpapi_cell_is_not_gives_the_jsearch_time(
    engine: AsyncEngine, logged_in_client: httpx.AsyncClient, now: datetime
) -> None:
    query_id = await created(logged_in_client, "Platform Engineer")
    await in_settings(logged_in_client, {query_id: True}, remote=False)  # Berlin, the one location
    cells = await cells_of(engine, query_id, [k for k in SAVED_CELLS if k[1] == "berlin"])
    await covered_at(engine, cells, {("jsearch", "berlin"): now - timedelta(hours=2)})  # SerpApi's never

    assert (await figures_of(logged_in_client, query_id))["last_covered_at"] == now - timedelta(hours=2)


async def test_two_locations_give_the_older_coverage(
    engine: AsyncEngine, logged_in_client: httpx.AsyncClient, now: datetime
) -> None:
    query_id = await created(logged_in_client, "Platform Engineer")
    await in_settings(logged_in_client, {query_id: True})
    cells = await cells_of(engine, query_id)
    frontiers = {
        ("jsearch", "berlin"): now - timedelta(hours=1),  # Berlin: the newer of its two
        ("serpapi", "berlin"): now - timedelta(hours=6),
        ("serpapi", "remote-de"): now - timedelta(hours=4),  # remote-de: its SerpApi cell alone
        ("adzuna", "remote-de"): now - timedelta(hours=9),  # no paid cell
    }
    await covered_at(engine, cells, frontiers)

    assert (await figures_of(logged_in_client, query_id))["last_covered_at"] == now - timedelta(hours=4)


async def test_a_location_covered_by_neither_paid_cell_gives_null(
    engine: AsyncEngine, logged_in_client: httpx.AsyncClient, now: datetime
) -> None:
    query_id = await created(logged_in_client, "Platform Engineer")
    await in_settings(logged_in_client, {query_id: True})
    cells = await cells_of(engine, query_id)
    frontiers = {
        ("jsearch", "berlin"): now - timedelta(hours=1),
        ("serpapi", "berlin"): now - timedelta(hours=2),
        ("adzuna", "remote-de"): now - timedelta(hours=1),  # remote-de: its Adzuna cell alone, no paid one
    }
    await covered_at(engine, cells, frontiers)

    assert (await figures_of(logged_in_client, query_id))["last_covered_at"] is None


async def test_a_cell_of_a_location_removed_from_the_scope_is_not_counted_or_listed(
    engine: AsyncEngine, logged_in_client: httpx.AsyncClient, now: datetime
) -> None:
    query_id = await created(logged_in_client, "Platform Engineer")
    await in_settings(logged_in_client, {query_id: True})
    cells = await cells_of(engine, query_id)
    hours = [now - timedelta(hours=n) for n in range(10)]
    await set_cell(engine, cells["jsearch", "berlin"], covered=hours[9], ema=0.0, incomplete=3)
    await set_cell(engine, cells["adzuna", "berlin"], attempted=hours[9], ema=0.0)
    await set_cell(engine, cells["serpapi", "berlin"], attempted=hours[9], ema=0.0)
    await intervals(engine, cells["serpapi", "berlin"], ("gap", "open"), at=now)
    await set_cell(engine, cells["jsearch", "remote-de"], covered=hours[1], ema=1.0)
    before = await figures_of(logged_in_client, query_id)
    assert (before["cells"], before["last_covered_at"], before["open_gaps"], before["too_broad"]) == (
        6,
        hours[9],
        1,
        True,
    )
    assert before["yield_ema"] == pytest.approx(0.25)  # the four searched cells: 0, 0, 0 and 1

    # Berlin removed; remote-de stays
    payload = await in_settings(logged_in_client, {query_id: True}, locations=())

    assert await figures_of(logged_in_client, query_id) == {
        "enabled": True,
        "cells": 3,
        "last_covered_at": hours[1],
        "open_gaps": 0,
        "yield_ema": pytest.approx(1.0),  # remote-de's one searched cell
        "too_broad": False,
        "refresh_cadence_days": await dry_run_cadence(logged_in_client, payload),
    }
    assert await stats_keys(logged_in_client, query_id) == [
        ("adzuna", "remote-de"),
        ("jsearch", "remote-de"),
        ("serpapi", "remote-de"),
    ]


async def test_a_cell_of_a_disabled_source_is_not_counted_or_listed(
    engine: AsyncEngine, logged_in_client: httpx.AsyncClient, now: datetime
) -> None:
    query_id = await created(logged_in_client, "Platform Engineer")
    await in_settings(logged_in_client, {query_id: True})
    cells = await cells_of(engine, query_id)
    hours = [now - timedelta(hours=n) for n in range(10)]
    await set_cell(engine, cells["jsearch", "berlin"], covered=hours[1], ema=0.0)
    await set_cell(engine, cells["jsearch", "remote-de"], covered=hours[2], ema=0.0, incomplete=3)
    await intervals(engine, cells["jsearch", "berlin"], ("gap", "open"), ("gap", "open"), at=now)
    await set_cell(engine, cells["serpapi", "berlin"], covered=hours[5], ema=1.0)
    await set_cell(engine, cells["serpapi", "remote-de"], covered=hours[3], ema=1.0)
    await intervals(engine, cells["serpapi", "remote-de"], ("gap", "open"), at=now)
    before = await figures_of(logged_in_client, query_id)
    assert (before["cells"], before["last_covered_at"], before["open_gaps"], before["too_broad"]) == (
        6,
        hours[2],  # Berlin as of JSearch (1 h), remote-de as of JSearch (2 h)
        3,
        True,
    )
    assert before["yield_ema"] == pytest.approx(0.5)  # the four searched cells: 0, 0, 1 and 1
    assert before["refresh_cadence_days"] is not None

    await in_settings(logged_in_client, {query_id: True}, disabled_sources=["jsearch"])

    assert await figures_of(logged_in_client, query_id) == {
        "enabled": True,
        "cells": 4,
        "last_covered_at": hours[5],  # SerpApi's alone now: Berlin 5 h, remote-de 3 h
        "open_gaps": 1,
        "yield_ema": pytest.approx(1.0),  # the two searched SerpApi cells
        "too_broad": False,
        "refresh_cadence_days": None,  # JSearch is not planned
    }
    assert await stats_keys(logged_in_client, query_id) == [
        ("adzuna", "berlin"),
        ("adzuna", "remote-de"),
        ("serpapi", "berlin"),
        ("serpapi", "remote-de"),
    ]


async def test_a_disabled_query_lists_enabled_false_with_its_cells_and_coverage_at_the_current_scope(
    engine: AsyncEngine, logged_in_client: httpx.AsyncClient, now: datetime
) -> None:
    query_id = await created(logged_in_client, "Platform Engineer")
    other = await created(logged_in_client, "Data Engineer")
    await in_settings(logged_in_client, {query_id: True, other: True})
    cells = await cells_of(engine, query_id)
    hours = [now - timedelta(hours=n) for n in range(10)]
    await set_cell(engine, cells["jsearch", "berlin"], covered=hours[9], ema=0.0, incomplete=3)
    await intervals(engine, cells["jsearch", "berlin"], ("gap", "open"), at=now)
    await set_cell(engine, cells["jsearch", "remote-de"], covered=hours[4], ema=1.0)
    await set_cell(engine, cells["serpapi", "remote-de"], covered=hours[2], ema=0.25)
    await intervals(engine, cells["adzuna", "remote-de"], ("gap", "open"), ("gap", "open"), at=now)

    # disabled, and Berlin removed from the scope: its remote-de cells are what it would search again
    payload = await in_settings(logged_in_client, {query_id: False, other: True}, locations=())
    cadence = await dry_run_cadence(logged_in_client, payload)

    assert await figures_of(logged_in_client, query_id) == {
        "enabled": False,
        "cells": 3,
        "last_covered_at": hours[2],  # remote-de as of the newer of its two paid cells
        "open_gaps": 2,
        "yield_ema": pytest.approx((1.0 + 0.25) / 2),  # its two searched cells
        "too_broad": False,  # its too broad cell is Berlin's
        "refresh_cadence_days": cadence,
    }
    assert await stats_keys(logged_in_client, query_id) == [
        ("adzuna", "remote-de"),
        ("jsearch", "remote-de"),
        ("serpapi", "remote-de"),
    ]


async def test_a_disabled_query_is_judged_on_a_location_added_to_the_scope_since(
    engine: AsyncEngine, logged_in_client: httpx.AsyncClient, now: datetime
) -> None:
    query_id = await created(logged_in_client, "Platform Engineer")
    await in_settings(logged_in_client, {query_id: True})
    cells = await cells_of(engine, query_id)
    hour_ago = now - timedelta(hours=1)
    await covered_at(engine, cells, {("jsearch", "berlin"): hour_ago, ("jsearch", "remote-de"): hour_ago})
    enabled = await figures_of(logged_in_client, query_id)
    assert (enabled["cells"], enabled["last_covered_at"]) == (6, hour_ago)

    # disabled, then Hamburg joins the scope: the save makes no cell of it there
    await in_settings(logged_in_client, {query_id: False})
    await in_settings(logged_in_client, {query_id: False}, locations=(berlin(30), HAMBURG))

    disabled = await figures_of(logged_in_client, query_id)
    assert (disabled["enabled"], disabled["cells"], disabled["last_covered_at"]) == (False, 9, None)
    assert len(await stats_keys(logged_in_client, query_id)) == 6  # Hamburg's three cells do not exist yet


async def test_the_yield_is_the_mean_of_the_searched_cells(
    engine: AsyncEngine, logged_in_client: httpx.AsyncClient, now: datetime
) -> None:
    measured = await created(logged_in_client, "Platform Engineer")
    unsearched = await created(logged_in_client, "Data Engineer")
    await in_settings(logged_in_client, {measured: True, unsearched: True}, remote=False)  # Berlin alone
    cells = await cells_of(engine, measured, [k for k in SAVED_CELLS if k[1] == "berlin"])
    hour_ago = now - timedelta(hours=1)
    await set_cell(engine, cells["jsearch", "berlin"], attempted=hour_ago, success=hour_ago, ema=0.0)
    await set_cell(engine, cells["adzuna", "berlin"], attempted=hour_ago, ema=0.0)
    # its SerpApi cell, never searched, keeps the prior 0.5

    listing_ = by_id(await listed(logged_in_client))
    assert listing_[measured]["yield_ema"] == 0.0
    assert listing_[unsearched]["yield_ema"] is None  # none of its cells was searched
    stats = {
        x["source_id"]: x["yield_ema"]
        for x in await listed(logged_in_client, STATS)
        if x["query_id"] == measured
    }
    assert stats == {"adzuna": 0.0, "jsearch": 0.0, "serpapi": 0.5}  # each cell shows its own


async def test_without_a_planned_search_source_no_query_has_cells(
    engine: AsyncEngine, logged_in_client: httpx.AsyncClient, now: datetime
) -> None:
    query_id = await created(logged_in_client, "Platform Engineer")
    await in_settings(logged_in_client, {query_id: True})
    await set_cell(engine, await cell(engine, query_id, "jsearch", "berlin"), covered=now, ema=0.0)

    await in_settings(logged_in_client, {query_id: True}, disabled_sources=SEARCH_SOURCES)

    assert await figures_of(logged_in_client, query_id) == {"enabled": True, **NO_CELLS}
    assert await listed(logged_in_client, STATS) == []


async def test_without_settings_every_live_query_is_listed_without_cells(
    engine: AsyncEngine, logged_in_client: httpx.AsyncClient, now: datetime
) -> None:
    query_id = await created(logged_in_client, "Platform Engineer")
    await in_settings(logged_in_client, {query_id: True})
    async with engine.connect() as conn:  # nothing seeded, in a transaction rolled back
        await conn.execute(sa.text("DELETE FROM active_config"))
        queries = await listing.query_list(conn, now=now)
        cells = await listing.cell_stats(conn, now=now)
        await conn.rollback()

    assert [(q.id, q.enabled, q.cells, q.last_covered_at, q.refresh_cadence_days) for q in queries] == [
        (query_id, False, 0, None, None)
    ]
    assert cells == []


# --- GET /api/queries/stats -------------------------------------------------------------------------------


async def test_stats_list_every_cell_with_open_gaps(
    engine: AsyncEngine, logged_in_client: httpx.AsyncClient, now: datetime
) -> None:
    a = await created(logged_in_client, "Platform Engineer")
    b = await created(logged_in_client, "Data Engineer")
    unlisted = await created(logged_in_client, "Backend Developer")
    payload = await in_settings(logged_in_client, {a: True, b: True})
    cadence = await dry_run_cadence(logged_in_client, payload)
    a_cells, b_cells = await cells_of(engine, a), await cells_of(engine, b)
    covered, success = now - timedelta(hours=6), now - timedelta(hours=2)
    await set_cell(
        engine,
        a_cells["jsearch", "berlin"],
        covered=covered,
        success=success,
        ema=0.75,
        streak=1,
        incomplete=1,
    )
    await intervals(engine, a_cells["jsearch", "berlin"], ("gap", "open"), ("gap", "open"), at=now)
    await intervals(engine, a_cells["serpapi", "remote-de"], ("gap", "open"), ("gap", "closed"), at=now)
    await intervals(engine, a_cells["adzuna", "berlin"], ("covered", None), ("gap", "abandoned"), at=now)
    await intervals(engine, b_cells["jsearch", "berlin"], ("gap", "open"), at=now)
    # listed: the cell at the scope's Berlin of a query the settings do not list
    unlisted_cell = await cell(engine, unlisted, "jsearch", "berlin")
    await intervals(engine, unlisted_cell, ("gap", "open"), at=now)
    # not listed, each with an open gap: a retired query's cell, a cell at a place outside the scope, and
    # Arbeitnow's cell without a query
    async with engine.begin() as conn:
        retired = int((await conn.execute(RETIRED_QUERY, {"t": "Python Developer", "at": now})).scalar_one())
        arbeitnow = await free_cell(conn, "arbeitnow")
    await intervals(engine, await cell(engine, retired, "jsearch", "berlin"), ("gap", "open"), at=now)
    await intervals(engine, await cell(engine, unlisted, "jsearch", "hamburg"), ("gap", "open"), at=now)
    await intervals(engine, arbeitnow, ("gap", "open"), at=now)

    body = await listed(logged_in_client, STATS)

    gaps = {(a, "jsearch", "berlin"): 2, (a, "serpapi", "remote-de"): 1, (b, "jsearch", "berlin"): 1}
    blank: dict[str, Any] = {
        "last_covered_at": None,
        "last_success_at": None,
        "yield_ema": 0.5,
        "zero_yield_streak": 0,
        "consecutive_incomplete": 0,
        "too_broad": False,
    }
    expected: list[dict[str, Any]] = [
        {
            "cell_id": own[source_id, key],
            "query_id": query_id,
            "source_id": source_id,
            "instance_label": "default",
            "location_key": key,
            **blank,
            "open_gaps": gaps.get((query_id, source_id, key), 0),
            "cadence_days": cadence if source_id == "jsearch" else None,
        }
        for query_id, own in ((a, a_cells), (b, b_cells))
        for source_id, key in SAVED_CELLS  # in (source, location key) order
    ]
    expected[2] |= {
        "last_covered_at": covered,
        "last_success_at": success,
        "yield_ema": 0.75,
        "zero_yield_streak": 1,
        "consecutive_incomplete": 1,
    }
    expected.append(
        {
            "cell_id": unlisted_cell,
            "query_id": unlisted,
            "source_id": "jsearch",
            "instance_label": "default",
            "location_key": "berlin",
            **blank,
            "open_gaps": 1,
            "cadence_days": cadence,
        }
    )
    got = [
        x | {"last_covered_at": at(x["last_covered_at"]), "last_success_at": at(x["last_success_at"])}
        for x in body
    ]
    assert got == expected
