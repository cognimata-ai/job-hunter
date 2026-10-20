"""The one adapter factory and the clients it builds on (Task 24; spec §5.1, §5.2, §19.2).

`make_adapter` is the one place where a source instance becomes its adapter: by `source_id`,
`JSearchAdapter` with the `rapidapi_key`, `SerpApiAdapter` with the `serpapi_key`, `AdzunaAdapter` with the
`adzuna_app_id` and `adzuna_app_key`, `ArbeitnowAdapter`, and the ATS boards through `ats.adapter_for`; each
on the client and with the clock it is given, its keys read through `secret`. Its two callers:

- `build_adapter`, the worker's (live or fake): the instance's adapter on its source's client of
  `make_clients`, with the provider secrets of `cfg`, or the keys `b"fake"` in fake mode
  (`JOBHUNTER_FAKE_SOURCES`, whose fixture transport serves every request). A secret that is absent gives the
  empty key and never an error: `sync_instances` disabled that source's `default` instance when the worker
  started (plan Deviation 6, `MISSING_SECRET`), so the planner never plans it and the breaker gate skips
  every new attempt of it (a resumed run's receipts included). Building the adapter therefore cannot fail a
  traversal (carry 3), and the adapter of a stored response still parses it. Nothing is ever sent with that
  key even if an attempt reaches the adapter anyway (an instance restored to `ok` from a stale view of the
  secrets, final review C2): the paid adapter refuses locally, builds no request and fails the attempt as
  `auth` before the send (`http.missing_credential`).
- the planner's spec adapters (`quota.planner`), which plan and never send: on a client that refuses every
  request, without credentials.

`make_clients` makes one client per source (`http.make_client`), which the worker's teardown closes. In
fake mode it refuses to run before `http.configure(cfg)` has put the fixture transport in place: a client
made earlier would be on the live transport (carry 1).
"""

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Final

import httpx

from jobhunter.clock import utcnow
from jobhunter.config import AppConfig
from jobhunter.db.seed import SOURCES
from jobhunter.sources import ats, http
from jobhunter.sources.adzuna import AdzunaAdapter
from jobhunter.sources.arbeitnow import ArbeitnowAdapter
from jobhunter.sources.base import SourceAdapter
from jobhunter.sources.instances import InstanceRow
from jobhunter.sources.jsearch import JSearchAdapter
from jobhunter.sources.serpapi import SerpApiAdapter

# The keys of every provider in fake mode: the fixture transport answers whatever key a request carries.
FAKE_KEY: Final = b"fake"

Secret = Callable[[str], bytes]


def make_adapter(
    instance: InstanceRow,
    client: httpx.AsyncClient,
    *,
    secret: Secret,
    clock: Callable[[], datetime],
) -> SourceAdapter:
    """The adapter of `instance` on `client` with `clock`, its provider keys read by `secret(name)`.

    Raises `ValueError` (from `ats.adapter_for`) for an instance of no known source.
    """
    match instance.source_id:
        case "jsearch":
            return JSearchAdapter(
                client, api_key=secret("rapidapi_key"), instance_id=instance.id, clock=clock
            )
        case "serpapi":
            return SerpApiAdapter(client, api_key=secret("serpapi_key"), instance_id=instance.id, clock=clock)
        case "adzuna":
            return AdzunaAdapter(
                client,
                app_id=secret("adzuna_app_id"),
                app_key=secret("adzuna_app_key"),
                instance_id=instance.id,
                clock=clock,
            )
        case "arbeitnow":
            return ArbeitnowAdapter(client, instance_id=instance.id, clock=clock)
    return ats.adapter_for(instance, client, clock=clock)


def build_adapter(
    instance: InstanceRow,
    cfg: AppConfig,
    clients: Mapping[str, httpx.AsyncClient],
    *,
    clock: Callable[[], datetime] = utcnow,
) -> SourceAdapter:
    """The worker's adapter of `instance` on its source's client in `clients` (see the module docstring)."""

    def secret(name: str) -> bytes:
        if cfg.fake_sources:
            return FAKE_KEY
        return cfg.secrets.get(name, b"")  # absent: the instance is disabled, and the adapter sends nothing

    return make_adapter(instance, clients[instance.source_id], secret=secret, clock=clock)


def make_clients(cfg: AppConfig) -> dict[str, httpx.AsyncClient]:
    """One client per source (`http.make_client`), by source id; the caller closes them.

    Raises `RuntimeError` in fake mode when `http.configure(cfg)` has not put the fixture transport in place.
    """
    if cfg.fake_sources and not http.serves_fixtures():
        raise RuntimeError("fake mode: http.configure(cfg) must run before the source clients are made")
    return {source.id: http.make_client(source.id) for source in SOURCES}
