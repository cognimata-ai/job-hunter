"""The weekly live smoke of the free sources (spec §18.8 `smoke-free-sources.yml`; marker `live`, deselected
by default, never run by the CI workflow).

One request each, page 1 of Arbeitnow and of the six public ATS boards the contract fixtures were recorded
from (`tests/contract/fixtures/ats/*.meta.json`), through the source's real adapter
(`registry.build_adapter`) on its real client (`http.make_client`, the live transport). Each must answer 200
with a valid envelope (`check_envelope`), at least one item, and one parse outcome per item.

**Never a paid host.** Every client of this module sends only to `FREE_HOSTS` and the Personio host family
(a Personio board may redirect within it); a request to any other host, a paid provider's first (JSearch,
SerpApi, Adzuna: `PAID_HOSTS`), raises `HostRefused` from the client's request hook before anything is
sent, so the test errors out. The guard's own tests are not `live`: they run offline in the default suite.
"""

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Final

import httpx
import pytest
from jobhunter.clock import utcnow
from jobhunter.config import AppConfig, load_config
from jobhunter.sources import http, registry
from jobhunter.sources.adzuna import SEARCH_URL as ADZUNA_URL
from jobhunter.sources.base import CellPlan
from jobhunter.sources.instances import InstanceRow
from jobhunter.sources.jsearch import SEARCH_URL as JSEARCH_URL
from jobhunter.sources.serpapi import ACCOUNT_URL as SERPAPI_ACCOUNT_URL
from jobhunter.sources.serpapi import SEARCH_URL as SERPAPI_URL

FREE_HOSTS: Final = frozenset(
    {
        "www.arbeitnow.com",
        "boards-api.greenhouse.io",
        "api.lever.co",
        "api.ashbyhq.com",
        "everphone.jobs.personio.de",
        "bunq.recruitee.com",
        "api.smartrecruiters.com",
    }
)
PAID_URLS: Final = (JSEARCH_URL, SERPAPI_URL, SERPAPI_ACCOUNT_URL, ADZUNA_URL)
PAID_HOSTS: Final = frozenset(httpx.URL(url).host for url in PAID_URLS)
# The free sources and the boards of the recordings: `(source_id, instance_key)`.
FREE_SOURCES: Final = (
    ("arbeitnow", "default"),
    ("greenhouse", "getyourguide"),
    ("lever", "global:leverdemo"),
    ("ashby", "n8n"),
    ("personio", "everphone"),
    ("recruitee", "bunq"),
    ("smartrecruiters", "DeliveryHero"),
)


class HostRefused(Exception):
    """A request of the smoke to a host outside its allowlist; never an `httpx` error, so that `http.send`
    cannot turn it into an outcome: it propagates and errors the test out."""


def allowed(host: str) -> bool:
    return host in FREE_HOSTS or host.endswith(http.PERSONIO_HOST_SUFFIX)


async def _refuse_unlisted(request: httpx.Request) -> None:
    if not allowed(request.url.host):
        raise HostRefused(request.url.host)


def guarded(client: httpx.AsyncClient) -> httpx.AsyncClient:
    """`client`, refusing every request to a host outside the allowlist before it is sent."""
    client.event_hooks = {"request": [_refuse_unlisted], "response": []}
    return client


def test_the_allowlist_holds_the_free_hosts_only() -> None:
    assert PAID_HOSTS == {"jsearch.p.rapidapi.com", "serpapi.com", "api.adzuna.com"}
    assert not [host for host in PAID_HOSTS if allowed(host)]
    assert {source for source, _ in FREE_SOURCES}.isdisjoint({"jsearch", "serpapi", "adzuna"})


async def test_a_request_to_a_paid_host_errors_out_before_it_is_sent() -> None:
    sent: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200)

    async with guarded(httpx.AsyncClient(transport=httpx.MockTransport(answer))) as client:
        for url in PAID_URLS:
            with pytest.raises(HostRefused):
                await client.get(url)
        assert (await client.get("https://www.arbeitnow.com/api/job-board-api")).status_code == 200
    assert [request.url.host for request in sent] == ["www.arbeitnow.com"]  # nothing reached a paid host


@pytest.fixture
def live_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[AppConfig]:
    """A worker configuration out of fake mode and without any secret, with the HTTP layer configured for it
    (the live transport); the process-wide configuration it found is restored after the test."""
    env = {"JOBHUNTER_ENV": "test", "JOBHUNTER_DB_NAME": "jobhunter"}
    cfg = load_config("worker", env, tmp_path / "no-secrets")
    monkeypatch.setattr(http, "_configuration", http._configuration)  # restored on teardown
    http.configure(cfg)
    yield cfg


def _instance(source_id: str, instance_key: str) -> InstanceRow:
    board = None if instance_key == "default" else instance_key.rpartition(":")[2]
    region = instance_key.partition(":")[0] if source_id == "lever" else None
    return InstanceRow(1, source_id, instance_key, None, "ok", None, board, region, None)


@pytest.mark.live
@pytest.mark.parametrize(("source_id", "instance_key"), FREE_SOURCES, ids=[s for s, _ in FREE_SOURCES])
async def test_page_1_of_a_free_source_has_its_envelope_and_items(
    live_config: AppConfig, source_id: str, instance_key: str
) -> None:
    now = utcnow()
    cell = CellPlan(
        cell_id=1,
        source_id=source_id,
        instance_id=1,
        query_text=None,
        query_lang=None,
        location_key=None,
        location_label=None,
        radius_km=None,
        phase="primary",
        request_class="free",
        bucket=None,
        window=(now - timedelta(days=30), now),
        max_days_old=None,
    )
    async with guarded(http.make_client(source_id)) as client:
        adapter = registry.build_adapter(_instance(source_id, instance_key), live_config, {source_id: client})
        outcome = await adapter.fetch(adapter.plan(cell))
    assert outcome.error is None and outcome.page is not None, outcome.error
    page = outcome.page
    assert page.status == 200
    adapter.check_envelope(page)  # raises `ContractViolation` for a broken envelope
    elements = adapter.elements(page)
    assert len(elements) >= 1
    assert len(adapter.parse(page)) == len(elements)
