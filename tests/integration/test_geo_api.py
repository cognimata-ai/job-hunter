"""`GET /api/geo/places` (spec §8.12, §14.2; plan Task 22, Deviation 3): the scope-location autocomplete,
served from the bundled gazetteer. No network, no database beyond the session.
"""

import asyncio
import threading
from typing import Any, Final

import httpx
import pytest
from jobhunter.api.app import JobHunterApp
from jobhunter.geo.gazetteer import Gazetteer

pytestmark = pytest.mark.integration

URL: Final = "/api/geo/places"
MUENCHEN: Final = "geonames:2867714"
BERLIN: Final = "geonames:2950159"
HAMBURG: Final = "geonames:2911298"
FIELDS: Final = {"gazetteer_id", "label", "postal_codes", "lat", "lon", "population"}


async def places(client: httpx.AsyncClient, **params: Any) -> list[dict[str, Any]]:
    r = await client.get(URL, params=params)
    assert r.status_code == 200, (r.status_code, r.text)
    out: list[dict[str, Any]] = r.json()
    return out


@pytest.mark.parametrize("q", ["münch", "Munich", "MÜNCHEN", "  munch "])
async def test_a_name_or_another_spelling_lists_muenchen_first(logged_in_client, q: str) -> None:
    found = await places(logged_in_client, q=q)
    assert found[0]["gazetteer_id"] == MUENCHEN and found[0]["label"] == "München"


async def test_a_postal_code_gives_its_place(logged_in_client) -> None:
    found = await places(logged_in_client, q="10115")
    assert [p["gazetteer_id"] for p in found] == [BERLIN]
    assert "10115" in found[0]["postal_codes"]


async def test_a_place_carries_the_documented_members(logged_in_client) -> None:
    (berlin,) = await places(logged_in_client, q="10115")
    assert set(berlin) == FIELDS
    assert berlin["label"] == "Berlin"
    assert isinstance(berlin["postal_codes"], list) and all(
        isinstance(c, str) for c in berlin["postal_codes"]
    )
    assert isinstance(berlin["lat"], float) and isinstance(berlin["lon"], float)
    assert 52 < berlin["lat"] < 53 and 13 < berlin["lon"] < 14
    assert isinstance(berlin["population"], int) and berlin["population"] > 3_000_000


async def test_the_limit_is_honoured_and_defaults_to_ten(logged_in_client) -> None:
    assert len(await places(logged_in_client, q="ber", limit=3)) == 3
    assert len(await places(logged_in_client, q="ber", limit=1)) == 1
    assert len(await places(logged_in_client, q="", limit=25)) == 25
    assert len(await places(logged_in_client)) == 10  # no q, no limit


async def test_an_empty_query_lists_the_most_populous_places(logged_in_client) -> None:
    for params in ({}, {"q": ""}, {"q": "   "}):
        found = await places(logged_in_client, limit=5, **params)
        assert [p["gazetteer_id"] for p in found][:3] == [BERLIN, HAMBURG, MUENCHEN], params
        populations = [p["population"] for p in found]
        assert populations == sorted(populations, reverse=True), params


async def test_a_query_that_matches_nothing_is_an_empty_list(logged_in_client) -> None:
    assert await places(logged_in_client, q="zzzzzzzz") == []


@pytest.mark.parametrize("limit", ["0", "-1", "26", "1000", "ten", "", "2.5"])
async def test_a_limit_outside_1_to_25_is_refused(logged_in_client, limit: str) -> None:
    r = await logged_in_client.get(URL, params={"q": "ber", "limit": limit})
    assert (r.status_code, r.json()["code"]) == (422, "validation_failed")
    assert [e["field"] for e in r.json()["errors"]] == ["query.limit"]


async def test_the_query_is_at_most_100_characters_without_a_nul(logged_in_client) -> None:
    assert await places(logged_in_client, q="x" * 100) == []
    for q, code in (("x" * 101, "string_too_long"), ("Ber\x00lin", "value_error")):
        r = await logged_in_client.get(URL, params={"q": q})
        assert (r.status_code, r.json()["code"]) == (422, "validation_failed"), repr(q)
        assert [(e["field"], e["code"]) for e in r.json()["errors"]] == [("query.q", code)], repr(q)


async def test_the_route_needs_a_session(api_client) -> None:
    r = await api_client.get(URL, params={"q": "ber"})
    assert (r.status_code, r.json()["code"]) == (401, "unauthenticated")


async def test_the_gazetteer_is_loaded_once_lazily_off_the_event_loop(
    api_app: JobHunterApp, logged_in_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling 5: one load on `app.state`, in a worker thread, however many first requests race."""
    assert getattr(api_app.api.state, "gazetteer", None) is None  # nothing at start-up
    real = Gazetteer.load
    threads: list[int] = []

    def load() -> Gazetteer:
        threads.append(threading.get_ident())
        return real()

    monkeypatch.setattr(Gazetteer, "load", load)
    first = await asyncio.gather(*(places(logged_in_client, q="ber") for _ in range(4)))
    await places(logged_in_client, q="ham")
    assert all(found == first[0] for found in first)
    assert len(threads) == 1 and threads[0] != threading.get_ident()
    assert isinstance(api_app.api.state.gazetteer, Gazetteer)
