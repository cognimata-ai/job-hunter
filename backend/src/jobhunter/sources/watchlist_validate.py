"""The `watchlist_validate` operation (spec §5.4; plan Deviation 20: the API route is M4's).

`validate_company` makes one GET of the watchlist company's board: the first list page at the ATS's fixed
API host, through the board's own adapter and `http.send`, so redirects are refused except within the
Personio host family (§5.2), exactly as collection will request it. The result:

- `ok`: a 2xx whose envelope passes the board's contract; the board's source instance is created (or an
  orphaned one linked) through `instances.instance_for_company`;
- `not_found`: a 404;
- `error`: anything else (a transport failure, another status, a redirect, a body that breaks the contract or
  the provider's own error report).

`validation_status` and `validated_at` are written either way. A watchlist company that does not exist raises
`LookupError` before anything is sent.

The handler (`@register_operation_handler("watchlist_validate")`, payload `{"watchlist_company_id": id}`,
dedup key `watchlist_validate:<id>`, `request_validation`) holds no lock while the GET is in flight: it
locks the operation's owner row only after the response, in the transaction of the writes, so the heartbeat
is never held up by the request. A company removed before its validation ran ends the operation quietly.
"""

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Final, Literal

import httpx
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.clock import utcnow
from jobhunter.db.sql import q
from jobhunter.observability.logging import get_logger
from jobhunter.sources import ats, http
from jobhunter.sources.base import SourceError
from jobhunter.sources.http import HttpOutcome
from jobhunter.sources.instances import InstanceRow, instance_for_company
from jobhunter.types import JSONValue, get_int
from jobhunter.worker.operations import (
    OperationContext,
    create_operation,
    lock_operation_owner,
    register_operation_handler,
)
from jobhunter.worker.tasks import Deferrer

KIND: Final = "watchlist_validate"
Validation = Literal["ok", "not_found", "error"]
# The spec of a validation GET belongs to no instance and no cell yet: neither exists before it succeeds.
_NO_INSTANCE: Final = 0
_NO_CELL: Final = 0

_COMPANY = q(
    "SELECT ats_type, board_slug, lever_region, display_name FROM watchlist_companies WHERE id = :id"
)
_RECORD = q("UPDATE watchlist_companies SET validation_status = :status, validated_at = :now WHERE id = :id")

_log = get_logger("jobhunter.sources.watchlist_validate")


def dedup_key(company_id: int) -> str:
    return f"{KIND}:{company_id}"


async def request_validation(
    conn: AsyncConnection, company_id: int, *, defer: Deferrer | None
) -> tuple[int, bool]:
    """The live validation operation of the company, created (and its job deferred) when there is none."""
    payload: dict[str, JSONValue] = {"watchlist_company_id": company_id}
    return await create_operation(
        conn, kind=KIND, payload=payload, dedup_key=dedup_key(company_id), defer=defer
    )


def _classify(adapter: ats.AtsAdapter, outcome: HttpOutcome) -> Validation:
    page = outcome.page
    if outcome.error is not None or page is None:
        return "error"
    if page.status == 404:
        return "not_found"
    if not 200 <= page.status < 300:
        return "error"
    try:
        adapter.check_envelope(page)
    except SourceError:
        return "error"
    return "ok"


async def validate_company(
    conn: AsyncConnection,
    company_id: int,
    *,
    client: httpx.AsyncClient,
    now: datetime,
    guard: Callable[[AsyncConnection], Awaitable[None]] | None = None,
) -> Validation:
    """Validate the board of watchlist company `company_id` with one GET (module docstring) and record it.

    `guard` runs after the response and before the writes, in `conn`'s transaction (the handler's owner lock).
    """
    company = (await conn.execute(_COMPANY, {"id": company_id})).first()
    if company is None:
        raise LookupError(f"watchlist company {company_id} does not exist")
    board = InstanceRow(
        id=_NO_INSTANCE,
        source_id=company.ats_type,
        instance_key=(
            f"{company.lever_region}:{company.board_slug}"
            if company.ats_type == "lever"
            else company.board_slug
        ),
        watchlist_company_id=company_id,
        health="ok",
        half_open_after=None,
        board_slug=company.board_slug,
        lever_region=company.lever_region,
        display_name=company.display_name,
    )
    adapter = ats.adapter_for(board, client, clock=utcnow)
    outcome = await adapter.fetch(adapter.first_spec(cell_id=_NO_CELL, window=None, request_class="free"))
    status = _classify(adapter, outcome)
    if guard is not None:
        await guard(conn)
    await conn.execute(_RECORD, {"id": company_id, "status": status, "now": now})
    if status == "ok":
        await instance_for_company(conn, company_id)
    _log.info("watchlist_validated", watchlist_company_id=company_id, ats=company.ats_type, status=status)
    return status


@register_operation_handler("watchlist_validate")
async def run_watchlist_validate(ctx: OperationContext) -> None:
    """The `watchlist_validate` operation: `validate_company` under the operation's owner lock."""
    company_id = get_int(dict(ctx.claim.payload), "watchlist_company_id")
    async with ctx.engine.connect() as conn:
        ats_type = (await conn.execute(_COMPANY, {"id": company_id})).scalar_one_or_none()
    if ats_type is None:
        _log.info(
            "watchlist_company_gone", watchlist_company_id=company_id, operation_id=ctx.claim.operation_id
        )
        return

    async def owner(conn: AsyncConnection) -> None:
        await lock_operation_owner(conn, ctx.claim)

    async with http.make_client(ats_type) as client, ctx.engine.begin() as conn:
        try:
            await validate_company(conn, company_id, client=client, now=utcnow(), guard=owner)
        except LookupError:  # removed while its board answered: nothing is left to validate
            _log.info(
                "watchlist_company_gone", watchlist_company_id=company_id, operation_id=ctx.claim.operation_id
            )
