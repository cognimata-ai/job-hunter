"""The p95 of the Jobs list over HTTP (spec §14.3, §15.3; plan M4 Task 5, B29, Ruling T4-a).

50,000 cards seeded set-based (`bulk_seed_cards`: 30 % scored, 500 groups, German and English ads) and
analysed, on a server of their own (ANALYZE writes statistics no reset takes back), behind the whole api
(`create_app`, edge and session included) at the lifespan's pool size. Six requests, each once to warm up and
then `REPEATS` times: the first page of the default score sort (with its counts), its third page by cursor,
the first page of the unscored section (`scoring_state=unscored`), the first page of the `posted` sort, a
keyword page (`q=entwickler`, the page Ruling T4-a watches) and a Berlin 30 km page. Each request's p95 (the
nearest rank) is recorded (`record_property`) and printed, and must be below `P95_LIMIT_S`: the budget is
never relaxed here.
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

from tests.support.fixtures import TEST_PASSWORD, TEST_USERNAME, ConfigFactory
from tests.support.jobs_list import BERLIN, BulkSeed, bulk_seed_cards
from tests.support.pg import PgServer, config_for, remove_pg_server, start_pg_server

pytestmark = [pytest.mark.perf, pytest.mark.integration]

CARDS: Final = 50_000
REPEATS: Final = 20
P95_LIMIT_S: Final = 0.300  # B29
URL: Final = "/api/jobs"
BERLIN_30_KM: Final = {
    "loc_id": "geonames:2950159",
    "loc_lat": BERLIN[0],
    "loc_lon": BERLIN[1],
    "radius_km": 30,
}


@pytest.fixture(scope="module")
def seeded(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[PgServer, BulkSeed]]:
    server = start_pg_server(tmp_path_factory.mktemp("jobs-list-perf"))
    try:
        run_migrate(config_for(server, "migrate"))
        yield server, asyncio.run(_seed(server))
    finally:
        remove_pg_server(server)


async def _seed(server: PgServer) -> BulkSeed:
    engine = make_null_engine(server.dsn("migrator", driver="sqlalchemy"))
    try:
        seed = await bulk_seed_cards(engine, CARDS, scored_share=0.3, groups=500, langs=("de", "en"))
        async with engine.connect() as c:
            await c.execute(sa.text("ANALYZE"))  # as the owner
            await c.commit()
    finally:
        await engine.dispose()
    return seed


def p95(samples: list[float]) -> float:
    """The nearest-rank 95th percentile."""
    ordered = sorted(samples)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


async def _timed(client: httpx.AsyncClient, params: dict[str, Any]) -> list[float]:
    """The request once to warm up, then `REPEATS` timed runs; every answer a non-empty 200 page."""
    samples: list[float] = []
    for run in range(REPEATS + 1):
        started = time.perf_counter()
        r = await client.get(URL, params=params)
        elapsed = time.perf_counter() - started
        assert r.status_code == 200, (params, r.status_code, r.text[:500])
        assert r.json()["items"], params
        if run:
            samples.append(elapsed)
    return samples


async def test_the_list_p95_is_under_300_ms(
    seeded: tuple[PgServer, BulkSeed], make_config: ConfigFactory, record_property: Any
) -> None:
    server, seed = seeded
    assert len(seed.card_ids) == CARDS
    cfg = make_config("api", server)
    engine = make_engine(cfg, pool_size=ENGINE_POOL_SIZE)
    try:
        async with engine.begin() as c:
            await users.create_user(c, TEST_USERNAME, TEST_PASSWORD, now=utcnow())
        transport = httpx.ASGITransport(
            app=create_app(cfg=cfg, engine=engine), client=("198.51.100.30", 50000)
        )
        headers = {"Origin": "https://testserver", "X-JH-Request": "1"}
        async with httpx.AsyncClient(
            transport=transport, base_url="https://testserver", headers=headers
        ) as client:
            login = await client.post(
                "/api/auth/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD}
            )
            assert login.status_code == 204, login.text
            one = (await client.get(URL)).json()
            two = (await client.get(URL, params={"cursor": one["next_cursor"]})).json()
            requests: dict[str, dict[str, Any]] = {
                "score_page_1": {},
                "score_page_3": {"cursor": two["next_cursor"]},
                "unscored_page": {"scoring_state": "unscored"},
                "posted_page": {"sort": "posted"},
                "keyword_entwickler": {"q": "entwickler"},
                "berlin_30_km": BERLIN_30_KM,
            }
            measured = {name: p95(await _timed(client, params)) for name, params in requests.items()}
    finally:
        await engine.dispose()
    for name, seconds in measured.items():
        record_property(f"{name}_p95_ms", round(seconds * 1000, 1))
        print(f"{name}: p95 {seconds * 1000:.1f} ms")
    assert all(seconds < P95_LIMIT_S for seconds in measured.values()), measured
