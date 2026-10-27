"""Collection test helpers (Task 8; later tasks add theirs): a running run, the default source instances,
planned fetch requests and a reconcile context.

`queued_run` inserts a manual run over `active_config` with its own statement (it binds `include_paid`, which
`pg.MANUAL_RUN_SQL` fixes to `false`) and admits it as attempt 1 of worker epoch 1 (`runs.start_attempt`): the
test needs the `current_epoch` fixture. Instance ids change with every database reset (Task 1), so they are
always looked up by `(source_id, instance_key)`.

Task 12: `acquire_ctx` is the acquisition context of a test (the seeded run settings, `utcnow` and a sleep
that does not wait unless given); `source_cell` the coverage cell of a source's default instance for the test
query (`TEST_QUERY`) and a location key, created when absent, and `jsearch_cell` its JSearch `remote-de`
cell; `page1_spec` the JSearch page-1 spec of a cell (bucket `week`), its instance read from the cell row;
`supersede` gives a run its next attempt, as a takeover does.

Task 13: `DEFAULT_CELL` is every `cells.CellRow` field of a never-attempted JSearch `remote-de` cell, as a
dict; `seed_inputs` writes the queries and the watchlist companies (with their board instances) of a test and
a new settings version that enables them (and, with `enabled`, exactly those sources), pointed at by
`active_config`, so a run queued afterwards plans them (plan Deviation 3).

Task 14: `make_deps` is the `CollectDeps` of a test (the real `RateLimiter` over `SOURCE_LIMITS`, a fresh
`FetchContext`, `utcnow`, no embedder unless given); the fixture `job_factory` inserts a job with one
posting and its observations directly, with the facts given (`MadeJob`); `seed_inputs_sync` is
`seed_inputs` for the sync crash tests, on an engine of its own; `manual_paid_run` seeds the test query with
only the source under test enabled (Ruling P5) and requests a manual run that includes paid sources, whose
`run_collect(id, 1)` job waits for the worker.

Task 16: `posting_factory` inserts a posting with its job and one observation (the availability and the last
sighting as given; a missing source instance is created); `board` and `board20` are the Greenhouse watchlist
board `BOARD_SLUG` (enabled through `seed_inputs`; its fixture route answers `greenhouse_404`) with 3 or 20
`active` postings and their jobs, its instance's `last_snapshot_active_count` their number (`Board`).

Task 17: the fixture `job_with_postings` inserts a job with one posting and one observation per source,
straight into the tables (the resolution path arrives in Task 18); each observation's `normalized` is
`observations.normalized` with that source's overrides. The job row is a placeholder, not a canonical
selection: its title, `title_norm` and `seniority_tokens` come from `normalize_title` of the first posting's
title, its other fields say nothing, and its `det_facts_hash` is no hash of its facts, so the first
`recompute_job` changes the det facts. `add_posting` adds such a posting to a job and `add_observation` a
newer observation to a posting (it becomes the posting's latest), both on the caller's connection.

Task 19: `activate_collection_settings` activates a copy of the active settings with `collection` values
changed (for example `stale_ttl_days`).

Task 18: `pending_item` stores a one-element page for a run (a `free` list request of the source's default
instance, `succeeded`, its response fetched at `fetched_at`) and parses it with `items.parse_response` and an
adapter whose one item is `observations.normalized` with the overrides: the `pending` raw item a resolver
takes. Unless the overrides give `raw_keys`, the item's keys are the ones its source's adapter reads (§8.4):
the source's own id key (`htidocid` for JSearch and SerpApi, `adzuna` for Adzuna) and the keys of its
primary link (`keys_from_link`), which is stored canonical. `key_pair` resolves job A (a JSearch posting
with `htidocid` `H` and `pub:stepstone.de` 1) and job B (an Adzuna posting with id 9) for a run, A first.
`job_lockable` probes from a connection of its own whether a job row can be locked at once (`FOR UPDATE
NOWAIT`): `False` while another transaction holds it.

Task 22: `make_deps` and `ctx` take an `embedder` client (none by default); the fixture `revisions_awaiting`
inserts jobs whose one revision is `awaiting_embedding`, with texts of their own, duplicated or long on
request (`insert_revisions_awaiting`).

Task 23: the fixture `collect_deps` is `make_deps(engine, adapters)` with an `EmbedderClient` on the fake
embedder; `manual_run_id` inserts a queued manual run (not admitted: `execute_run` starts it);
`run_to_the_end` seeds the inputs of a run (by default a watchlist company, the Greenhouse board
`getyourguide`, and Arbeitnow), inserts the run and executes attempt 1 through `execute_run` on the real
collection graph (or `graph`, built over `saver`, an `InMemorySaver` unless given), and returns the run id;
`conflicting_graph` is the real graph whose `fetch_cell` adds an outcome of its own for one shared work id
(`CONFLICT_WORK_ID`) to every update, so two Sends of one step give the reducer two differing outcomes.

M2 Task 18: `job_factory.many(n)` (`insert_jobs`) inserts `n` unmerged jobs in one statement, each with a
`det_facts_hash` of its own and no posting, and writes the active scope's hard-filter results for them, so the
scope apply has retained jobs to precompute and the active scope's view is complete.

M3 Task 14: `queued_run` and `manual_run_id` record the active tuple on the run, as the run creation does
(node 9 and I5 read the run's tuple).
"""

import asyncio
import hashlib
import json
import os
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any, Final, Protocol, cast
from unittest import mock

import httpx
import procrastinate
import pytest
from jobhunter.clock import utcnow
from jobhunter.db.session import make_null_engine
from jobhunter.db.sql import LOCK_NOT_AVAILABLE, pg_error, q
from jobhunter.db.transitions import transition
from jobhunter.dedup.identity import resolve_item
from jobhunter.embeddings import EmbedderClient, make_http_client
from jobhunter.graphs.collect import graph as collect_graph
from jobhunter.graphs.collect.deps import CollectDeps, FetchContext
from jobhunter.graphs.collect.graph import CollectGraph, build_collect_graph
from jobhunter.graphs.collect.nodes.fetch import make_fetch_cell
from jobhunter.graphs.collect.state import CollectUpdate, FetchCellInput, FetchOutcome
from jobhunter.normalize.companies import company_key
from jobhunter.normalize.fields import ATS_SOURCES
from jobhunter.normalize.keys import AdKey, keys_from_link
from jobhunter.normalize.titles import normalize_title
from jobhunter.normalize.urls import canonical_url
from jobhunter.quota.windows import Provider
from jobhunter.run_settings import scope_from_payload
from jobhunter.scoring.filters import BATCH, recompute_stale
from jobhunter.sources import items, receipts
from jobhunter.sources.acquire import AcquireContext
from jobhunter.sources.base import (
    REMOTE_DE,
    Billing,
    CellPlan,
    ItemOutcome,
    NormalizedPosting,
    Parsed,
    RawPage,
    RequestSpec,
    SourceAdapter,
)
from jobhunter.sources.instances import InstanceRow, instance_for_company
from jobhunter.sources.jsearch import JSearchAdapter
from jobhunter.sources.ratelimit import SOURCE_LIMITS, RateLimiter
from jobhunter.types import JSONValue, get_list, get_object
from jobhunter.worker import runs
from jobhunter.worker.reconcilers import ReconcileContext
from jobhunter.worker.runs import RunOwner
from jobhunter.worker.tasks import defer_in_tx, make_queue_app
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.embedder import FakeEmbedder
from tests.support.observations import normalized
from tests.support.pg import PgServer, config_for
from tests.support.quota import seeded_run_settings

# The run records the active tuple, as the run creation does (`runs.request_manual_run`, `tick`): node 9 and
# I5 read it (M3 Task 14).
_QUEUED_RUN: Final = q(
    "INSERT INTO runs (trigger, scheduled_slot, status, include_paid, settings_version_id, scope_version_id, "
    "llm_tuple_id) SELECT 'manual', NULL, 'queued', :include_paid, settings_version_id, scope_version_id, "
    "llm_tuple_id FROM active_config RETURNING id"
)
_INSTANCE: Final = q("SELECT id FROM source_instances WHERE source_id = :s AND instance_key = :k")
_CELL: Final = q(
    "INSERT INTO coverage_cells (source_instance_id, query_id, location_key) VALUES (:i, NULL, :loc) "
    "ON CONFLICT DO NOTHING"
)
_CELL_ID: Final = q(
    "SELECT id FROM coverage_cells WHERE source_instance_id = :i AND query_id IS NULL AND location_key = :loc"
)
_PLANNED: Final = q(
    "INSERT INTO fetch_requests (run_id, run_attempt_no, cell_id, source_instance_id, phase, operation, "
    "request_key, attempt_seq, page_no, request_class, spec, status) VALUES (:run, :attempt, :cell, "
    ":instance, :phase, 'list', :key, :seq, 1, 'normal', :spec, 'planned') RETURNING id",
    jsonb=["spec"],
)
CELL_LOCATION: Final = "berlin"
TEST_QUERY: Final = "python developer"
_QUERY: Final = q("INSERT INTO queries (text, lang, origin) VALUES (:t, 'en', 'user') ON CONFLICT DO NOTHING")
_QUERY_ID: Final = q("SELECT id FROM queries WHERE lower(text) = lower(:t) AND retired_at IS NULL")
_SOURCE_CELL: Final = q(
    "INSERT INTO coverage_cells (source_instance_id, query_id, location_key) VALUES (:i, :query, :loc) "
    "ON CONFLICT DO NOTHING"
)
_SOURCE_CELL_ID: Final = q(
    "SELECT id FROM coverage_cells WHERE source_instance_id = :i AND query_id IS NOT DISTINCT FROM :query "
    "AND location_key IS NOT DISTINCT FROM :loc"
)
_CELL_ROW: Final = q(
    "SELECT c.source_instance_id, c.location_key, qr.text FROM coverage_cells c "
    "LEFT JOIN queries qr ON qr.id = c.query_id WHERE c.id = :id"
)


async def queued_run(engine: AsyncEngine, *, include_paid: bool = True) -> RunOwner:
    """A manual run over the active configuration, admitted as attempt 1 by worker epoch 1."""
    async with engine.begin() as conn:
        run_id = int((await conn.execute(_QUEUED_RUN, {"include_paid": include_paid})).scalar_one())
        admission, owner = await runs.start_attempt(conn, run_id, 1, epoch=1)
    if admission != "started" or owner is None:
        raise RuntimeError(f"queued_run: run {run_id} was not admitted ({admission})")
    return owner


async def instance_id(conn: AsyncConnection, source_id: str, instance_key: str = "default") -> int:
    """The id of the source instance `(source_id, instance_key)`."""
    return int((await conn.execute(_INSTANCE, {"s": source_id, "k": instance_key})).scalar_one())


async def planned_request(
    engine: AsyncEngine,
    owner: RunOwner,
    *,
    provider: str,
    key: bytes,
    attempt_seq: int,
    cell_id: int | None = None,
) -> int:
    """A `planned` list request of `owner` for `key` / `attempt_seq` on the provider's default instance.

    Without `cell_id` it uses the instance's `berlin` coverage cell, created when absent. SerpApi requests are
    `fallback` ones, every other provider's `primary`.
    """
    async with engine.begin() as conn:
        instance = await instance_id(conn, provider)
        if cell_id is None:
            await conn.execute(_CELL, {"i": instance, "loc": CELL_LOCATION})
            cell_id = int((await conn.execute(_CELL_ID, {"i": instance, "loc": CELL_LOCATION})).scalar_one())
        spec = RequestSpec(
            source_instance_id=instance,
            operation="list",
            cell_id=cell_id,
            page_no=1,
            params={"query": "python"},
            cursor_in=None,
            detail_native_id=None,
            parent_fetch_request_id=None,
            window=None,
            request_class="normal",
        )
        row = await conn.execute(
            _PLANNED,
            {
                "run": owner.run_id,
                "attempt": owner.attempt_no,
                "cell": cell_id,
                "instance": instance,
                "phase": "fallback" if provider == "serpapi" else "primary",
                "key": key,
                "seq": attempt_seq,
                "spec": spec.to_json(),
            },
        )
        return int(row.scalar_one())


async def request_sent(conn: AsyncConnection, fetch_request_id: int, entry_id: int) -> None:
    """Move a planned request `reserved` (linked to its ledger entry) and then `dispatched`, as the
    acquisition protocol does around `ledger.reserve` and `ledger.mark_dispatched`."""
    await transition(
        conn,
        "fetch_requests",
        fetch_request_id,
        ["planned"],
        "reserved",
        set_={"quota_ledger_id": entry_id},
    )
    await transition(conn, "fetch_requests", fetch_request_id, ["reserved"], "dispatched")


async def _no_wait(seconds: float) -> None:
    return None


def acquire_ctx(
    engine: AsyncEngine,
    owner: RunOwner,
    adapter: SourceAdapter,
    *,
    billing: Billing,
    provider: Provider | None,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    stopped: Callable[[], bool] = lambda: False,
) -> AcquireContext:
    """The acquisition context of `owner` for `adapter`: the seeded run settings (6 h interval), `utcnow`
    unless `clock` is given, and a sleep that returns at once unless `sleep` is given."""
    return AcquireContext(
        engine=engine,
        owner=owner,
        adapter=adapter,
        billing=billing,
        provider=provider,
        settings=seeded_run_settings(),
        clock=utcnow if clock is None else clock,
        sleep=_no_wait if sleep is None else sleep,
        admission_stopped=stopped,
    )


async def source_cell(
    engine: AsyncEngine,
    source_id: str,
    *,
    query: str | None = TEST_QUERY,
    location_key: str | None = REMOTE_DE,
) -> int:
    """The coverage cell of the source's default instance for `query` (a `queries` row, created when absent;
    `None` for a cell without a query, as Arbeitnow's) and `location_key`, created when absent."""
    async with engine.begin() as conn:
        instance = await instance_id(conn, source_id)
        query_id = None
        if query is not None:
            await conn.execute(_QUERY, {"t": query})
            query_id = int((await conn.execute(_QUERY_ID, {"t": query})).scalar_one())
        cell = {"i": instance, "query": query_id, "loc": location_key}
        await conn.execute(_SOURCE_CELL, cell)
        return int((await conn.execute(_SOURCE_CELL_ID, cell)).scalar_one())


async def jsearch_cell(engine: AsyncEngine) -> int:
    """The JSearch default instance's `remote-de` cell for `TEST_QUERY`."""
    return await source_cell(engine, "jsearch")


async def page1_spec(engine: AsyncEngine, cell_id: int) -> RequestSpec:
    """The JSearch page-1 spec of the cell (bucket `week`), its instance read from the cell row."""
    async with engine.connect() as conn:
        row = (await conn.execute(_CELL_ROW, {"id": cell_id})).one()
    now = utcnow()
    plan = CellPlan(
        cell_id=cell_id,
        source_id="jsearch",
        instance_id=int(row.source_instance_id),
        query_text=row.text,
        query_lang="en",
        location_key=row.location_key,
        location_label=None,
        radius_km=None,
        phase="primary",
        request_class="normal",
        bucket="week",
        window=(now - timedelta(days=7), now),
        max_days_old=None,
    )
    async with httpx.AsyncClient() as client:  # `plan` sends nothing
        adapter = JSearchAdapter(client, api_key=b"fake", instance_id=plan.instance_id, clock=utcnow)
        return adapter.plan(plan)


async def supersede(engine: AsyncEngine, owner: RunOwner) -> RunOwner:
    """The run's next attempt, by worker epoch 1 (a takeover): `owner` loses the run. Needs `queue_app`."""
    async with engine.begin() as conn:
        attempt = await runs.begin_new_attempt(
            conn, owner.run_id, epoch=1, end_reason="worker_takeover", defer=defer_in_tx
        )
    if attempt is None:
        raise RuntimeError(f"supersede: run {owner.run_id} got no new attempt")
    return RunOwner(owner.run_id, attempt)


def ctx(
    engine: AsyncEngine,
    queue_app: procrastinate.App,
    now: datetime | None = None,
    epoch: int = 1,
    *,
    embedder: EmbedderClient | None = None,
) -> ReconcileContext:
    """A reconcile context at `now` (the current time when omitted) for worker `epoch`, with `embedder`."""
    return ReconcileContext(
        engine=engine,
        queue=queue_app,
        defer=defer_in_tx,
        now=utcnow() if now is None else now,
        epoch=epoch,
        embedder=embedder,
    )


# Every `cells.CellRow` field of a never-attempted JSearch `remote-de` cell, with the column defaults of
# `coverage_cells` (`yield_ema` 0.5, no streaks).
DEFAULT_CELL: Final[Mapping[str, object]] = MappingProxyType(
    {
        "id": 1,
        "source_instance_id": 1,
        "query_id": 1,
        "location_key": REMOTE_DE,
        "enabled": True,
        "last_attempted_at": None,
        "last_covered_at": None,
        "last_success_at": None,
        "continuation": None,
        "yield_ema": 0.5,
        "zero_yield_streak": 0,
        "consecutive_incomplete": 0,
        "last_exploration_at": None,
        "source_id": "jsearch",
        "instance_key": "default",
        "query_text": TEST_QUERY,
        "query_lang": "en",
        "exploration": False,
        "location_label": None,
        "radius_km": None,
    }
)

# The source switches of `sources.enabled` (§13.1).
SOURCE_SWITCHES: Final = ("jsearch", "serpapi", "adzuna", "arbeitnow", "ats")
_COMPANY: Final = q(
    "INSERT INTO watchlist_companies (display_name, company_key, ats_type, board_slug, lever_region, "
    "detection, validation_status, validated_at) VALUES (:slug, lower(:slug), :ats, :slug, :region, "
    "'manual', 'ok', now()) ON CONFLICT DO NOTHING"
)
_COMPANY_ID: Final = q(
    "SELECT id FROM watchlist_companies WHERE ats_type = :ats AND board_slug = :slug "
    "AND lever_region IS NOT DISTINCT FROM :region"
)
_ACTIVE_PAYLOAD: Final = q(
    "SELECT v.payload FROM active_config a JOIN settings_versions v ON v.id = a.settings_version_id "
    "WHERE a.id = 1 FOR UPDATE OF a"
)
_NEXT_VERSION: Final = q(
    "INSERT INTO settings_versions (version_no, schema_version, payload, scope_version_id, model_policy_id, "
    "execution_config_id, evaluation_policy_id, effect_classes) "
    "SELECT (SELECT max(version_no) + 1 FROM settings_versions), v.schema_version, :payload, "
    "v.scope_version_id, v.model_policy_id, v.execution_config_id, v.evaluation_policy_id, '{collection}' "
    "FROM active_config a JOIN settings_versions v ON v.id = a.settings_version_id WHERE a.id = 1 "
    "RETURNING id",
    jsonb=["payload"],
)
_ACTIVATE: Final = q("UPDATE active_config SET settings_version_id = :v WHERE id = 1")


def _enabling(entries: JSONValue, key: str, ids: Sequence[int]) -> list[JSONValue]:
    """The settings entries `[{key: id, "enabled": …}]` with every id of `ids` enabled (kept in place)."""
    merged: dict[int, dict[str, JSONValue]] = {}
    for entry in get_list(entries, ""):
        entry_id = get_object(entry, "")[key]
        if not isinstance(entry_id, int):
            raise TypeError(f"{key} must be an integer, got {entry_id!r}")
        merged[entry_id] = dict(get_object(entry, ""))
    for entry_id in ids:
        merged[entry_id] = {key: entry_id, "enabled": True}
    return list(merged.values())


async def seed_inputs(
    engine: AsyncEngine,
    *,
    queries: Sequence[str] = (),
    companies: Sequence[tuple[str, str]] = (),
    enabled: Sequence[str] | None = None,
    lever_region: str = "global",
) -> None:
    """The test's collection inputs (plan Deviation 3), committed:

    - a `queries` row per text (`lang='en'`, `origin='user'`);
    - a watchlist company per `(ats_type, slug)` (`detection='manual'`, `validation_status='ok'`; a Lever
      board in `lever_region`, `global` by default) and its board instance (`instances.instance_for_company`);
    - a new settings version copied from the active one whose `queries[]` and `watchlist[]` entries enable
      them and, when `enabled` is given, whose `sources.enabled` is true for exactly those switches
      (`SOURCE_SWITCHES`); `active_config` points at it, so a run queued afterwards uses it.
    """
    if enabled is not None and (unknown := set(enabled) - set(SOURCE_SWITCHES)):
        raise ValueError(f"unknown source switches: {sorted(unknown)}")
    async with engine.begin() as conn:
        # `active_config` first, as the lock order has it (§3.3.3)
        payload = dict(get_object((await conn.execute(_ACTIVE_PAYLOAD)).scalar_one(), ""))
        query_ids: list[int] = []
        for text in queries:
            await conn.execute(_QUERY, {"t": text})
            query_ids.append(int((await conn.execute(_QUERY_ID, {"t": text})).scalar_one()))
        company_ids: list[int] = []
        for ats, slug in companies:
            board = {"ats": ats, "slug": slug, "region": lever_region if ats == "lever" else None}
            await conn.execute(_COMPANY, board)
            company_id = int((await conn.execute(_COMPANY_ID, board)).scalar_one())
            await instance_for_company(conn, company_id)
            company_ids.append(company_id)
        payload["queries"] = _enabling(payload["queries"], "query_id", query_ids)
        payload["watchlist"] = _enabling(payload["watchlist"], "watchlist_company_id", company_ids)
        if enabled is not None:
            switches: dict[str, JSONValue] = {switch: switch in enabled for switch in SOURCE_SWITCHES}
            payload["sources"] = {**get_object(payload, "sources"), "enabled": switches}
        version = int((await conn.execute(_NEXT_VERSION, {"payload": payload})).scalar_one())
        await conn.execute(_ACTIVATE, {"v": version})


async def activate_collection_settings(engine: AsyncEngine, **values: JSONValue) -> None:
    """A new settings version copied from the active one with `collection.<key> = value` for each of `values`
    (for example `stale_ttl_days=10`), committed and pointed at by `active_config` (Task 19)."""
    async with engine.begin() as conn:
        payload = dict(get_object((await conn.execute(_ACTIVE_PAYLOAD)).scalar_one(), ""))
        payload["collection"] = {**get_object(payload, "collection"), **values}
        version = int((await conn.execute(_NEXT_VERSION, {"payload": payload})).scalar_one())
        await conn.execute(_ACTIVATE, {"v": version})


# ---- Task 14 ----------------------------------------------------------------------------------------------


def make_deps(
    engine: AsyncEngine,
    adapters: Callable[[InstanceRow], SourceAdapter],
    *,
    admission_stopped: Callable[[], bool] = lambda: False,
    embedder: EmbedderClient | None = None,
) -> CollectDeps:
    """The `CollectDeps` of a test: the real `RateLimiter` over `SOURCE_LIMITS` on `time.monotonic` and
    `asyncio.sleep`, a fresh `FetchContext`, `utcnow` and `embedder` (none by default)."""
    return CollectDeps(
        engine=engine,
        adapters=adapters,
        embedder=embedder,
        clock=utcnow,
        sleep=asyncio.sleep,
        admission_stopped=admission_stopped,
        limiter=RateLimiter(SOURCE_LIMITS.values(), clock=time.monotonic, sleep=asyncio.sleep),
        fetch_ctx=FetchContext(),
    )


@dataclass(frozen=True)
class MadeJob:
    job_id: int
    posting_id: int
    observation_ids: tuple[int, ...]


class JobFactory(Protocol):
    def __call__(
        self,
        *,
        native_id: str,
        source_id: str = "jsearch",
        instance_key: str = "default",
        observations: Sequence[tuple[datetime, int | None]] = (),
        title: str = "Python Developer",
        company: str = "Acme",
        availability: str = "active",
        structured_seniority: str | None = None,
    ) -> Awaitable[MadeJob]: ...

    def many(self, n: int) -> Awaitable[list[int]]: ...


_JOB: Final = q(
    "INSERT INTO jobs (company_key, company_display, title, title_norm, location_kind, work_mode_possible, "
    "work_mode_source, employment_source, ad_language, completeness, posted_precision, first_seen_at, "
    "last_seen_at, availability, availability_changed_at, source_ids, field_provenance, det_facts_hash, "
    "structured_seniority) "
    "VALUES (lower(:company), :company, :title, lower(:title), 'unknown', ARRAY['remote','hybrid','onsite'], "
    "'none', 'none', 'unknown', 'full', 'unknown', :first, :last, :availability, :changed, ARRAY[:source], "
    "'{}', :facts, :seniority) RETURNING id"
)
_POSTING: Final = q(
    "INSERT INTO postings (source_instance_id, native_id, job_id, first_seen_at, last_seen_at, seen_count, "
    "availability, availability_changed_at) VALUES (:instance, :native_id, :job, :first, :last, :seen, "
    ":availability, :changed) RETURNING id"
)
_OBSERVATION: Final = q(
    "INSERT INTO posting_observations (posting_id, raw_item_id, source_instance_id, raw_response_sha256, "
    "observed_at, normalized, content_hash, completeness, quality) VALUES (:posting, :raw_item, :instance, "
    ":sha, :at, '{}', :sha, 'full', 'ok') RETURNING id"
)
_ENSURE_INSTANCE: Final = q(
    "INSERT INTO source_instances (source_id, instance_key) VALUES (:s, :k) ON CONFLICT DO NOTHING"
)
_LINK_ITEM: Final = q("UPDATE raw_items SET observation_id = :o WHERE id = :i")
_LATEST: Final = q("UPDATE postings SET latest_observation_id = :o WHERE id = :p")


async def insert_job(
    engine: AsyncEngine,
    *,
    native_id: str,
    source_id: str = "jsearch",
    instance_key: str = "default",
    observations: Sequence[tuple[datetime, int | None]] = (),
    title: str = "Python Developer",
    company: str = "Acme",
    availability: str = "active",
    availability_changed_at: datetime | None = None,
    create_instance: bool = False,
    structured_seniority: str | None = None,
) -> MadeJob:
    """A job with one posting of `(source_id, instance_key)` and one observation per entry of `observations`
    (one now without any), inserted directly; the availability of the job and the posting was last changed at
    `availability_changed_at` (the last sighting by default). With `create_instance` a missing source instance
    is created (a board that `seed_inputs` did not make). `structured_seniority` is the job's column of that
    name (the provider's own level, `source_structured.seniority`)."""
    seen = list(observations) or [(utcnow(), None)]
    first, last = min(at for at, _ in seen), max(at for at, _ in seen)
    changed = last if availability_changed_at is None else availability_changed_at
    async with engine.begin() as conn:
        if create_instance:
            await conn.execute(_ENSURE_INSTANCE, {"s": source_id, "k": instance_key})
        instance = await instance_id(conn, source_id, instance_key)
        job_params: dict[str, Any] = {
            "company": company,
            "title": title,
            "first": first,
            "last": last,
            "changed": changed,
            "availability": availability,
            "source": source_id,
            "facts": hashlib.sha256(f"{source_id}:{native_id}".encode()).digest(),
            "seniority": structured_seniority,
        }
        job = int((await conn.execute(_JOB, job_params)).scalar_one())
        posting_params = {
            "instance": instance,
            "native_id": native_id,
            "job": job,
            "first": first,
            "last": last,
            "seen": len(seen),
            "changed": changed,
            "availability": availability,
        }
        posting = int((await conn.execute(_POSTING, posting_params)).scalar_one())
        made: list[int] = []
        for at, raw_item in sorted(seen, key=lambda entry: entry[0]):
            sha = hashlib.sha256(f"{native_id}@{at.isoformat()}".encode()).digest()
            params = {"posting": posting, "raw_item": raw_item, "instance": instance, "sha": sha, "at": at}
            observation = int((await conn.execute(_OBSERVATION, params)).scalar_one())
            if raw_item is not None:
                await conn.execute(_LINK_ITEM, {"o": observation, "i": raw_item})
            made.append(observation)
        await conn.execute(_LATEST, {"o": made[-1], "p": posting})
    return MadeJob(job, posting, tuple(made))


# Task 18: `many` inserts unmerged jobs in bulk, each with det-facts of its own (no posting: the hard filters
# and the completeness checks read the `jobs` row alone), and writes the active scope's results for them.
_MANY_JOBS: Final = q(
    "INSERT INTO jobs (company_key, company_display, title, title_norm, location_kind, work_mode_possible, "
    "work_mode_source, employment_source, ad_language, completeness, posted_precision, first_seen_at, "
    "last_seen_at, availability, availability_changed_at, source_ids, field_provenance, det_facts_hash) "
    "SELECT 'musterfirma ' || g, 'Musterfirma ' || g, 'Python Developer', 'python developer', "
    "'unknown', ARRAY['remote','hybrid','onsite'], 'none', 'none', 'unknown', 'full', 'unknown', now(), "
    "now(), 'active', now(), ARRAY['jsearch'], '{}', sha256(convert_to(:tag || ':' || g, 'UTF8')) "
    "FROM generate_series(1, :n) AS g ORDER BY g RETURNING id"
)
_ACTIVE_SCOPE: Final = q(
    "SELECT s.id, s.payload FROM active_config a JOIN scope_versions s ON s.id = a.scope_version_id "
    "WHERE a.id = 1 FOR SHARE OF a"
)


async def _unguarded(conn: AsyncConnection) -> None:
    """The guard of a test's own recompute: no owner to prove."""


async def insert_jobs(engine: AsyncEngine, n: int) -> list[int]:
    """`n` unmerged jobs inserted in one statement, each with a `det_facts_hash` of its own, and the active
    scope's `hard_filter_results` for them (`filters.recompute_stale` over that scope alone, as a collection
    transaction writes them): the view of the active scope is complete. Returns the job ids, ascending."""
    async with engine.begin() as conn:
        tag = uuid.uuid4().hex
        job_ids = [int(job_id) for job_id in (await conn.execute(_MANY_JOBS, {"n": n, "tag": tag})).scalars()]
        row = (await conn.execute(_ACTIVE_SCOPE)).one()
        scopes = {int(row.id): scope_from_payload(get_object(row.payload, ""))}
        while await recompute_stale(conn, scopes, guard=_unguarded) == BATCH:
            pass
    return job_ids


@dataclass(frozen=True)
class _JobFactory:
    """The `job_factory` fixture's factory: one job a call (`insert_job`), many at once (`insert_jobs`)."""

    engine: AsyncEngine

    async def __call__(
        self,
        *,
        native_id: str,
        source_id: str = "jsearch",
        instance_key: str = "default",
        observations: Sequence[tuple[datetime, int | None]] = (),
        title: str = "Python Developer",
        company: str = "Acme",
        availability: str = "active",
        structured_seniority: str | None = None,
    ) -> MadeJob:
        return await insert_job(
            self.engine,
            native_id=native_id,
            source_id=source_id,
            instance_key=instance_key,
            observations=observations,
            title=title,
            company=company,
            availability=availability,
            structured_seniority=structured_seniority,
        )

    async def many(self, n: int) -> list[int]:
        return await insert_jobs(self.engine, n)


@pytest.fixture
def job_factory(engine: AsyncEngine) -> JobFactory:
    """`await job_factory(native_id=…, observations=[(observed_at, raw_item_id), …])`: a job with one posting
    of `(source_id, instance_key)` and one observation per entry (a `raw_item_id`, when given, is a `linked`
    raw item that the observation came from), inserted directly; one observation now without any.
    `await job_factory.many(n)` (Task 18): `n` unmerged jobs with det-facts and the active scope's hard-filter
    results (`insert_jobs`); their ids."""
    return _JobFactory(engine)


# ---- Task 16 ----------------------------------------------------------------------------------------------


class PostingFactory(Protocol):
    def __call__(
        self,
        engine: AsyncEngine,
        *,
        source_id: str,
        native_id: str,
        availability: str,
        last_seen_at: datetime,
        availability_changed_at: datetime,
        instance_key: str = "default",
        structured_seniority: str | None = None,
    ) -> Awaitable[int]: ...


async def make_posting(
    engine: AsyncEngine,
    *,
    source_id: str,
    native_id: str,
    availability: str,
    last_seen_at: datetime,
    availability_changed_at: datetime,
    instance_key: str = "default",
    structured_seniority: str | None = None,
) -> int:
    """The id of a posting of `(source_id, instance_key)` (the instance is created when absent) with its job
    (the same availability) and one observation at `last_seen_at`; `availability_changed_at` is when the
    availability last changed.

    A missing instance is created without a watchlist company: `greenhouse` / `default` is such an instance, a
    board that no watchlist entry owns, so a test that plans a run over the watchlist (Tasks 19 and 20) must
    not expect it to be planned or served; the watchlist boards come from `seed_inputs` (`board`, `board20`).
    """
    made = await insert_job(
        engine,
        native_id=native_id,
        source_id=source_id,
        instance_key=instance_key,
        observations=[(last_seen_at, None)],
        availability=availability,
        availability_changed_at=availability_changed_at,
        create_instance=True,
        structured_seniority=structured_seniority,
    )
    return made.posting_id


@pytest.fixture
def posting_factory() -> PostingFactory:
    """`await posting_factory(engine, source_id=…, native_id=…, availability=…, last_seen_at=…,
    availability_changed_at=…)`: a posting with its job and one observation; returns the posting id.

    As `make_posting` says: a missing instance is created without a watchlist company (`greenhouse` /
    `default` is such a board), so a test that plans a run over the watchlist must not expect it to be planned
    or served; the watchlist boards come from `seed_inputs`."""
    return make_posting


BOARD_SLUG: Final = "acme-board"
_BOARD_COUNT: Final = q("UPDATE source_instances SET last_snapshot_active_count = :n WHERE id = :i")


@dataclass(frozen=True)
class Board:
    """A watchlist board of the fixtures: its source instance and its postings by native id."""

    instance_id: int
    postings: Mapping[str, int]


async def make_board(engine: AsyncEngine, native_ids: Sequence[str]) -> Board:
    """The Greenhouse board `BOARD_SLUG` (its fixture route answers `greenhouse_404`; enabled by
    `seed_inputs(…, enabled=("ats",))`), with one `active` posting and job per native id, sighted a day ago,
    and `last_snapshot_active_count` = the number of postings."""
    await seed_inputs(engine, companies=(("greenhouse", BOARD_SLUG),), enabled=("ats",))
    sighted = utcnow() - timedelta(days=1)
    postings = {
        native_id: await make_posting(
            engine,
            source_id="greenhouse",
            instance_key=BOARD_SLUG,
            native_id=native_id,
            availability="active",
            last_seen_at=sighted,
            availability_changed_at=sighted,
        )
        for native_id in native_ids
    }
    async with engine.begin() as conn:
        instance = await instance_id(conn, "greenhouse", BOARD_SLUG)
        await conn.execute(_BOARD_COUNT, {"n": len(postings), "i": instance})
    return Board(instance, MappingProxyType(postings))


@pytest.fixture
async def board(engine: AsyncEngine, current_epoch: int) -> Board:
    """`BOARD_SLUG` with the 3 active postings `a`, `b` and `c`."""
    return await make_board(engine, ("a", "b", "c"))


@pytest.fixture
async def board20(engine: AsyncEngine, current_epoch: int) -> Board:
    """`BOARD_SLUG` with the 20 active postings `p01` to `p20`."""
    return await make_board(engine, tuple(f"p{n:02d}" for n in range(1, 21)))


def seed_inputs_sync(server: PgServer, **inputs: Any) -> None:
    """`seed_inputs(**inputs)` for a sync test, on an engine of its own against `server`."""

    async def seed() -> None:
        engine = make_null_engine(server.dsn("app", driver="sqlalchemy"))
        try:
            await seed_inputs(engine, **inputs)
        finally:
            await engine.dispose()

    asyncio.run(seed())


def manual_paid_run(server: PgServer, *, only: str) -> int:
    """A queued manual run that includes paid sources, over the test query with only the source switch
    `only` enabled (Ruling P5); its `run_collect(id, 1)` job, deferred on the task app, waits for a worker.
    Returns the run id."""
    seed_inputs_sync(server, queries=(TEST_QUERY,), enabled=(only,))

    async def request() -> int:
        make_queue_app(config_for(server, "worker").dsn("psycopg"))  # the task app `defer_in_tx` defers on
        engine = make_null_engine(server.dsn("app", driver="sqlalchemy"))
        try:
            async with engine.begin() as conn:
                row, created = await runs.request_manual_run(conn, include_paid=True, defer=defer_in_tx)
        finally:
            await engine.dispose()
        if not created:
            raise RuntimeError(f"manual_paid_run: collection {row.id} was already active")
        return row.id

    return asyncio.run(request())


# ---- Task 17 ----------------------------------------------------------------------------------------------

_PLACEHOLDER_JOB: Final = q(
    "INSERT INTO jobs (company_key, company_display, title, title_norm, seniority_tokens, location_kind, "
    "work_mode_possible, work_mode_source, employment_source, ad_language, completeness, posted_precision, "
    "first_seen_at, last_seen_at, availability, availability_changed_at, source_ids, field_provenance, "
    "det_facts_hash) VALUES (:company_key, :company, :title, :title_norm, CAST(:tokens AS text[]), "
    "'unknown', ARRAY['remote','hybrid','onsite'], 'none', 'none', 'unknown', 'full', 'unknown', :at, :at, "
    "'active', :at, CAST(:sources AS text[]), '{}', :facts) RETURNING id"
)
_SOURCE_POSTING: Final = q(
    "INSERT INTO postings (source_instance_id, native_id, job_id, first_seen_at, last_seen_at, availability, "
    "availability_changed_at) VALUES (:instance, :native_id, :job, :at, :at, 'active', :at) RETURNING id"
)
_NORMALIZED_OBSERVATION: Final = q(
    "INSERT INTO posting_observations (posting_id, source_instance_id, raw_response_sha256, observed_at, "
    "normalized, content_hash, completeness, quality) VALUES (:posting, :instance, :sha, :at, :normalized, "
    ":sha, :completeness, :quality) RETURNING id",
    jsonb=["normalized"],
)
_POSTING_ROW: Final = q("SELECT source_instance_id, native_id FROM postings WHERE id = :p")
_WATCHLIST_COMPANY: Final = q(
    "INSERT INTO watchlist_companies (display_name, company_key, ats_type, board_slug, lever_region, "
    "detection, validation_status) VALUES (:display, :key, :ats, :slug, :region, 'manual', 'ok') RETURNING id"
)


@dataclass(frozen=True)
class JobPostings:
    """A job of `job_with_postings`: its postings and their observations, by source id."""

    job_id: int
    postings: Mapping[str, int]
    observations: Mapping[str, int]


async def _posting_instance(conn: AsyncConnection, source_id: str, watchlist_display: str | None) -> int:
    """The `default` instance of `source_id` (created when absent), or, for an ATS source with a
    `watchlist_display`, the board instance of a new watchlist company of that display name."""
    if watchlist_display is None or source_id not in ATS_SOURCES:
        await conn.execute(_ENSURE_INSTANCE, {"s": source_id, "k": "default"})
        return await instance_id(conn, source_id)
    company = {
        "display": watchlist_display,
        "key": company_key(watchlist_display),
        "ats": source_id,
        "slug": f"board-{uuid.uuid4().hex[:12]}",
        "region": "global" if source_id == "lever" else None,
    }
    company_id = int((await conn.execute(_WATCHLIST_COMPANY, company)).scalar_one())
    return await instance_for_company(conn, company_id)


async def _observe(
    conn: AsyncConnection,
    posting_id: int,
    instance: int,
    native_id: str,
    at: datetime,
    overrides: Mapping[str, Any],
) -> int:
    """A new observation of the posting at `at` (`observations.normalized` with `overrides`), its latest."""
    posting = normalized(native_id, **overrides)
    params = {
        "posting": posting_id,
        "instance": instance,
        "sha": hashlib.sha256(f"{native_id}@{at.isoformat()}@{uuid.uuid4()}".encode()).digest(),
        "at": at,
        "normalized": posting.model_dump(mode="json"),
        "completeness": posting.description_completeness,
        "quality": posting.description_quality,
    }
    observation = int((await conn.execute(_NORMALIZED_OBSERVATION, params)).scalar_one())
    await conn.execute(_LATEST, {"o": observation, "p": posting_id})
    return observation


async def add_posting(
    conn: AsyncConnection,
    job_id: int,
    source_id: str,
    *,
    observed_at: datetime,
    watchlist_display: str | None = None,
    **overrides: Any,
) -> tuple[int, int]:
    """A posting of `source_id` on the job with one observation at `observed_at` (`observations.normalized`
    with `overrides`); an ATS posting is on a new watchlist board when `watchlist_display` is given. Returns
    the posting id and the observation id."""
    instance = await _posting_instance(conn, source_id, watchlist_display)
    native_id = f"{source_id}-{uuid.uuid4().hex[:12]}"
    posting_params = {"instance": instance, "native_id": native_id, "job": job_id, "at": observed_at}
    posting = int((await conn.execute(_SOURCE_POSTING, posting_params)).scalar_one())
    return posting, await _observe(conn, posting, instance, native_id, observed_at, overrides)


async def add_observation(
    conn: AsyncConnection, posting_id: int, *, observed_at: datetime, **overrides: Any
) -> int:
    """A newer observation of the posting (`observations.normalized` with `overrides`), now its latest one;
    returns its id."""
    row = (await conn.execute(_POSTING_ROW, {"p": posting_id})).one()
    return await _observe(
        conn, posting_id, int(row.source_instance_id), row.native_id, observed_at, overrides
    )


class JobWithPostings(Protocol):
    def __call__(
        self,
        postings: Mapping[str, Mapping[str, Any]],
        *,
        observed_at: datetime | None = None,
        watchlist_display: str | None = None,
    ) -> Awaitable[JobPostings]: ...


@pytest.fixture
def job_with_postings(engine: AsyncEngine) -> JobWithPostings:
    """`await job_with_postings({source_id: {normalized overrides}, ...})`: a job with one posting and one
    observation per source, in the mapping's order, all observed at `observed_at` (an hour ago by default) and
    first seen then; the ATS postings are on new watchlist boards named `watchlist_display` when it is given.
    The job row is a placeholder (see the module docstring)."""

    async def make(
        postings: Mapping[str, Mapping[str, Any]],
        *,
        observed_at: datetime | None = None,
        watchlist_display: str | None = None,
    ) -> JobPostings:
        if not postings:
            raise ValueError("job_with_postings needs at least one posting")
        at = utcnow() - timedelta(hours=1) if observed_at is None else observed_at
        first = normalized(**dict(next(iter(postings.values()))))
        title = normalize_title(first.title_raw).title
        normalized_title = normalize_title(title)
        job_params = {
            "company_key": company_key(first.company_raw),
            "company": first.company_raw,
            "title": title,
            "title_norm": normalized_title.title_norm,
            "tokens": sorted(normalized_title.seniority_tokens),
            "at": at,
            "sources": sorted(postings),
            "facts": hashlib.sha256(b"a placeholder, not the hash of the job's facts").digest(),
        }
        made_postings: dict[str, int] = {}
        made_observations: dict[str, int] = {}
        async with engine.begin() as conn:
            job = int((await conn.execute(_PLACEHOLDER_JOB, job_params)).scalar_one())
            for source_id, overrides in postings.items():
                made_postings[source_id], made_observations[source_id] = await add_posting(
                    conn, job, source_id, observed_at=at, watchlist_display=watchlist_display, **overrides
                )
        return JobPostings(job, MappingProxyType(made_postings), MappingProxyType(made_observations))

    return make


# ---- Task 18 ----------------------------------------------------------------------------------------------

_ITEM_OF: Final = q("SELECT id FROM raw_items WHERE raw_response_id = :rr")
# The key a source's own id is (§8.4), as its adapter puts it first in `raw_keys`.
_NATIVE_KEY_NS: Final = {"jsearch": "htidocid", "serpapi": "htidocid", "adzuna": "adzuna"}
STEPSTONE_1: Final = "https://stepstone.de/x--1-inline.html"
STEPSTONE_2: Final = "https://stepstone.de/x--2-inline.html"


class _OneItem:
    """The adapter of a stored one-element page whose one item parses as `posting` (`parse_response` calls
    nothing else)."""

    def __init__(self, posting: NormalizedPosting) -> None:
        self._posting = posting

    def elements(self, page: RawPage) -> list[bytes]:
        return [page.body]

    def parse(self, page: RawPage) -> list[ItemOutcome]:
        return [Parsed(0, self._posting.native_id, self._posting.item_ts, self._posting)]


def _item_posting(source_id: str, overrides: Mapping[str, Any]) -> NormalizedPosting:
    """`observations.normalized` with the overrides, a fresh native id unless one is given, the primary link
    canonical, and the keys of the source's adapter unless `raw_keys` is given (module docstring)."""
    fields = dict(overrides)
    native_id = str(fields.pop("native_id", f"{source_id}-{uuid.uuid4().hex[:12]}"))
    link = fields.get("primary_link")
    if link is not None:
        fields["primary_link"] = link = canonical_url(link)
    if "raw_keys" not in fields:
        ns = _NATIVE_KEY_NS.get(source_id)
        native = [AdKey(ns, native_id)] if ns is not None else []
        fields["raw_keys"] = (*native, *(keys_from_link(link) if link is not None else []))
    return normalized(native_id, **fields)


async def pending_item(
    engine: AsyncEngine,
    owner: RunOwner,
    source_id: str,
    normalized_overrides: Mapping[str, Any],
    *,
    fetched_at: datetime | None = None,
) -> int:
    """The id of a `pending` raw item of `owner`'s run from the source's default instance, its page fetched at
    `fetched_at` (now when `None`), parsed by `items.parse_response` (module docstring)."""
    at = utcnow() if fetched_at is None else fetched_at
    posting = _item_posting(source_id, normalized_overrides)
    cell = await source_cell(engine, source_id)
    async with engine.begin() as conn:
        instance = await instance_id(conn, source_id)
        spec = RequestSpec(
            source_instance_id=instance,
            operation="list",
            cell_id=cell,
            page_no=1,
            params={"item": uuid.uuid4().hex},
            cursor_in=None,
            detail_native_id=None,
            parent_fetch_request_id=None,
            window=None,
            request_class="free",
        )
        fetch_request = await receipts.insert_fetch_request(
            conn,
            owner=owner,
            cell_id=cell,
            source_instance_id=instance,
            phase="primary",
            spec=spec,
            request_key=uuid.uuid4().bytes,
            attempt_seq=1,
            status="planned",
        )
        await transition(conn, "fetch_requests", fetch_request, ["planned"], "dispatched")
        body = json.dumps({"native_id": posting.native_id, "nonce": uuid.uuid4().hex}).encode()
        page = RawPage(spec, 200, {"content-type": "application/json"}, body, at)
        raw_response = await receipts.store_response(
            conn,
            fetch_request,
            page,
            source_instance_id=instance,
            ledger_entry_id=None,
            succeeded=True,
            items=1,
        )
        await transition(
            conn,
            "fetch_requests",
            fetch_request,
            ["dispatched"],
            "succeeded",
            set_={"http_status": 200, "items": 1},
        )
    async with engine.begin() as conn:
        adapter = cast(SourceAdapter, _OneItem(posting))
        await items.parse_response(conn, raw_response, adapter=adapter, owner=owner)
        return int((await conn.execute(_ITEM_OF, {"rr": raw_response})).scalar_one())


async def resolved(engine: AsyncEngine, owner: RunOwner, raw_item_id: int) -> str:
    """`identity.resolve_item` of the raw item in a transaction of its own; its resolution."""
    async with engine.begin() as conn:
        return await resolve_item(conn, owner=owner, raw_item_id=raw_item_id)


_JOB_OF_POSTING: Final = q(
    "SELECT p.id, p.job_id FROM postings p JOIN source_instances i ON i.id = p.source_instance_id "
    "WHERE i.source_id = :s AND i.instance_key = 'default' AND p.native_id = :n"
)


@dataclass(frozen=True)
class KeyPair:
    """Job A (a JSearch posting: `htidocid` `H`, `pub:stepstone.de` 1) and job B (an Adzuna posting, id 9)."""

    a: int
    b: int
    posting_a: int
    posting_b: int


async def key_pair(engine: AsyncEngine, owner: RunOwner, *, at: datetime) -> KeyPair:
    """A and B (`KeyPair`), resolved for `owner`'s run from items fetched at `at` and an hour later."""
    first = await pending_item(
        engine, owner, "jsearch", {"native_id": "H", "primary_link": STEPSTONE_1}, fetched_at=at
    )
    second = await pending_item(
        engine, owner, "adzuna", {"native_id": "9"}, fetched_at=at + timedelta(hours=1)
    )
    for raw_item_id in (first, second):
        if await resolved(engine, owner, raw_item_id) != "linked":
            raise RuntimeError(f"key_pair: raw item {raw_item_id} was not linked")
    async with engine.connect() as conn:
        a = (await conn.execute(_JOB_OF_POSTING, {"s": "jsearch", "n": "H"})).one()
        b = (await conn.execute(_JOB_OF_POSTING, {"s": "adzuna", "n": "9"})).one()
    return KeyPair(int(a.job_id), int(b.job_id), int(a.id), int(b.id))


_TRY_LOCK_JOB: Final = q("SELECT id FROM jobs WHERE id = :job FOR UPDATE NOWAIT")


async def job_lockable(engine: AsyncEngine, job_id: int) -> bool:
    """Whether the job row can be locked at once from a new transaction (released straight away)."""
    try:
        async with engine.begin() as conn:
            await conn.execute(_TRY_LOCK_JOB, {"job": job_id})
    except DBAPIError as err:
        if pg_error(err).sqlstate == LOCK_NOT_AVAILABLE:
            return False
        raise
    return True


# ---- Task 22 ----------------------------------------------------------------------------------------------

# A description of this many characters is over the fake embedder's 4,000: its embedding is `truncated`.
LONG_DESCRIPTION_CHARS: Final = 5_000
_AWAITING_REVISION: Final = q(
    "INSERT INTO job_revisions (job_id, revision_no, material_hash, llm_input, description_text, "
    "description_completeness, ad_language, embedding_status) VALUES (:job, 1, :hash, :llm_input, "
    ":description, 'full', 'en', 'awaiting_embedding') RETURNING id",
    jsonb=["llm_input"],
)


class RevisionsAwaiting(Protocol):
    def __call__(
        self, engine: AsyncEngine, n: int, *, duplicate_texts: int = 0, long_texts: int = 0
    ) -> Awaitable[list[int]]: ...


async def insert_revisions_awaiting(
    engine: AsyncEngine, n: int, *, duplicate_texts: int = 0, long_texts: int = 0
) -> list[int]:
    """`n` jobs (`insert_job`), each with one revision `awaiting_embedding`; the revision ids, oldest first.

    Each revision's `llm_input` has a `title`, a `company_display` and a `description_text` of its own, but
    the first `duplicate_texts` revisions share one text, and the last `long_texts` have a description of
    `LONG_DESCRIPTION_CHARS` characters. Every call makes jobs of its own (fresh native ids).
    """
    if duplicate_texts + long_texts > n or duplicate_texts == 1:
        raise ValueError(
            "revisions_awaiting: duplicate_texts must be 0 or at least 2, and fit with long_texts"
        )
    batch = uuid.uuid4().hex[:8]
    ids: list[int] = []
    for i in range(n):
        job = await insert_job(engine, native_id=f"emb-{batch}-{i}")
        k = 0 if i < duplicate_texts else i
        description = f"Ad {batch}-{k}: build and run Python services for a logistics platform."
        if i >= n - long_texts:
            description = (description + " ") * (LONG_DESCRIPTION_CHARS // len(description) + 1)
        llm_input = {
            "title": f"Python Developer {k}",
            "company_display": "Acme",
            "description_text": description,
        }
        params = {
            "job": job.job_id,
            "hash": hashlib.sha256(f"{batch}:{i}".encode()).digest(),
            "llm_input": llm_input,
            "description": description,
        }
        async with engine.begin() as conn:
            ids.append(int((await conn.execute(_AWAITING_REVISION, params)).scalar_one()))
    return ids


@pytest.fixture
def revisions_awaiting() -> RevisionsAwaiting:
    """`await revisions_awaiting(engine, n, duplicate_texts=0, long_texts=0)`: `insert_revisions_awaiting`."""
    return insert_revisions_awaiting


# ---- Task 23 ----------------------------------------------------------------------------------------------


@pytest.fixture
async def collect_deps(
    engine: AsyncEngine, adapters: Callable[[InstanceRow], SourceAdapter], fake_embedder: FakeEmbedder
) -> AsyncIterator[CollectDeps]:
    """`make_deps(engine, adapters)` with an `EmbedderClient` on the fake embedder (its HTTP client is closed
    after the test)."""
    async with make_http_client() as http:
        yield make_deps(engine, adapters, embedder=EmbedderClient(fake_embedder.base_url, http))


async def manual_run_id(engine: AsyncEngine, *, include_paid: bool = False) -> int:
    """A queued manual run over the active configuration, not admitted (`execute_run` starts it)."""
    async with engine.begin() as conn:
        return int((await conn.execute(_QUEUED_RUN, {"include_paid": include_paid})).scalar_one())


GraphFactory = Callable[[BaseCheckpointSaver[str], CollectDeps], CollectGraph]
DEFAULT_BOARD: Final = ("greenhouse", "getyourguide")  # its fixture route answers `greenhouse_getyourguide`


async def run_to_the_end(
    engine: AsyncEngine,
    deps: CollectDeps,
    *,
    include_paid: bool = False,
    queries: Sequence[str] = (),
    companies: Sequence[tuple[str, str]] = (DEFAULT_BOARD,),
    enabled: Sequence[str] = ("arbeitnow", "ats"),
    graph: GraphFactory = build_collect_graph,
    saver: BaseCheckpointSaver[str] | None = None,
) -> int:
    """The id of a manual run over the inputs given (`seed_inputs`: by default the board `DEFAULT_BOARD` and
    Arbeitnow), which `execute_run` admitted as attempt 1 of epoch 1 (it needs `current_epoch`) and ran to its
    end on `graph(saver, deps)`; raises unless the admission was `started`."""
    await seed_inputs(engine, queries=queries, companies=companies, enabled=enabled)
    run_id = await manual_run_id(engine, include_paid=include_paid)
    compiled = graph(InMemorySaver() if saver is None else saver, deps)
    admission = await runs.execute_run(engine, compiled, run_id, 1, epoch=1)
    if admission != "started":
        raise RuntimeError(f"run_to_the_end: run {run_id} was not started ({admission})")
    return run_id


CONFLICT_WORK_ID: Final = "fr:0"  # no fetch request has id 0


def conflicting_graph(saver: BaseCheckpointSaver[str], deps: CollectDeps) -> CollectGraph:
    """The real collection graph whose `fetch_cell` adds the outcome `CONFLICT_WORK_ID` (its `items` the
    Send's own fetch request id) to every update: two Sends of one step conflict in the reducer."""

    def make(
        cell_deps: CollectDeps, *, cell_timeout: timedelta
    ) -> Callable[[FetchCellInput], Awaitable[CollectUpdate]]:
        cell = make_fetch_cell(cell_deps, cell_timeout=cell_timeout)

        async def fetch_cell(state: FetchCellInput) -> CollectUpdate:
            update = await cell(state)
            conflict: FetchOutcome = {
                "work_id": CONFLICT_WORK_ID,
                "status": "succeeded",
                "items": state["fetch_request_id"],
                "new_postings": 0,
            }
            update["outcomes"] = {**update.get("outcomes", {}), CONFLICT_WORK_ID: conflict}
            return update

        return fetch_cell

    # the name `build_collect_graph` calls, replaced while it builds the graph
    with mock.patch(f"{collect_graph.__name__}.make_fetch_cell", make):
        return build_collect_graph(saver, deps)


# ---- Task 24 ----------------------------------------------------------------------------------------------

# Far from the database clock, as in the M0 retention tests: a window measured from the database's `now()`
# instead of the caller's `now` would delete nothing, or everything.
RETENTION_SHIFT: Final = timedelta(days=1000)
_DAY: Final = timedelta(days=1)

_FINISHED_RUN: Final = q(
    "INSERT INTO runs (trigger, scheduled_slot, status, include_paid, settings_version_id, scope_version_id, "
    "attempt_no, queued_at, started_at, finished_at) SELECT 'manual', NULL, 'succeeded', false, "
    "settings_version_id, scope_version_id, 1, :at, :at, :at FROM active_config RETURNING id"
)
_FREE_CELL: Final = q("INSERT INTO coverage_cells (source_instance_id) VALUES (:i) ON CONFLICT DO NOTHING")
_FREE_CELL_ID: Final = q(
    "SELECT id FROM coverage_cells WHERE source_instance_id = :i AND query_id IS NULL "
    "AND location_key IS NULL"
)
# A request of the run on the cell; a terminal one `failed` (a page may come with it), else `planned`.
_AGED_REQUEST: Final = q(
    "INSERT INTO fetch_requests (run_id, run_attempt_no, cell_id, source_instance_id, phase, operation, "
    "request_key, page_no, request_class, spec, status, error_code, http_status, created_at, finished_at) "
    "SELECT CAST(:run AS bigint), 1, c.id, c.source_instance_id, 'primary', 'list', CAST(:key AS bytea), 1, "
    "'free', '{}', CAST(:status AS text), CASE WHEN CAST(:status AS text) = 'failed' THEN 'http_500' END, "
    "CASE WHEN CAST(:status AS text) = 'failed' THEN 500 END, CAST(:at AS timestamptz), "
    "CASE WHEN CAST(:status AS text) = 'failed' THEN CAST(:at AS timestamptz) END FROM coverage_cells c "
    "WHERE c.id = :cell RETURNING id"
)
_AGED_RESPONSE: Final = q(
    "INSERT INTO raw_responses (fetch_request_id, source_instance_id, http_status, headers, body_zstd, "
    "body_sha256, body_ref_id, fetched_at, expires_at, purged_at) SELECT f.id, f.source_instance_id, 500, "
    "'{}', CAST(:body AS bytea), CAST(:sha AS bytea), CAST(:ref AS bigint), CAST(:fetched AS timestamptz), "
    "CAST(:expires AS timestamptz), CAST(:purged AS timestamptz) FROM fetch_requests f WHERE f.id = :fr "
    "RETURNING id"
)
_LINK_RESPONSE: Final = q("UPDATE fetch_requests SET raw_response_id = :r WHERE id = :f")
_AGED_ITEM: Final = q(
    "INSERT INTO raw_items (raw_response_id, item_index, native_id, item_sha256, disposition, reason_code, "
    "normalized, created_at, disposed_at) VALUES (:r, :index, :native, :sha, :disposition, :reason, "
    "NULLIF(:normalized, CAST('null' AS jsonb)), :at, :at) RETURNING id",  # JSONB binds None as JSON null
    jsonb=["normalized"],
)
_AGED_SNAPSHOT: Final = q(
    "INSERT INTO ats_snapshots (source_instance_id, status, incomplete_reason, event_snapshot_id, "
    "member_count, taken_at, applied_at, acknowledged_at) VALUES (:i, :status, "
    "CASE WHEN :status = 'incomplete' THEN 'no_end' END, :event, 0, :at, :at, :acknowledged) RETURNING id"
)
_POINT_SNAPSHOTS: Final = q(
    "UPDATE source_instances SET last_complete_snapshot_id = :complete, mass_event_snapshot_id = :mass "
    "WHERE id = :i"
)
_EMBEDDING: Final = q(
    "INSERT INTO embeddings (embedding_model_id, text_sha256, input_kind, vector, created_at) "
    "VALUES ('m1-retention-test', :sha, 'passage', CAST(:vector AS halfvec), :at) RETURNING id"
)
_EMBEDDED_REVISION: Final = q(
    "INSERT INTO job_revisions (job_id, revision_no, material_hash, llm_input, description_text, "
    "description_completeness, ad_language, embedding_status, embedding_id) VALUES (:job, 1, :hash, '{}', "
    "'Build Python services.', 'full', 'en', 'embedded', :embedding) RETURNING id"
)
_INTERVAL: Final = q(
    "INSERT INTO coverage_intervals (cell_id, kind, span, gap_status, reason, created_at, closed_at) "
    "VALUES (:cell, :kind, tstzrange(:lower, :upper, '[)'), :gap_status, :reason, :created, :closed) "
    "RETURNING id"
)
_UNIT_VECTOR: Final = "[" + ",".join(["0"] * 1023 + ["1"]) + "]"


@dataclass(frozen=True)
class M1Rows:
    """The aged rows of `m1_rows`, by name, and the `now` they are aged from (`utcnow() +
    RETENTION_SHIFT`)."""

    now: datetime
    ids: Mapping[str, int]

    def __getitem__(self, name: str) -> int:
        return self.ids[name]


async def insert_finished_run(conn: AsyncConnection, *, at: datetime) -> int:
    """A `succeeded` manual run at attempt 1 that was queued, started and finished at `at`."""
    return int((await conn.execute(_FINISHED_RUN, {"at": at})).scalar_one())


async def free_cell(conn: AsyncConnection, source_id: str, instance_key: str = "default") -> int:
    """The coverage cell without query and location of the instance (Arbeitnow's, a board's)."""
    instance = await instance_id(conn, source_id, instance_key)
    await conn.execute(_FREE_CELL, {"i": instance})
    return int((await conn.execute(_FREE_CELL_ID, {"i": instance})).scalar_one())


async def aged_request(
    conn: AsyncConnection, *, run_id: int, cell_id: int, created_at: datetime, status: str = "failed"
) -> int:
    """A fetch request of `run_id` on `cell_id` created at `created_at`: `failed` (terminal) or `planned`."""
    params = {"run": run_id, "cell": cell_id, "key": os.urandom(32), "status": status, "at": created_at}
    return int((await conn.execute(_AGED_REQUEST, params)).scalar_one())


async def aged_response(
    conn: AsyncConnection,
    fetch_request_id: int,
    *,
    fetched_at: datetime,
    expires_at: datetime,
    body: bool = True,
    ref: int | None = None,
    purged_at: datetime | None = None,
) -> int:
    """The stored response of the request: its own body, or (`body=False`) the body of the row `ref` (§7.7
    deduplication), or nothing once `purged_at` is set."""
    params = {
        "fr": fetch_request_id,
        "body": os.urandom(16) if body else None,
        "sha": os.urandom(32),
        "ref": ref,
        "fetched": fetched_at,
        "expires": expires_at,
        "purged": purged_at,
    }
    response = int((await conn.execute(_AGED_RESPONSE, params)).scalar_one())
    await conn.execute(_LINK_RESPONSE, {"r": response, "f": fetch_request_id})
    return response


async def _aged_item(
    conn: AsyncConnection, response: int, index: int, *, created_at: datetime, deferred: bool = False
) -> int:
    params = {
        "r": response,
        "index": index,
        "native": f"m1-{index}",
        "sha": os.urandom(32),
        "disposition": "deferred" if deferred else "linked",
        "reason": "job_fenced" if deferred else None,
        "normalized": {"title": "Python Developer"} if deferred else None,
        "at": created_at,
    }
    return int((await conn.execute(_AGED_ITEM, params)).scalar_one())


async def _snapshot(
    conn: AsyncConnection,
    board: int,
    status: str,
    *,
    taken_at: datetime,
    event: int | None = None,
    acknowledged_at: datetime | None = None,
) -> int:
    params = {"i": board, "status": status, "event": event, "at": taken_at, "acknowledged": acknowledged_at}
    return int((await conn.execute(_AGED_SNAPSHOT, params)).scalar_one())


async def _interval(
    conn: AsyncConnection,
    cell: int,
    kind: str,
    *,
    lower: datetime,
    created_at: datetime,
    closed_at: datetime | None = None,
) -> int:
    gap = kind == "gap"
    params = {
        "cell": cell,
        "kind": kind,
        "lower": lower,
        "upper": lower + _DAY,
        "gap_status": None if not gap else ("open" if closed_at is None else "closed"),
        "reason": "page_cap" if gap else None,
        "created": created_at,
        "closed": closed_at,
    }
    return int((await conn.execute(_INTERVAL, params)).scalar_one())


@pytest.fixture
async def m1_rows(engine: AsyncEngine) -> M1Rows:
    """The aged rows of the M1 retention rules, on either side of each window, at `now = utcnow() + 1000 d`:

    - `raw_items` (90 d by `created_at`): `item_old` (91 d, linked to the observation `obs_old`),
      `item_recent` (89 d, linked to `obs_recent`) and `item_deferred` (91 d, `deferred('job_fenced')`: a
      later run re-resolves it), all on `items_response`;
    - `raw_bodies` (by `expires_at`): `body_expired` (1 s before `now`), `body_live` (1 s after) and the
      deduplicated `body_shared` (fetched 398 d ago, on the 398-day-old request `fr_shared`), whose
      `expires_at` its referencing row `body_ref` (no body of its own) raised to `now + 10 d`;
    - `fetch_requests` (397 d by `created_at`, terminal): `fr_old` (398 d, `failed`, with its response
      `response_old`), `fr_recent` (396 d), `fr_open` (398 d, still `planned`) and `fr_shared`;
    - `ats_snapshots` (30 d by `taken_at`) of the board instance `board`: `snap_old` (31 d), `snap_recent`
      (29 d), the board's `last_complete` (45 d) and its open mass event: `mass_open` (40 d, the instance's
      `mass_event_snapshot_id`) and its unacknowledged follow-up `mass_followup` (35 d); `mass_closed` (50 d)
      opened an event that is no longer open;
    - `embeddings`: `emb_used` (the embedding of the revision `revision`) and `emb_orphan` (no revision);
    - `coverage_intervals` (397 d by `coalesce(closed_at, created_at)`) of the Arbeitnow cell:
      `gap_closed_old` (closed 398 d ago), `gap_closed_recent` (created 400 d, closed 396 d ago),
      `covered_old` (398 d), `covered_recent` (396 d) and `gap_open` (open, 10 d).

    Every request belongs to `run`, a run that finished a day before `now` (the `runs` rule keeps it).
    """
    now = utcnow() + RETENTION_SHIFT
    ids: dict[str, int] = {}
    async with engine.begin() as conn:
        ids["run"] = run = await insert_finished_run(conn, at=now - _DAY)
        ids["cell"] = cell = await free_cell(conn, "arbeitnow")
        await conn.execute(_ENSURE_INSTANCE, {"s": "greenhouse", "k": "m1-retention-board"})
        ids["board"] = board = await instance_id(conn, "greenhouse", "m1-retention-board")
        board_cell = await free_cell(conn, "greenhouse", "m1-retention-board")

        async def request(name: str, days: float, status: str = "failed") -> int:
            ids[name] = await aged_request(
                conn, run_id=run, cell_id=board_cell, created_at=now - days * _DAY, status=status
            )
            return ids[name]

        purged_at = now - 60 * _DAY  # a body purged long ago
        ids["response_old"] = await aged_response(
            conn,
            await request("fr_old", 398),
            fetched_at=now - 398 * _DAY,
            expires_at=now - 368 * _DAY,
            body=False,
            purged_at=purged_at,
        )
        await aged_response(
            conn,
            await request("fr_recent", 396),
            fetched_at=now - 396 * _DAY,
            expires_at=now - 366 * _DAY,
            body=False,
            purged_at=purged_at,
        )
        await request("fr_open", 398, status="planned")
        ids["body_expired"] = await aged_response(
            conn,
            await request("fr_expired", 31),
            fetched_at=now - 30 * _DAY - timedelta(seconds=1),
            expires_at=now - timedelta(seconds=1),
        )
        ids["body_live"] = await aged_response(
            conn,
            await request("fr_live", 29),
            fetched_at=now - 30 * _DAY + timedelta(seconds=1),
            expires_at=now + timedelta(seconds=1),
        )
        ids["body_shared"] = shared = await aged_response(
            conn, await request("fr_shared", 398), fetched_at=now - 398 * _DAY, expires_at=now + 10 * _DAY
        )
        ids["body_ref"] = await aged_response(
            conn,
            await request("fr_ref", 20),
            fetched_at=now - 20 * _DAY,
            expires_at=now + 10 * _DAY,
            body=False,
            ref=shared,
        )
        ids["items_response"] = items = await aged_response(
            conn,
            await request("fr_items", 100),
            fetched_at=now - 100 * _DAY,
            expires_at=now - 70 * _DAY,
            body=False,
            purged_at=purged_at,
        )
        ids["item_old"] = await _aged_item(conn, items, 0, created_at=now - 91 * _DAY)
        ids["item_recent"] = await _aged_item(conn, items, 1, created_at=now - 89 * _DAY)
        ids["item_deferred"] = await _aged_item(conn, items, 2, created_at=now - 91 * _DAY, deferred=True)

        ids["snap_old"] = await _snapshot(conn, board, "incomplete", taken_at=now - 31 * _DAY)
        ids["snap_recent"] = await _snapshot(conn, board, "incomplete", taken_at=now - 29 * _DAY)
        ids["last_complete"] = last = await _snapshot(conn, board, "complete", taken_at=now - 45 * _DAY)
        ids["mass_closed"] = await _snapshot(conn, board, "mass_disappearance", taken_at=now - 50 * _DAY)
        ids["mass_open"] = mass = await _snapshot(conn, board, "mass_disappearance", taken_at=now - 40 * _DAY)
        ids["mass_followup"] = await _snapshot(
            conn, board, "mass_disappearance", taken_at=now - 35 * _DAY, event=mass
        )
        await conn.execute(_POINT_SNAPSHOTS, {"i": board, "complete": last, "mass": mass})

        vector = {"vector": _UNIT_VECTOR, "at": now - _DAY}
        ids["emb_used"] = used = int(
            (await conn.execute(_EMBEDDING, {"sha": os.urandom(32), **vector})).scalar_one()
        )
        ids["emb_orphan"] = int(
            (await conn.execute(_EMBEDDING, {"sha": os.urandom(32), **vector})).scalar_one()
        )

        for offset, (name, kind, created, closed) in enumerate(
            (
                ("gap_closed_old", "gap", 400, 398),
                ("gap_closed_recent", "gap", 400, 396),
                ("covered_old", "covered", 398, None),
                ("covered_recent", "covered", 396, None),
                ("gap_open", "gap", 10, None),
            )
        ):
            ids[name] = await _interval(
                conn,
                cell,
                kind,
                lower=now - (500 + 10 * offset) * _DAY,  # a span of its own
                created_at=now - created * _DAY,
                closed_at=None if closed is None else now - closed * _DAY,
            )
    old = await insert_job(
        engine, native_id="m1-retention-old", observations=[(now - 91 * _DAY, ids["item_old"])]
    )
    recent = await insert_job(
        engine, native_id="m1-retention-recent", observations=[(now - 89 * _DAY, ids["item_recent"])]
    )
    ids["obs_old"], ids["obs_recent"] = old.observation_ids[0], recent.observation_ids[0]
    async with engine.begin() as conn:
        params = {"job": old.job_id, "hash": os.urandom(32), "embedding": used}
        ids["revision"] = int((await conn.execute(_EMBEDDED_REVISION, params)).scalar_one())
    return M1Rows(now=now, ids=MappingProxyType(ids))
