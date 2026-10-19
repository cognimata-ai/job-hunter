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

Task 13: `DEFAULT_CELL` is every `planner.CellRow` field of a never-attempted JSearch `remote-de` cell, as a
dict; `seed_inputs` writes the queries and the watchlist companies (with their board instances) of a test and
a new settings version that enables them (and, with `enabled`, exactly those sources), pointed at by
`active_config`, so a run queued afterwards plans them (plan Deviation 3).
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Final

import httpx
import procrastinate
from jobhunter.clock import utcnow
from jobhunter.db.sql import q
from jobhunter.db.transitions import transition
from jobhunter.quota.windows import Provider
from jobhunter.sources.acquire import AcquireContext
from jobhunter.sources.base import REMOTE_DE, Billing, CellPlan, RequestSpec, SourceAdapter
from jobhunter.sources.instances import instance_for_company
from jobhunter.sources.jsearch import JSearchAdapter
from jobhunter.types import JSONValue, get_list, get_object
from jobhunter.worker import runs
from jobhunter.worker.reconcilers import ReconcileContext
from jobhunter.worker.runs import RunOwner
from jobhunter.worker.tasks import defer_in_tx
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.quota import seeded_run_settings

_QUEUED_RUN: Final = q(
    "INSERT INTO runs (trigger, scheduled_slot, status, include_paid, settings_version_id, scope_version_id) "
    "SELECT 'manual', NULL, 'queued', :include_paid, settings_version_id, scope_version_id "
    "FROM active_config RETURNING id"
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
    engine: AsyncEngine, queue_app: procrastinate.App, now: datetime | None = None, epoch: int = 1
) -> ReconcileContext:
    """A reconcile context at `now` (the current time when omitted) for worker `epoch`."""
    return ReconcileContext(
        engine=engine, queue=queue_app, defer=defer_in_tx, now=utcnow() if now is None else now, epoch=epoch
    )


# Every `planner.CellRow` field of a never-attempted JSearch `remote-de` cell, with the column defaults of
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
) -> None:
    """The test's collection inputs (plan Deviation 3), committed:

    - a `queries` row per text (`lang='en'`, `origin='user'`);
    - a watchlist company per `(ats_type, slug)` (`detection='manual'`, `validation_status='ok'`; a Lever
      board in the `global` region) and its board instance (`instances.instance_for_company`);
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
            board = {"ats": ats, "slug": slug, "region": "global" if ats == "lever" else None}
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
