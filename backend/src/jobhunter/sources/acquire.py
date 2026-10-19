"""The one-page acquisition protocol (spec §3.3.3, §5.2, §6.2, §7.16.5; plan Task 12).

`acquire_page` turns one request spec into one `PageResult`, through durable receipts only, so that no
crash, restart or takeover ever sends a paid request twice or loses a paid response:

1. **Resolve the latest attempt** of the request key (§6.2 rule 2, on `receipts.fetch_attempts` and
   `ledger.latest_attempt`): no row, or a `planned` one, is attempted; a `succeeded` row is **replayed** from
   its stored response, without HTTP (`Served(replayed=True)`, logged `page_replayed`); a `failed` row keeps
   its receipt, and only the §5.2 retry rule earns a new attempt (below), otherwise it is returned as
   `Failed(replayed=True)` with no send, no ledger entry and no breaker write; a `reserved` row of a
   superseded owner is released (`ledger.release_never_dispatched`) and `skipped('superseded')`, and
   `attempt_seq + 1` is attempted (an own reservation, left by an earlier call of this attempt, is continued);
   a paid `dispatched`/`ambiguous` row is `Ambiguous` (`ledger.mark_ambiguous`) and never re-dispatched,
   while a free or Adzuna one is closed `ambiguous` and re-dispatched as `attempt_seq + 1`; a `skipped` row is
   its denial, re-attempted only after `shutdown` or `superseded` (§7.16.5). Attempts stop at
   `ledger.MAX_ATTEMPT_SEQ`.
2. **Reserve under the window lock**: `admission_stopped()` first (`skipped('shutdown')`); for SerpApi
   (`AccountAdapter`) the free account read, outside any transaction, whose failure is `Denied('window_cap')`
   with nothing sent (§5.3.2 fails closed); then `ledger.reserve` (the account read applied to the window,
   `ledger.observe_account`, before `decide`), the request `planned -> reserved`, or `skipped(<code>)`.
3. **Commit the dispatch marker**: `admission_stopped()` again (a stop releases the reservation,
   `never_dispatched`, and skips the request `shutdown`); then `ledger.mark_dispatched` and
   `reserved -> dispatched` in one transaction (a free request goes `planned -> dispatched`).
4. **Send once**: the throttle (the caller's rate limiter) and `adapter.fetch`, outside any transaction.
5. **Persist and settle in one transaction**: the response classified by its **HTTP status first**
   (`check_envelope`, `peek_native_ids` and `continuation` run on a 2xx page only, and every `SourceError`
   they raise is an outcome, never an exception), then the stored response (`receipts.store_response`), the
   request `succeeded | failed(error_code) | ambiguous`, the ledger settled by the transport outcome and the
   status alone (`adapter.settle`: a JSearch HTML 200 is `billed` and `failed('contract')` at once, Deviation
   23) with the response's observation, and the breaker (`failure_kind`; a timeout counts as `transient` for
   the breaker while its ledger entry stays `ambiguous`).

Fault points `collect.after_reserve`, `collect.after_dispatch_marker`, `collect.after_response` and
`collect.after_settle` mark the crash windows (§18.3 C1-C4). Every transaction that writes a receipt first
holds the run row `FOR SHARE` (`ledger.lock_run_owner`), so a superseded owner is `FencedOut` before it writes
or sends; the lock order inside is owner -> `quota_windows` -> ledger row -> `fetch_requests`/`raw_responses`
-> `source_instances`. `acquire_page` never raises `SourceError`: it raises `FencedOut` (the owner lost the
run, or its receipt was recovered by another) and lets bugs propagate.

**Retries (§5.2).** Keyed on the response status, never on the error kind: a 2xx is never retried (a 200
carrying the provider's own error is billed). A paid or quota source earns at most one `retry`-class attempt
per request, only after a 429 or 5xx *response*. A free source makes at most `FREE_MAX_ATTEMPTS` attempts per
request and run attempt, after a connect error, a 429 or a 5xx, waiting 1 s then 4 s (jittered by 25 %) or
the `Retry-After` the response asks for, at most 60 s, through `ctx.sleep`. A timeout is never retried in the
call (it is ambiguous); a later run attempt re-dispatches a free one.
"""

import random
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass, replace
from datetime import datetime
from email.utils import parsedate_to_datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal, Protocol, cast, get_args, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jobhunter.db.transitions import transition
from jobhunter.errors import FencedOut, InvariantViolation, TransitionConflict
from jobhunter.faults import afault_point
from jobhunter.observability.logging import get_logger
from jobhunter.quota import ledger
from jobhunter.quota.pacing import QuotaClass
from jobhunter.quota.windows import AccountObservation, Provider
from jobhunter.run_settings import RunSettings
from jobhunter.sources import breaker
from jobhunter.sources.base import (
    Billing,
    Continuation,
    RawPage,
    RequestClass,
    RequestSpec,
    SourceAdapter,
    SourceError,
    SourceErrorKind,
    request_key,
)
from jobhunter.sources.breaker import FailureKind
from jobhunter.sources.http import HttpOutcome
from jobhunter.sources.receipts import (
    FetchAttempt,
    fetch_attempts,
    insert_fetch_request,
    load_response,
    store_response,
)

if TYPE_CHECKING:
    # `worker.runs` imports `observability.health`, which imports the quota ledger: the owner type only.
    from jobhunter.worker.runs import RunOwner

FREE_MAX_ATTEMPTS: Final = 3
FREE_BACKOFF_S: Final = (1.0, 4.0)  # before the second and the third attempt
JITTER: Final = 0.25
RETRY_AFTER_MAX_S: Final = 60.0
# The error code of a request found `dispatched` without a settlement (as the reconciler `ledger_unsettled`).
UNSETTLED: Final = "unsettled"
# The skips after which the request is attempted again (§7.16.5); every other skip is the request's denial.
_REATTEMPTED: Final = frozenset({"shutdown", "superseded"})
_SENT: Final = frozenset({"dispatched", "succeeded", "failed", "ambiguous"})
_FAILURE_KINDS: Final[Mapping[str, FailureKind]] = MappingProxyType({k: k for k in get_args(FailureKind)})
_RANDOM: Final = random.SystemRandom()

log = get_logger("jobhunter.sources.acquire")


@runtime_checkable
class AccountAdapter(Protocol):
    """An adapter with a free account read made before every search (SerpApi, §5.3.2)."""

    async def account(self) -> AccountObservation: ...


@dataclass
class AcquireContext:
    """What one owner's acquisitions from one adapter share. `provider` is the quota provider of the
    source (`None` for a free one); `settings` gives the quota settings and the schedule's interval."""

    engine: AsyncEngine
    owner: "RunOwner"
    adapter: SourceAdapter
    billing: Billing
    provider: Provider | None
    settings: RunSettings
    clock: Callable[[], datetime]
    sleep: Callable[[float], Awaitable[None]]
    admission_stopped: Callable[[], bool]

    def __post_init__(self) -> None:
        if (self.billing == "free") != (self.provider is None):
            raise ValueError("a free source has no quota provider, and every other source has one")


@dataclass(frozen=True)
class Served:
    fetch_request_id: int
    page: RawPage
    replayed: bool
    new_native_ids: int | None


@dataclass(frozen=True)
class Denied:
    fetch_request_id: int
    skip_reason: str


@dataclass(frozen=True)
class Failed:
    fetch_request_id: int
    error_code: str
    http_status: int | None
    replayed: bool


@dataclass(frozen=True)
class Ambiguous:
    fetch_request_id: int


PageResult = Served | Denied | Failed | Ambiguous


def error_code(err: SourceError) -> str:
    """The `fetch_requests.error_code` of a failed attempt: the error's kind (`transient`, `rate_limited`,
    `auth`, `quota_exhausted`, `contract`, `not_found`, `client`, `ambiguous`)."""
    return err.kind


def failure_kind(err: SourceError) -> FailureKind | None:
    """The breaker kind of an error: the six `FailureKind`s are themselves; `ambiguous` and
    `quota_exhausted` are `None`, no breaker write (the protocol still counts an ambiguous attempt as
    `transient`, `_breaker_kind`)."""
    return _FAILURE_KINDS.get(err.kind)


def _breaker_kind(err: SourceError) -> FailureKind | None:
    """What the breaker records for a failed attempt: a lost or timed-out one is `transient` (§17.1), so a
    host that always times out opens its breaker; the ledger keeps it `ambiguous`."""
    return "transient" if err.kind == "ambiguous" else failure_kind(err)


def status_error(status: int, billing: Billing) -> SourceError:
    """The error of a non-2xx response, by its status alone (§17.1): 429 `rate_limited`, 401/403 `auth`,
    5xx `transient`, a 404 `not_found` for a free source (a board or feed that is gone) and `client`
    otherwise, any other status `client`."""
    kind: SourceErrorKind
    if status == 429:
        kind = "rate_limited"
    elif status in (401, 403):
        kind = "auth"
    elif status >= 500:
        kind = "transient"
    elif status == 404 and billing == "free":
        kind = "not_found"
    else:
        kind = "client"
    return SourceError(kind, code=f"http_{status}", status=status)


@dataclass(frozen=True)
class _Envelope:
    """A 2xx page whose envelope holds: its native ids and its continuation."""

    page: RawPage
    ids: list[str | None]
    continuation: Continuation
    cursor: str | None


def _classify(adapter: SourceAdapter, outcome: HttpOutcome, billing: Billing) -> _Envelope | SourceError:
    """Status first: the envelope is judged only on a 2xx page (a 429, 404 or 401 body is no contract)."""
    page = outcome.page
    if page is None:
        return outcome.error or SourceError("ambiguous", code="no_response")
    if not 200 <= page.status < 300:
        return status_error(page.status, billing)
    if outcome.error is not None:  # a 2xx body over the cap, or undecodable: not the contract
        return outcome.error
    try:
        adapter.check_envelope(page)
        ids = adapter.peek_native_ids(page)
        continuation, cursor = adapter.continuation(page)
    except SourceError as err:  # a broken envelope, or the provider's own error report delivered with a 2xx
        return err
    return _Envelope(page, ids, continuation, cursor)


def _retry_after(page: RawPage | None) -> float:
    """The wait a response asks for (`Retry-After`, seconds or an HTTP date), at most 60 s; 0 without one."""
    if page is None or not (value := page.headers.get("retry-after", "").strip()):
        return 0.0
    if value.isascii() and value.isdigit():
        return min(float(value), RETRY_AFTER_MAX_S)
    try:
        delay = (parsedate_to_datetime(value) - page.fetched_at).total_seconds()
    except (TypeError, ValueError):
        return 0.0
    return min(max(delay, 0.0), RETRY_AFTER_MAX_S)


def _retryable(last: FetchAttempt, billing: Billing) -> bool:
    """§5.2, keyed on the response status: a 429 or a 5xx; a free source also after a connect error (a failed
    attempt without a response)."""
    if last.http_status is not None:
        return last.http_status == 429 or last.http_status >= 500
    return billing == "free" and last.error_code == "transient"


@dataclass(frozen=True)
class _Attempt:
    """The attempt to make: an existing row to use (`row_id`, or a new one), and an own reservation of it
    to continue from (`entry_id`)."""

    attempt_seq: int
    request_class: RequestClass
    row_id: int | None = None
    entry_id: int | None = None
    delay_s: float = 0.0


class _Again:
    """The receipts changed under the resolution (a recovery moved them): resolve once more."""


_AGAIN: Final = _Again()


def _following(last: FetchAttempt, request_class: RequestClass, delay_s: float = 0.0) -> _Attempt | None:
    if last.attempt_seq >= ledger.MAX_ATTEMPT_SEQ:
        return None
    return _Attempt(last.attempt_seq + 1, request_class, delay_s=delay_s)


async def _move(
    conn: AsyncConnection,
    fetch_request_id: int,
    from_states: list[str],
    to_state: str,
    set_: Mapping[str, object] | None = None,
) -> None:
    """A transition of the owner's own receipt; a receipt moved by someone else (a newer owner, the
    reconciler) means the owner has lost it: `FencedOut`."""
    try:
        await transition(conn, "fetch_requests", fetch_request_id, from_states, to_state, set_=set_)
    except TransitionConflict as exc:
        raise FencedOut("fetch_requests") from exc


class _Acquisition:
    """One `acquire_page` call: the request, its key, and what this call has settled itself."""

    def __init__(
        self,
        ctx: AcquireContext,
        spec: RequestSpec,
        *,
        phase: Literal["primary", "fallback"],
        known_native_ids: Callable[[Sequence[str | None]], Awaitable[int]] | None,
        throttle: Callable[[], AbstractAsyncContextManager[object]] | None,
    ) -> None:
        self.ctx = ctx
        self.spec = spec
        self.phase = phase
        self.key = request_key(spec, ctx.owner.run_id)
        self.known_native_ids = known_native_ids
        self.throttle = throttle
        self.settled_here: set[int] = set()
        self.last_page: RawPage | None = None  # the response of this call's latest attempt

    # -- step 1: resolve the latest attempt
    async def resolve(self, history: Sequence[FetchAttempt]) -> PageResult | _Attempt | _Again:
        if not history:
            return _Attempt(1, self.spec.request_class)
        last = history[-1]
        here = last.id in self.settled_here
        if last.status == "planned":
            return _Attempt(last.attempt_seq, cast(RequestClass, last.request_class), row_id=last.id)
        if last.status == "succeeded":
            return await self._replay(last)
        if last.status == "skipped":
            reason = last.skip_reason or ""
            if reason in _REATTEMPTED:
                return _following(last, cast(RequestClass, last.request_class)) or Denied(last.id, reason)
            return Denied(last.id, reason)
        if last.status == "failed":
            nxt = self._retry(history, last)
            if nxt is not None:
                log.info(
                    "page_retry",
                    fetch_request_id=last.id,
                    attempt_seq=nxt.attempt_seq,
                    http_status=last.http_status,
                    delay_s=nxt.delay_s,
                )
                return nxt
            return Failed(last.id, last.error_code or "", last.http_status, replayed=not here)
        if last.status == "reserved":
            return await self._reserved(last)
        # dispatched or ambiguous: no response was stored (persistence and settlement commit together)
        if last.status == "dispatched" and not await self._close_ambiguous(last):
            return _AGAIN
        if self.ctx.billing == "paid" or here:
            log.warning("page_ambiguous", fetch_request_id=last.id, attempt_seq=last.attempt_seq)
            return Ambiguous(last.id)
        return _following(last, cast(RequestClass, last.request_class)) or Ambiguous(last.id)

    async def _replay(self, last: FetchAttempt) -> Served:
        if last.raw_response_id is None:
            raise InvariantViolation(f"fetch request {last.id} succeeded without a stored response")
        spec = replace(self.spec, request_class=cast(RequestClass, last.request_class))
        async with self.ctx.engine.connect() as conn:
            page = await load_response(conn, last.raw_response_id, spec)
        replayed = last.id not in self.settled_here
        if replayed:
            log.info("page_replayed", fetch_request_id=last.id, attempt_seq=last.attempt_seq)
        return Served(last.id, page, replayed=replayed, new_native_ids=last.new_native_ids)

    def _retry(self, history: Sequence[FetchAttempt], last: FetchAttempt) -> _Attempt | None:
        """The §5.2 retry of a failed attempt, or `None`."""
        if not _retryable(last, self.ctx.billing):
            return None
        if self.ctx.billing != "free":
            if any(a.request_class == "retry" and a.status in _SENT for a in history):
                return None  # at most one retry per spec
            return _following(last, "retry")
        owner = self.ctx.owner
        sends = sum(1 for a in history if a.status in _SENT and a.run_attempt_no == owner.attempt_no)
        if sends >= FREE_MAX_ATTEMPTS:
            return None
        delay = 0.0
        if sends > 0:  # a wait between this run attempt's own attempts
            backoff = FREE_BACKOFF_S[min(sends, len(FREE_BACKOFF_S)) - 1]
            jittered = backoff * _RANDOM.uniform(1 - JITTER, 1 + JITTER)
            asked = _retry_after(self.last_page) if last.id in self.settled_here else 0.0
            delay = max(jittered, asked)
        return _following(last, "free", delay)

    async def _reserved(self, last: FetchAttempt) -> _Attempt | PageResult | _Again:
        """A reservation without a dispatch marker: continued when it is the owner's own, else released."""
        ctx = self.ctx
        if ctx.provider is None or last.quota_ledger_id is None:
            raise InvariantViolation(f"fetch request {last.id} is reserved without a ledger entry")
        async with ctx.engine.connect() as conn:
            entry = await ledger.latest_attempt(conn, ctx.provider, self.key)
        if entry is None or entry.entry_id != last.quota_ledger_id:
            raise InvariantViolation(f"fetch request {last.id} and its ledger entry disagree")
        if (entry.run_id, entry.run_attempt_no) == (ctx.owner.run_id, ctx.owner.attempt_no):
            # an earlier call of this attempt reserved it and failed before its marker: nothing was sent
            return _Attempt(
                last.attempt_seq,
                cast(RequestClass, last.request_class),
                row_id=last.id,
                entry_id=entry.entry_id,
            )
        async with ctx.engine.begin() as conn:
            await ledger.lock_run_owner(conn, ctx.owner)  # the stale owner's marker now finds no predicate
            released = await ledger.release_never_dispatched(conn, entry.entry_id, now=ctx.clock())
            if released:
                await _move(conn, last.id, ["reserved"], "skipped", {"skip_reason": "superseded"})
        if not released:
            return _AGAIN  # recovered meanwhile (the reconciler moves both together)
        log.info("reservation_released", fetch_request_id=last.id, attempt_seq=last.attempt_seq)
        return _following(last, cast(RequestClass, last.request_class)) or Denied(last.id, "superseded")

    async def _close_ambiguous(self, last: FetchAttempt) -> bool:
        """`dispatched -> ambiguous` for the request and its ledger entry (which stays charged); `False` when
        the entry was recovered meanwhile."""
        ctx = self.ctx
        async with ctx.engine.begin() as conn:
            await ledger.lock_run_owner(conn, ctx.owner)
            if last.quota_ledger_id is not None:
                if not await ledger.mark_ambiguous(conn, last.quota_ledger_id, now=ctx.clock()):
                    return False
            await _move(conn, last.id, ["dispatched"], "ambiguous", {"error_code": UNSETTLED})
        return True

    # -- steps 2 to 5: one attempt
    async def attempt(self, nxt: _Attempt, cell_id: int) -> PageResult | int:
        """Make the attempt; its result, or the id of its `failed` receipt (the retry rule decides next)."""
        ctx, owner = self.ctx, self.ctx.owner
        spec = replace(self.spec, request_class=nxt.request_class)
        if nxt.delay_s > 0:
            await ctx.sleep(nxt.delay_s)
        row, entry = nxt.row_id, nxt.entry_id
        if entry is None:
            if ctx.admission_stopped():
                return await self._skip(row, spec, nxt, cell_id, "shutdown")
            account = None
            if isinstance(ctx.adapter, AccountAdapter):
                try:
                    account = await ctx.adapter.account()
                except SourceError as err:  # §5.3.2: no fresh account read, no search
                    log.warning("account_read_failed", error_kind=err.kind, error_code=err.code)
                    return await self._skip(row, spec, nxt, cell_id, "window_cap")
            if row is None:
                async with ctx.engine.begin() as conn:
                    await ledger.lock_run_owner(conn, owner)
                    row = await self._insert(conn, spec, nxt, cell_id, "planned")
            if ctx.provider is not None:
                reserved = await self._reserve(ctx.provider, row, spec, nxt, account)
                if isinstance(reserved, Denied):
                    return reserved
                entry = reserved
        elif row is None:
            raise ValueError("an own reservation is continued on its own receipt")
        await afault_point("collect.after_reserve")
        if ctx.admission_stopped():
            async with ctx.engine.begin() as conn:
                await ledger.lock_run_owner(conn, owner)
                if entry is not None:
                    await ledger.release_never_dispatched(conn, entry, now=ctx.clock())
                await _move(
                    conn,
                    row,
                    ["reserved" if entry is not None else "planned"],
                    "skipped",
                    {"skip_reason": "shutdown"},
                )
            return Denied(row, "shutdown")
        async with ctx.engine.begin() as conn:
            if entry is not None:
                await ledger.mark_dispatched(conn, entry, owner)  # FencedOut: abort without sending
                await _move(conn, row, ["reserved"], "dispatched")
            else:
                await ledger.lock_run_owner(conn, owner)
                await _move(conn, row, ["planned"], "dispatched")
        await afault_point("collect.after_dispatch_marker")
        async with self.throttle() if self.throttle is not None else nullcontext():
            outcome = await ctx.adapter.fetch(spec)
        await afault_point("collect.after_response")
        verdict = _classify(ctx.adapter, outcome, ctx.billing)
        new_ids = None
        if isinstance(verdict, _Envelope) and self.known_native_ids is not None:
            distinct = {native_id for native_id in verdict.ids if native_id is not None}
            new_ids = max(0, len(distinct) - await self.known_native_ids(verdict.ids))
        result = await self._settle(row, entry, spec, outcome, verdict, new_ids)
        await afault_point("collect.after_settle")
        self.settled_here.add(row)
        self.last_page = outcome.page
        return result

    async def _insert(
        self,
        conn: AsyncConnection,
        spec: RequestSpec,
        nxt: _Attempt,
        cell_id: int,
        status: Literal["planned", "skipped"],
        skip_reason: str | None = None,
    ) -> int:
        return await insert_fetch_request(
            conn,
            owner=self.ctx.owner,
            cell_id=cell_id,
            source_instance_id=spec.source_instance_id,
            phase=self.phase,
            spec=spec,
            request_key=self.key,
            attempt_seq=nxt.attempt_seq,
            status=status,
            skip_reason=skip_reason,
        )

    async def _skip(
        self, row: int | None, spec: RequestSpec, nxt: _Attempt, cell_id: int, reason: str
    ) -> Denied:
        """The attempt ends before any reservation: its receipt `skipped(reason)`."""
        async with self.ctx.engine.begin() as conn:
            await ledger.lock_run_owner(conn, self.ctx.owner)
            if row is None:
                row = await self._insert(conn, spec, nxt, cell_id, "skipped", reason)
            else:
                await _move(conn, row, ["planned"], "skipped", {"skip_reason": reason})
        return Denied(row, reason)

    async def _reserve(
        self,
        provider: Provider,
        row: int,
        spec: RequestSpec,
        nxt: _Attempt,
        account: AccountObservation | None,
    ) -> int | Denied:
        """`ledger.reserve` and the request `planned -> reserved`, or its denial `skipped(<code>)`."""
        ctx = self.ctx
        if spec.request_class not in get_args(QuotaClass):
            raise ValueError(f"a {provider} request has a quota class, not {spec.request_class!r}")
        async with ctx.engine.begin() as conn:
            decision = await ledger.reserve(
                conn,
                provider,
                owner=ctx.owner,
                request_key=self.key,
                attempt_seq=nxt.attempt_seq,
                request_class=cast(QuotaClass, spec.request_class),
                fetch_request_id=row,
                settings=ctx.settings.quota,
                interval_s=ctx.settings.interval_s,
                now=ctx.clock(),
                account=account,
            )
            if isinstance(decision, ledger.Denial):
                await _move(conn, row, ["planned"], "skipped", {"skip_reason": decision.code})
                return Denied(row, decision.code)
            await _move(conn, row, ["planned"], "reserved", {"quota_ledger_id": decision.entry_id})
        return decision.entry_id

    async def _settle(
        self,
        row: int,
        entry: int | None,
        spec: RequestSpec,
        outcome: HttpOutcome,
        verdict: _Envelope | SourceError,
        new_ids: int | None,
    ) -> PageResult | int:
        """Step 5: the response, the request, the ledger and the breaker in one transaction."""
        ctx, page = self.ctx, outcome.page
        now = ctx.clock()
        status = None if page is None else page.status
        envelope = verdict if isinstance(verdict, _Envelope) else None
        async with ctx.engine.begin() as conn:
            await ledger.lock_run_owner(conn, ctx.owner)
            if entry is not None:  # billed by the transport outcome and the status alone, never by the body
                state, evidence = ctx.adapter.settle(outcome)
                observation = None if page is None else ctx.adapter.observation(page)
                await ledger.settle(
                    conn,
                    entry,
                    state=state,
                    evidence=evidence,
                    http_status=status,
                    provider_remaining=None if observation is None else observation.remaining,
                    observation=observation,
                    settings=ctx.settings.quota,
                    interval_s=ctx.settings.interval_s,
                    now=now,
                )
            if page is not None:
                await store_response(
                    conn,
                    row,
                    page,
                    source_instance_id=spec.source_instance_id,
                    ledger_entry_id=entry,
                    succeeded=envelope is not None,
                    items=None if envelope is None else len(envelope.ids),
                )
            if envelope is not None:
                done = {
                    "http_status": status,
                    "items": len(envelope.ids),
                    "continuation": envelope.continuation.value,
                    "next_cursor": None if envelope.cursor is None else {"cursor": envelope.cursor},
                    "new_native_ids": new_ids,
                }
                await _move(conn, row, ["dispatched"], "succeeded", done)
                await breaker.record_success(conn, spec.source_instance_id, now=now)
            else:
                err = cast(SourceError, verdict)
                to_state = "ambiguous" if err.kind == "ambiguous" else "failed"
                failed = {"http_status": status, "error_code": error_code(err)}
                await _move(conn, row, ["dispatched"], to_state, failed)
                if (kind := _breaker_kind(err)) is not None:
                    await breaker.record_failure(
                        conn, spec.source_instance_id, kind, billing=ctx.billing, now=now
                    )
        if envelope is not None:
            return Served(row, envelope.page, replayed=False, new_native_ids=new_ids)
        if isinstance(verdict, SourceError) and verdict.kind == "ambiguous":
            log.warning("page_ambiguous", fetch_request_id=row, error_code=verdict.code)
            return Ambiguous(row)
        return row


async def acquire_page(
    ctx: AcquireContext,
    spec: RequestSpec,
    *,
    cell_id: int,
    phase: Literal["primary", "fallback"],
    planned_id: int | None,
    known_native_ids: Callable[[Sequence[str | None]], Awaitable[int]] | None = None,
    throttle: Callable[[], AbstractAsyncContextManager[object]] | None = None,
) -> PageResult:
    """Acquire one page of `spec` for `ctx.owner` (see the module docstring).

    `planned_id` is the receipt the planner made for this request, if any (it must be one of the key's
    attempts); the latest attempt of the key decides either way. `known_native_ids` counts how many distinct
    ids of a served page are known postings already; `new_native_ids` is the page's distinct ids minus that
    count, persisted with the receipt. `throttle` is entered around each send (the caller's rate limit).
    """
    if spec.cell_id != cell_id:
        raise ValueError(f"the spec is for cell {spec.cell_id}, not {cell_id}")
    acquisition = _Acquisition(ctx, spec, phase=phase, known_native_ids=known_native_ids, throttle=throttle)
    # every round consumes an attempt (at most `MAX_ATTEMPT_SEQ`) or re-reads receipts a recovery changed
    for _ in range(4 * ledger.MAX_ATTEMPT_SEQ):
        async with ctx.engine.connect() as conn:
            history = await fetch_attempts(conn, acquisition.key)
        if planned_id is not None and planned_id not in {a.id for a in history}:
            raise ValueError(f"fetch request {planned_id} is no attempt of this request")
        step = await acquisition.resolve(history)
        if isinstance(step, _Again):
            continue
        if not isinstance(step, _Attempt):
            return step
        result = await acquisition.attempt(step, cell_id)
        if not isinstance(result, int):
            return result
    raise RuntimeError(f"no outcome for request key {acquisition.key.hex()} after its attempts")
