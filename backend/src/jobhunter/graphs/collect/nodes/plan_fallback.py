"""Node 3, `plan_fallback` (spec §6.6, §9.2): the SerpApi specs of the JSearch cells the run did not serve.

It runs once every primary spec is terminal. It reads the run's settings version and `include_paid` from the
run row and, in a run that includes paid sources, makes the free SerpApi account read for the projection
(`serpapi_account`, outside any transaction and within `ACCOUNT_READ_TIMEOUT_S`; the acquisition reads the
account again before every search), unless the instance's breaker blocks it (`breaker.available`: the read
is a send to SerpApi as well, so an `auth_failed` or open instance gets none before its half-open time;
final review F3). A read that expires or fails is no read: the projection then fails
closed by its own rules (`window_cap` without an account window). A read that fails as `auth` (a missing key,
refused locally, or a 401/403) is counted by the instance's breaker as well, which opens `auth_failed` with
`source.auth_failed` at once (Ruling FR-l: a missing credential is visible).

A read is an observation of the SerpApi window (§5.3.2: "read immediately before every SerpApi search"), and
while JSearch serves every cell it is the only SerpApi read there is: it is persisted under the run's owner
row in a transaction of its own (`ledger.record_account`: the window lock, the stale-renewal rule of T7-a,
the foreign-usage detection and the T7-b renewal check), exactly as the acquisition applies its read before a
search, so `quota.serpapi_renewal_stale` opens in a month that the lagging renewal locks out and
`quota.serpapi_foreign_usage` resolves without a search (final review F3). The source instances are then
read again, so the planner sees a breaker the failed read opened. Then `planner.plan_fallback` in one
transaction, whose new `skipped` receipts are counted in `FETCH_REQUESTS` after it commits. `route` sends the
graph to `fetch_fallback` when it planned specs, else to `parse_normalize`.
"""

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Final, Literal

from jobhunter.db.sql import q
from jobhunter.graphs.collect.deps import CollectDeps
from jobhunter.graphs.collect.nodes import Node
from jobhunter.graphs.collect.nodes.plan import count_skips_written
from jobhunter.graphs.collect.state import CollectState, CollectUpdate
from jobhunter.observability.logging import get_logger
from jobhunter.quota import ledger, planner
from jobhunter.quota.windows import AccountObservation
from jobhunter.run_settings import RunSettings, load_run_settings
from jobhunter.sources.acquire import AccountAdapter, failure_kind, record_account_auth_failure
from jobhunter.sources.base import SourceAdapter, SourceError
from jobhunter.sources.breaker import available
from jobhunter.sources.instances import InstanceRow, list_instances
from jobhunter.worker.runs import RunOwner

# How long the projection's account read may take (the node's own timeout is 30 s, §9.2).
ACCOUNT_READ_TIMEOUT_S: Final = 10.0

_RUN = q("SELECT settings_version_id, include_paid FROM runs WHERE id = :i")

log = get_logger("jobhunter.graphs.collect.plan_fallback")


async def serpapi_account(
    adapters: Callable[[InstanceRow], SourceAdapter],
    settings: RunSettings,
    instances: Sequence[InstanceRow],
    *,
    now: datetime,
    timeout_s: float = ACCOUNT_READ_TIMEOUT_S,
    on_auth_failure: Callable[[int], Awaitable[None]] | None = None,
) -> AccountObservation | None:
    """The free SerpApi account read for the fallback projection (§5.3.2), or `None`: SerpApi disabled (by
    settings or health) or blocked by its breaker at `now` (no read is sent), an adapter without the read,
    or a read that failed (`SourceError`: `http.send` folds every transport error into one) or did not end
    within `timeout_s`. A read that failed as `auth` is handed to `on_auth_failure` (the instance id) first.
    Anything else (a bug, a fenced owner, a database error, a cancellation) propagates."""
    instance = next((i for i in instances if i.source_id == "serpapi"), None)
    if not settings.sources.serpapi or instance is None or available(instance, now) == "blocked":
        return None  # `blocked` covers `disabled`
    adapter = adapters(instance)
    if not isinstance(adapter, AccountAdapter):
        return None
    try:
        async with asyncio.timeout(timeout_s):
            return await adapter.account()
    except TimeoutError:
        log.warning("account_read_failed", error_kind="timeout", error_code="account_read_timeout")
    except SourceError as err:
        log.warning("account_read_failed", error_kind=err.kind, error_code=err.code)
        if on_auth_failure is not None and failure_kind(err) == "auth":
            await on_auth_failure(instance.id)
    return None


def make_plan_fallback(deps: CollectDeps) -> Node:
    async def plan_fallback(state: CollectState) -> CollectUpdate:
        owner = RunOwner(state["run_id"], state["attempt_no"])
        async with deps.engine.connect() as conn:
            run = (await conn.execute(_RUN, {"i": owner.run_id})).one()
            settings = await load_run_settings(conn, int(run.settings_version_id))
            instances = await list_instances(conn)

        async def auth_failed(instance_id: int) -> None:
            await record_account_auth_failure(
                deps.engine, owner, instance_id, billing="paid", now=deps.clock()
            )

        account: AccountObservation | None = None
        if run.include_paid:
            account = await serpapi_account(
                deps.adapters, settings, instances, now=deps.clock(), on_auth_failure=auth_failed
            )
            if account is not None:  # an observation of the window, persisted (Ruling T7-b)
                async with deps.engine.begin() as conn:
                    await ledger.lock_run_owner(conn, owner)
                    await ledger.record_account(conn, account, settings=settings, now=deps.clock())
            async with deps.engine.connect() as conn:  # after the read: a breaker it opened is seen
                instances = await list_instances(conn)
        written: Counter[str] = Counter()
        async with deps.engine.begin() as conn:
            ids = await planner.plan_fallback(
                conn,
                owner=owner,
                settings=settings,
                instances=instances,
                now=deps.clock(),
                account=account,
                skips_written=written,
            )
        count_skips_written(written)  # once, after the commit: a reused fallback plan wrote none
        return {"fallback_fetch_request_ids": ids, "steps": ["plan_fallback"]}

    return plan_fallback


def route(state: CollectState) -> Literal["fetch_fallback", "parse_normalize"]:
    """`fetch_fallback` when the fallback planned specs, else `parse_normalize` (§9.2 edges)."""
    return "fetch_fallback" if state["fallback_fetch_request_ids"] else "parse_normalize"
