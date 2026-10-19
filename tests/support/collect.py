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
"""

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Final

import httpx
import procrastinate
from jobhunter.clock import utcnow
from jobhunter.db.sql import q
from jobhunter.db.transitions import transition
from jobhunter.quota.windows import Provider
from jobhunter.sources.acquire import AcquireContext
from jobhunter.sources.base import REMOTE_DE, Billing, CellPlan, RequestSpec, SourceAdapter
from jobhunter.sources.jsearch import JSearchAdapter
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
