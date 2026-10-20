"""Task 24: the one adapter factory (`sources.registry`) and the clients it builds adapters on.

Every adapter is built by `registry.build_adapter` (the worker's, live or fake) or `registry.make_adapter`
(the planner's, without credentials, carry 2). The keys are the configured secrets, `b"fake"` in fake mode,
and the empty key for a secret that is absent (carry 3: the source's instance is disabled by
`sync_instances`, so building its adapter must not fail a traversal), with which the paid adapter sends
nothing (final review C2). Nothing here reaches the network: every client runs on an `httpx.MockTransport`
that records the requests.
"""

import base64
import os
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from jobhunter.config import AppConfig, load_config
from jobhunter.db.seed import SOURCES
from jobhunter.sources import http, registry
from jobhunter.sources.adzuna import AdzunaAdapter
from jobhunter.sources.arbeitnow import ArbeitnowAdapter
from jobhunter.sources.ats import (
    AshbyAdapter,
    GreenhouseAdapter,
    LeverAdapter,
    PersonioAdapter,
    RecruiteeAdapter,
    SmartRecruitersAdapter,
)
from jobhunter.sources.base import MISSING_SECRET, REMOTE_DE, CellPlan, SourceError
from jobhunter.sources.instances import InstanceRow
from jobhunter.sources.jsearch import JSearchAdapter
from jobhunter.sources.serpapi import SerpApiAdapter

FIXED = datetime(2026, 10, 20, 7, 30, tzinfo=UTC)
PROVIDER_SECRETS = {
    "rapidapi_key": b"rk-test-0001",
    "serpapi_key": b"sk-test-0002",
    "adzuna_app_id": b"aid-test-03",
    "adzuna_app_key": b"akey-test-04",
}
# Each source's adapter class, and its instance key: the board of an ATS instance (Lever's is
# `<region>:<site>`).
ADAPTERS: dict[str, tuple[type, str]] = {
    "jsearch": (JSearchAdapter, "default"),
    "serpapi": (SerpApiAdapter, "default"),
    "adzuna": (AdzunaAdapter, "default"),
    "arbeitnow": (ArbeitnowAdapter, "default"),
    "greenhouse": (GreenhouseAdapter, "getyourguide"),
    "lever": (LeverAdapter, "eu:leverdemo"),
    "ashby": (AshbyAdapter, "n8n"),
    "personio": (PersonioAdapter, "everphone"),
    "recruitee": (RecruiteeAdapter, "bunq"),
    "smartrecruiters": (SmartRecruitersAdapter, "DeliveryHero"),
}


def worker_config(tmp_path: Path, secrets: dict[str, bytes], *, fake: bool = False) -> AppConfig:
    directory = tmp_path / f"secrets-{len(list(tmp_path.iterdir()))}"
    directory.mkdir()
    for name, value in {"data_key": base64.b64encode(os.urandom(32)), "db_app": b"pw", **secrets}.items():
        (directory / name).write_bytes(value)
        (directory / name).chmod(0o440)
    env = {"JOBHUNTER_ENV": "test", "JOBHUNTER_DB_NAME": "jobhunter"}
    if fake:
        env["JOBHUNTER_FAKE_SOURCES"] = "1"
    return load_config("worker", env, directory)


def instance(source_id: str, instance_id: int = 11) -> InstanceRow:
    key = ADAPTERS[source_id][1]
    region = key.partition(":")[0] if source_id == "lever" else None
    return InstanceRow(
        id=instance_id,
        source_id=source_id,
        instance_key=key,
        watchlist_company_id=None,
        health="ok",
        half_open_after=None,
        board_slug=key.rpartition(":")[2] if key != "default" else None,
        lever_region=region,
        display_name=None,
    )


def cell(source_id: str, instance_id: int = 11) -> CellPlan:
    """A cell the source's adapter plans page 1 of: a query cell for the search sources, a board otherwise."""
    search = source_id in ("jsearch", "serpapi", "adzuna")
    return CellPlan(
        cell_id=5,
        source_id=source_id,
        instance_id=instance_id,
        query_text="python developer" if search else None,
        query_lang="en" if search else None,
        location_key=REMOTE_DE if search else None,
        location_label=None,
        radius_km=None,
        phase="primary",
        request_class="normal" if search else "free",
        bucket="week" if source_id in ("jsearch", "serpapi") else None,
        window=(FIXED - timedelta(days=7), FIXED),
        max_days_old=7 if source_id == "adzuna" else None,
    )


class Recorder:
    """Clients for every source on one mock transport that answers 200 `{}` and records each request; every
    client it made is closed after the test."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.made: list[httpx.AsyncClient] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={})

    def client(self, transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
        made = httpx.AsyncClient(transport=transport or httpx.MockTransport(self.handle))
        self.made.append(made)
        return made

    def clients(self) -> dict[str, httpx.AsyncClient]:
        return {source.id: self.client() for source in SOURCES}


@pytest.fixture
async def recorder() -> AsyncIterator[Recorder]:
    made = Recorder()
    try:
        yield made
    finally:
        for client in made.made:
            await client.aclose()


@pytest.fixture
def default_http(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The HTTP layer's process-wide configuration back at its defaults (no fixture transport), restored."""
    monkeypatch.setattr(http, "_configuration", http._configuration)
    http._reset_configuration()
    yield


def test_every_source_gets_its_adapter_class(tmp_path, recorder) -> None:
    cfg = worker_config(tmp_path, PROVIDER_SECRETS)
    clients = recorder.clients()
    for source_id, (adapter_class, _) in ADAPTERS.items():
        adapter = registry.build_adapter(instance(source_id), cfg, clients)
        assert type(adapter) is adapter_class, source_id
        assert adapter.capabilities().source_id == source_id
    assert set(ADAPTERS) == {source.id for source in SOURCES}  # the table names every seeded source


@pytest.mark.parametrize("source_id", sorted(ADAPTERS))
async def test_every_adapter_sends_on_its_sources_client_with_the_given_clock(
    tmp_path, recorder, source_id
) -> None:
    cfg = worker_config(tmp_path, PROVIDER_SECRETS)
    # Only the source's own client may be used: the others refuse.
    refusing = httpx.MockTransport(lambda request: pytest.fail(f"sent on another source's client: {request}"))
    clients = {source.id: recorder.client(None if source.id == source_id else refusing) for source in SOURCES}
    adapter = registry.build_adapter(instance(source_id), cfg, clients, clock=lambda: FIXED)
    outcome = await adapter.fetch(adapter.plan(cell(source_id)))
    assert outcome.page is not None and outcome.page.fetched_at == FIXED  # the adapter's clock
    assert len(recorder.requests) == 1


def sent_keys(request: httpx.Request) -> dict[str, str | None]:
    """The credentials a request carries, wherever its source puts them."""
    return {
        "x-rapidapi-key": request.headers.get("x-rapidapi-key"),
        "api_key": request.url.params.get("api_key"),
        "app_id": request.url.params.get("app_id"),
        "app_key": request.url.params.get("app_key"),
    }


async def fetch_keys(adapter_of: Callable[[str], object], recorder: Recorder) -> dict[str, dict[str, str]]:
    keys: dict[str, dict[str, str]] = {}
    for source_id in ("jsearch", "serpapi", "adzuna"):
        adapter = adapter_of(source_id)
        await adapter.fetch(adapter.plan(cell(source_id)))  # type: ignore[attr-defined]
        keys[source_id] = {k: v for k, v in sent_keys(recorder.requests[-1]).items() if v is not None}
    return keys


async def test_the_live_keys_are_the_configured_secrets(tmp_path, recorder) -> None:
    cfg, clients = worker_config(tmp_path, PROVIDER_SECRETS), recorder.clients()
    keys = await fetch_keys(lambda s: registry.build_adapter(instance(s), cfg, clients), recorder)
    assert keys == {
        "jsearch": {"x-rapidapi-key": "rk-test-0001"},
        "serpapi": {"api_key": "sk-test-0002"},
        "adzuna": {"app_id": "aid-test-03", "app_key": "akey-test-04"},
    }


async def test_fake_mode_sends_the_fake_keys_and_needs_no_secret(tmp_path, recorder) -> None:
    with_secrets = replace(worker_config(tmp_path, PROVIDER_SECRETS), fake_sources=True)
    for cfg in (worker_config(tmp_path, {}, fake=True), with_secrets):
        clients = recorder.clients()

        def adapter_of(source_id: str, cfg: AppConfig = cfg, clients=clients) -> object:
            return registry.build_adapter(instance(source_id), cfg, clients)

        keys = await fetch_keys(adapter_of, recorder)
        assert keys == {
            "jsearch": {"x-rapidapi-key": "fake"},
            "serpapi": {"api_key": "fake"},
            "adzuna": {"app_id": "fake", "app_key": "fake"},
        }


async def test_a_missing_secret_still_builds_its_adapter_which_refuses_to_send(tmp_path, recorder) -> None:
    """Carry 3: building the adapter of a source without its secret never raises (its instance is disabled
    by `sync_instances`, so the breaker gate stops every attempt of it). Final review C2, defence in depth:
    should an attempt reach it anyway, the adapter builds no request and sends nothing; the attempt fails as
    `auth` (`missing_secret`) before any byte was sent. SerpApi's account read refuses the same way."""
    cfg, clients = worker_config(tmp_path, {}), recorder.clients()
    for source_id in ("jsearch", "serpapi", "adzuna"):
        adapter = registry.build_adapter(instance(source_id), cfg, clients)
        outcome = await adapter.fetch(adapter.plan(cell(source_id)))
        assert outcome.page is None and outcome.error is not None, source_id
        error = outcome.error
        assert (error.kind, error.code, error.before_send) == ("auth", "missing_secret", True), source_id
        assert error.code == MISSING_SECRET  # the one code, which the startup sync disables with too
    serpapi = registry.build_adapter(instance("serpapi"), cfg, clients)
    assert isinstance(serpapi, SerpApiAdapter)
    with pytest.raises(SourceError) as refused:
        await serpapi.account()
    assert (refused.value.kind, refused.value.code) == ("auth", "missing_secret")
    assert recorder.requests == []  # nothing reached the transport


@pytest.mark.parametrize("empty", ["rapidapi_key", "adzuna_app_id", "adzuna_app_key"])
async def test_one_empty_credential_is_refused_like_a_missing_one(tmp_path, recorder, empty) -> None:
    """An empty secret file counts as missing; Adzuna needs both of its credentials."""
    cfg = worker_config(tmp_path, {**PROVIDER_SECRETS, empty: b""})
    source_id = "jsearch" if empty == "rapidapi_key" else "adzuna"
    adapter = registry.build_adapter(instance(source_id), cfg, recorder.clients())
    outcome = await adapter.fetch(adapter.plan(cell(source_id)))
    assert outcome.error is not None and outcome.error.code == "missing_secret"
    assert recorder.requests == []


async def test_make_adapter_is_the_planners_factory_without_credentials(recorder) -> None:
    clients = recorder.clients()

    def no_secret(name: str) -> bytes:
        return b""

    for source_id, (adapter_class, _) in ADAPTERS.items():
        adapter = registry.make_adapter(
            instance(source_id), clients[source_id], secret=no_secret, clock=lambda: FIXED
        )
        assert type(adapter) is adapter_class
        assert adapter.plan(cell(source_id)).cell_id == 5  # it plans; the planner never sends


async def test_make_clients_makes_one_client_per_source(tmp_path, default_http) -> None:
    clients = registry.make_clients(worker_config(tmp_path, PROVIDER_SECRETS))
    try:
        assert set(clients) == {source.id for source in SOURCES}
        assert len({id(client) for client in clients.values()}) == len(clients)
    finally:
        for client in clients.values():
            await client.aclose()


async def test_make_clients_refuses_fake_mode_before_the_http_layer_serves_the_fixtures(
    tmp_path, default_http
) -> None:
    """Carry 1: a client made before `http.configure` in fake mode would be on the live transport."""
    cfg = worker_config(tmp_path, {}, fake=True)
    with pytest.raises(RuntimeError, match=r"http\.configure"):
        registry.make_clients(cfg)
    http.configure(cfg)
    clients = registry.make_clients(cfg)
    try:
        sent = await clients["arbeitnow"].get(
            "https://www.arbeitnow.com/api/job-board-api", params={"page": 1}
        )
        assert sent.status_code == 200 and sent.json()["data"]  # served by the fixture transport
    finally:
        for client in clients.values():
            await client.aclose()
