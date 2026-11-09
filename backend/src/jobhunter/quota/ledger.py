"""The quota ledger (spec §6.1-6.7, §7.8, §7.16.7, §17.4): one row per attempt to a quota provider, reserved
before the attempt and counted until positive evidence says it was not charged.

**Reservation** (`reserve`, §6.7 in its order): the run row `FOR SHARE` with its ownership predicate
(`lock_run_owner`, §3.3.3), the provider's current window `FOR UPDATE`, rolled forward or bootstrapped
(`windows.current_window_locked`: the window row is the quota lock), a SerpApi account read applied to it
(`observe_account`), the bucket accrued, `decide` with the hard caps of §6.5 (`serpapi_hour_ok`,
`adzuna_windows_ok`), and then either the `reserved` row and the saved window, or (a denial) the saved accrual
and nothing else. Global lock order: owner row -> `quota_windows` row -> ledger rows -> other domain rows.
`record_account` applies a SerpApi account read made outside a reservation (the fallback projection's read in
`plan_fallback`, and the read after a traversal's last search) with the same code under the same lock, so
the foreign-usage and renewal alerts see the reads of a month without a SerpApi search.

**The dispatch marker** (`mark_dispatched`) commits `reserved -> dispatched` immediately before the send,
under the owner's `FOR SHARE` again; a lost predicate or a row that is no longer `reserved` is `FencedOut`,
and the caller must not send. **Settlement** (`settle`) moves `dispatched` to `billed`, `not_billed` (with
refund evidence only: `connect_failed_before_send`, or for JSearch `rapidapi_gateway_404`) or `ambiguous`, and
reconciles the response's observation on the locked current window (§6.3, `windows.apply_observation_pure`).

**Recovery** has two writes, each under the lock of the entry's own window, and every recovery path uses them
(the replay branch of the acquisition, the shutdown path, the reconciler `ledger_unsettled`):
`release_never_dispatched` (`reserved -> not_billed('never_dispatched')`) and `mark_ambiguous`
(`dispatched -> ambiguous`). Both restore the window's probe columns (`windows.note_probe_outcome`), so a
bootstrap probe is never left "in flight". Nothing is refunded without evidence, and every timeout is
`ambiguous` (§6.1).

**Planning** (`project_allowance`) runs the same pure functions on an unlocked snapshot and writes nothing.

The pure cores (`admission`, `settle_window`, `serpapi_hour_admits`, `note_foreign_usage`, `adzuna_admits`)
are what the property tests drive (§18.1). `quota_windows` changes only through `windows.save_window` and
`windows.apply_changes` (Ruling P11); ledger rows change state only through `transition()`.

**The item owner** (`lock_item_owner`, M4 Task 12, M1 Carry-forward 10): the owner of an identity resolution
(`dedup.identity.resolve_item`) is an `ItemOwner`, a run's admitted attempt (`lock_run_owner`) or an
operation's claim (`worker.operations.lock_operation_owner`, the replay of rejected items, whose run is
over), held at the same place of the lock order, and refused under an active `global` purge fence either way.

Alerts are raised with the subject `provider:<name>`: `quota.limit_increased` (info),
`quota.unaccounted_usage` (warn) and the `adjustment` row it explains; `quota.exhausted` (warn, JSearch 429
exhaustion, resolved by a later validated observation with `remaining > 0`); `quota.ambiguous_attempt` (warn);
`quota.serpapi_foreign_usage` (warn, conservative mode for 24 h, resolved by a read without foreign usage
after that); and `quota.serpapi_renewal_stale` (warn, Ruling T7-b: a SerpApi window still unvalidated by a
read more than 48 h after its start; resolved when a read validates it).
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Literal

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.guards import global_fence_active
from jobhunter.db.sql import q
from jobhunter.db.transitions import transition
from jobhunter.errors import FencedOut, TransitionConflict
from jobhunter.observability.alerts import raise_alert, resolve_alert
from jobhunter.observability.logging import get_logger
from jobhunter.observability.metrics import QUOTA_REMAINING_UNITS, QUOTA_USED_UNITS
from jobhunter.quota.pacing import (
    Admit,
    Deny,
    DenyCode,
    PlannedSpec,
    QuotaClass,
    accrue,
    capacity,
    decide,
    fair_share,
    per_run_cap,
)
from jobhunter.quota.windows import PROBE_RETRY as PROBE_RETRY  # re-exported: the ledger's probe retry (§6.3)
from jobhunter.quota.windows import (
    AccountObservation,
    Insert,
    ObservationEffects,
    ProbeOutcome,
    Provider,
    Window,
    WindowChange,
    account_observation,
    apply_changes,
    apply_observation_pure,
    current_snapshot,
    current_window_locked,
    note_probe_outcome,
    roll_forward,
    save_window,
    window_locked,
)
from jobhunter.run_settings import QuotaSettings, RunSettings
from jobhunter.sources.base import Observation
from jobhunter.worker.epoch import fence_epoch
from jobhunter.worker.operations import OperationClaim, lock_operation_owner
from jobhunter.worker.reconcilers import ReconcileContext, reconciler

if TYPE_CHECKING:
    # `worker.runs` imports `observability.health`, which imports this module: the owner type only.
    from jobhunter.worker.runs import RunOwner

# The owner of an identity resolution (module docstring, The item owner): lazily evaluated, so the run's type
# needs no import at run time.
type ItemOwner = RunOwner | OperationClaim

COUNTED: Final = ("reserved", "dispatched", "billed", "ambiguous")
RECOVERY_AFTER: Final = timedelta(minutes=15)
MAX_ATTEMPT_SEQ: Final = 8
# Positive evidence of no charge that a settlement may carry (§6.1); `never_dispatched` is the release's own.
GATEWAY_404_EVIDENCE: Final = "rapidapi_gateway_404"
SETTLEMENT_EVIDENCE: Final = frozenset({GATEWAY_404_EVIDENCE, "connect_failed_before_send"})
# The providers behind the RapidAPI gateway, whose own 404 is the gateway's refund evidence (§6.1).
GATEWAY_PROVIDERS: Final = frozenset({"jsearch"})

# §6.5, SerpApi's hourly cap: provider lag for the app's own searches, the freshness an account observation
# needs to admit a search (fail closed), the foreign-usage tolerance and the conservative mode.
SERPAPI_USAGE_LAG: Final = timedelta(seconds=120)
SERPAPI_OBSERVATION_FRESH: Final = timedelta(seconds=60)
HOUR: Final = timedelta(hours=1)
FOREIGN_TOLERANCE: Final = 1
CONSERVATIVE_FOR: Final = timedelta(hours=24)
CONSERVATIVE_MARGIN: Final = 5
# Ruling T7-b: a SerpApi window that reads have not validated this long after its start is an operator matter.
RENEWAL_STALE_AFTER: Final = timedelta(hours=48)
# §6.5, Adzuna's rolling windows, in the order of their caps (minute, day, week, month).
ADZUNA_SPANS: Final = (timedelta(seconds=60), timedelta(hours=24), timedelta(days=7), timedelta(days=30))

# The reconciler recovers at most this many entries per tick; the next tick takes the rest.
RECOVERY_BATCH: Final = 100
# `quota_ledger.units` is a smallint: a larger adjustment is written as several rows.
_MAX_UNITS: Final = 32_767
# `quota_ledger.per_run_cap` is an integer: a larger cap (a window refined to seconds, T8-a) never binds.
_MAX_RUN_CAP: Final = 2**31 - 1
_HTTP_OK: Final = range(200, 300)

AMBIGUOUS_ALERT: Final = "quota.ambiguous_attempt"
EXHAUSTED_ALERT: Final = "quota.exhausted"
FOREIGN_USAGE_ALERT: Final = "quota.serpapi_foreign_usage"
RENEWAL_STALE_ALERT: Final = "quota.serpapi_renewal_stale"

SettleState = Literal["billed", "not_billed", "ambiguous"]

log = get_logger("jobhunter.quota.ledger")


@dataclass(frozen=True)
class Reservation:
    entry_id: int
    request_class: QuotaClass
    window_start: datetime


@dataclass(frozen=True)
class Denial:
    code: DenyCode


@dataclass(frozen=True)
class LedgerAttempt:
    """The latest attempt of a request key (§6.2 rule 2); `dispatched`: its dispatch marker was committed."""

    entry_id: int
    attempt_seq: int
    state: str
    fetch_request_id: int | None
    run_id: int | None
    run_attempt_no: int | None
    dispatched: bool


@dataclass(frozen=True)
class _Entry:
    provider: Provider
    window_start: datetime
    request_class: QuotaClass
    units: int
    state: str
    run_id: int | None
    run_attempt_no: int | None
    fetch_request_id: int | None
    created_at: datetime
    dispatched_at: datetime | None


_OWNER = q("SELECT 1 FROM runs WHERE id = :run AND attempt_no = :attempt AND status = 'running' FOR SHARE")
_ENTRY = q(
    "SELECT provider, window_start, request_class, units, state, run_id, run_attempt_no, fetch_request_id, "
    "created_at, dispatched_at FROM quota_ledger WHERE id = :id AND kind = 'request'"
)
# The ledger rows a window's counted usage sums (§6.7): the `COUNTED` attempts and every adjustment, with the
# bind `counted` (`counted_params()`, defined below it). `observability.lists` sums the same rows for every
# provider at once.
COUNTED_USAGE_SQL: Final = "(kind = 'adjustment' OR state = ANY(:counted))"
_USED = q(
    "SELECT COALESCE(SUM(units), 0) FROM quota_ledger WHERE provider = :p AND window_start = :ws "  # noqa: S608
    f"AND {COUNTED_USAGE_SQL}"
)
_RUN_NORMAL = q(
    "SELECT COALESCE(SUM(units), 0) FROM quota_ledger WHERE provider = :p AND run_id = :run "
    "AND request_class = 'normal' AND state = ANY(:counted)"
)
# The per-run cap the run's latest counted normal entry of a provider was admitted under (I3).
_RUN_CAP = q(
    "SELECT per_run_cap FROM quota_ledger WHERE provider = :p AND run_id = :run "
    "AND request_class = 'normal' AND state = ANY(:counted) ORDER BY id DESC LIMIT 1"
)
_INSERT_REQUEST = q(
    "INSERT INTO quota_ledger (provider, window_start, kind, request_class, request_key, attempt_seq, "
    "run_id, run_attempt_no, fetch_request_id, units, state, per_run_cap, created_at) VALUES (:p, :ws, "
    "'request', :cls, :key, :seq, :run, :attempt, :fr, :units, 'reserved', :cap, :now) RETURNING id"
)
_INSERT_ADJUSTMENT = q(
    "INSERT INTO quota_ledger (provider, window_start, kind, units, evidence, created_at) "
    "VALUES (:p, :ws, 'adjustment', :units, :evidence, :now)"
)
_LATEST = q(
    "SELECT id, attempt_seq, state, fetch_request_id, run_id, run_attempt_no, dispatched_at IS NOT NULL "
    "AS dispatched FROM quota_ledger WHERE provider = :p AND request_key = :key AND kind = 'request' "
    "ORDER BY attempt_seq DESC LIMIT 1"
)
# The rolling counts read `quota_ledger_rolling (provider, created_at DESC)`.
_SERPAPI_HOUR = q(
    "SELECT COALESCE(SUM(units) FILTER (WHERE created_at > :hour_ago), 0) AS last_hour, "
    "COALESCE(SUM(units) FILTER (WHERE created_at > :since), 0) AS since_observation FROM quota_ledger "
    "WHERE provider = 'serpapi' AND kind = 'request' AND state = ANY(:counted) "
    "AND created_at > LEAST(:hour_ago, :since)"
)
_SERPAPI_BEFORE = q(
    "SELECT COALESCE(SUM(units), 0) FROM quota_ledger WHERE provider = 'serpapi' AND kind = 'request' "
    "AND state = ANY(:counted) AND created_at > :hour_before AND created_at <= :observed_at"
)
_ADZUNA_ROLLING = q(
    "SELECT COALESCE(SUM(units) FILTER (WHERE created_at > :minute), 0) AS minute, "
    "COALESCE(SUM(units) FILTER (WHERE created_at > :day), 0) AS day, "
    "COALESCE(SUM(units) FILTER (WHERE created_at > :week), 0) AS week, "
    "COALESCE(SUM(units), 0) AS month FROM quota_ledger "
    "WHERE provider = 'adzuna' AND kind = 'request' AND state = ANY(:counted) AND created_at > :month"
)
# Stale `reserved` entries whose owner no longer holds its run, and `dispatched` ones unsettled for too long
# (`quota_ledger_unsettled` serves the scan). The owner is checked again under its row lock.
_UNSETTLED = q(
    "SELECT l.id FROM quota_ledger l WHERE l.state IN ('reserved', 'dispatched') AND ("
    "(l.state = 'reserved' AND l.created_at < :cutoff AND NOT EXISTS (SELECT 1 FROM runs r "
    "WHERE r.id = l.run_id AND r.attempt_no = l.run_attempt_no AND r.status = 'running')) "
    "OR (l.state = 'dispatched' AND l.dispatched_at < :cutoff)) ORDER BY l.id LIMIT :limit"
)
_LOCK_RUN = q("SELECT attempt_no, status FROM runs WHERE id = :run FOR UPDATE")


def counted_params() -> dict[str, Any]:
    """The bind `counted` of `COUNTED_USAGE_SQL` and of every statement here that names `:counted`."""
    return {"counted": list(COUNTED)}


def _subject(provider: str) -> str:
    return f"provider:{provider}"


async def _entry(conn: AsyncConnection, entry_id: int) -> _Entry | None:
    row = (await conn.execute(_ENTRY, {"id": entry_id})).first()
    if row is None:
        return None
    return _Entry(
        provider=row.provider,
        window_start=row.window_start,
        request_class=row.request_class,
        units=int(row.units),
        state=row.state,
        run_id=row.run_id,
        run_attempt_no=row.run_attempt_no,
        fetch_request_id=row.fetch_request_id,
        created_at=row.created_at,
        dispatched_at=row.dispatched_at,
    )


def _set_gauges(w: Window, used: int) -> None:
    QUOTA_USED_UNITS.labels(provider=w.provider).set(used)
    QUOTA_REMAINING_UNITS.labels(provider=w.provider).set(max(0, w.limit_units - used))


# -- counts
async def used_units(conn: AsyncConnection, provider: Provider, window_start: datetime) -> int:
    """Counted usage of the window (§6.7): `COUNTED` attempts and every adjustment."""
    params = {"p": provider, "ws": window_start, **counted_params()}
    return int((await conn.execute(_USED, params)).scalar_one())


async def normal_count_for_run(conn: AsyncConnection, provider: Provider, run_id: int) -> int:
    """The run's counted normal-class units of `provider`, across all its attempts (per-run bound, §6.4)."""
    params = {"p": provider, "run": run_id, **counted_params()}
    return int((await conn.execute(_RUN_NORMAL, params)).scalar_one())


async def admitted_run_cap(conn: AsyncConnection, provider: Provider, run_id: int) -> int | None:
    """The per-run cap (§6.4) that the run's latest counted normal entry of `provider` was admitted under, as
    its reservation recorded it (`per_run_cap`); `None` without such an entry.

    The run's normal units are within it (I3, §17.2), whatever the window has become since: the reservations
    of a provider serialise on its window row, so id order is admission order; that entry's admission kept
    the run's count (every entry counted now was counted then: an entry only leaves the counted set) within
    the cap in force then. A later lower cap is the bound, not an earlier higher one (final review F3, Minor
    1): an admission that exceeded the cap in force is caught, and a refinement after the last admission
    raises no false alarm."""
    params = {"p": provider, "run": run_id, **counted_params()}
    cap = (await conn.execute(_RUN_CAP, params)).scalar()
    return None if cap is None else int(cap)


async def latest_attempt(
    conn: AsyncConnection, provider: Provider, request_key: bytes
) -> LedgerAttempt | None:
    """The attempt of `request_key` with the highest `attempt_seq`, across all run attempts (§6.2 rule 2)."""
    row = (await conn.execute(_LATEST, {"p": provider, "key": request_key})).first()
    if row is None:
        return None
    return LedgerAttempt(
        entry_id=int(row.id),
        attempt_seq=int(row.attempt_seq),
        state=row.state,
        fetch_request_id=row.fetch_request_id,
        run_id=row.run_id,
        run_attempt_no=row.run_attempt_no,
        dispatched=bool(row.dispatched),
    )


# -- the hard caps beyond the monthly window (§6.5)
def serpapi_hour_admits(
    *,
    local_last_hour: int,
    this_hour: int,
    local_since_observation: int,
    hourly_cap: int,
    conservative: bool,
    units: int = 1,
) -> bool:
    """§6.5: `hour_used = max(local in the last 60 min, this_hour + local since the observation - 120 s)`,
    admitted iff `hour_used + units <= H`, or `<= H - 5` in conservative mode."""
    hour_used = max(local_last_hour, this_hour + local_since_observation)
    limit = hourly_cap - CONSERVATIVE_MARGIN if conservative else hourly_cap
    return hour_used + units <= limit


async def serpapi_hour_ok(
    conn: AsyncConnection, w: Window, now: datetime, units: int, settings: QuotaSettings, *, local_recent: int
) -> bool:
    """The SerpApi hourly rule on the current window's latest account observation (§6.5).

    Fails closed (`False`) without an observation younger than `SERPAPI_OBSERVATION_FRESH`: the account is
    read immediately before every search. `H = min(serpapi_hourly_cap, account_rate_limit_per_hour)`. The two
    local counts are read from the ledger; `local_recent` adds attempts the caller made that are not in it
    yet.
    """
    seen_at, this_hour = w.hour_observed_at, w.observed_this_hour
    if seen_at is None or this_hour is None or now - seen_at > SERPAPI_OBSERVATION_FRESH:
        return False
    params = {"hour_ago": now - HOUR, "since": seen_at - SERPAPI_USAGE_LAG, **counted_params()}
    counts = (await conn.execute(_SERPAPI_HOUR, params)).one()
    cap = settings.serpapi_hourly_cap
    if w.observed_hourly_cap is not None:
        cap = min(cap, w.observed_hourly_cap)
    return serpapi_hour_admits(
        local_last_hour=int(counts.last_hour) + local_recent,
        this_hour=this_hour,
        local_since_observation=int(counts.since_observation) + local_recent,
        hourly_cap=cap,
        conservative=w.conservative_until is not None and now < w.conservative_until,
        units=units,
    )


def note_foreign_usage(w: Window, *, this_hour: int, local_before: int, now: datetime) -> int:
    """§6.5 detection, pure: `foreign = this_hour - local attempts in the 60 min before the observation`.

    Beyond `FOREIGN_TOLERANCE` (one search, for lag and hour-boundary rounding) the window admits in
    conservative mode until 24 h after this detection (`conservative_until`). Returns `foreign`.
    """
    foreign = this_hour - local_before
    if foreign > FOREIGN_TOLERANCE:
        w.conservative_until = now + CONSERVATIVE_FOR
    return foreign


async def detect_foreign_usage(
    conn: AsyncConnection, w: Window, account: AccountObservation, now: datetime
) -> int:
    """Foreign usage at an account read (§6.5): `quota.serpapi_foreign_usage` (warn) and conservative mode
    beyond the tolerance; a read without it after the mode has ended resolves the alert. Returns `foreign`."""
    params = {
        "hour_before": account.observed_at - HOUR,
        "observed_at": account.observed_at,
        **counted_params(),
    }
    local_before = int((await conn.execute(_SERPAPI_BEFORE, params)).scalar_one())
    foreign = note_foreign_usage(w, this_hour=account.this_hour_searches, local_before=local_before, now=now)
    if foreign > FOREIGN_TOLERANCE:
        await raise_alert(
            conn,
            code=FOREIGN_USAGE_ALERT,
            severity="warn",
            subject=_subject("serpapi"),
            message="SerpApi reports more searches this hour than the app made",
            context={"foreign": foreign, "this_hour": account.this_hour_searches, "local": local_before},
        )
        log.warning("serpapi_foreign_usage", foreign=foreign, this_hour=account.this_hour_searches)
    elif w.conservative_until is None or now >= w.conservative_until:
        await resolve_alert(conn, code=FOREIGN_USAGE_ALERT, subject=_subject("serpapi"))
    return foreign


def adzuna_admits(counts: Sequence[int], units: int, settings: QuotaSettings) -> bool:
    """Adzuna's rolling caps (§6.5): `count + units <= cap` in each window of 60 s, 24 h, 7 d and 30 d."""
    caps = (
        settings.adzuna_cap_minute,
        settings.adzuna_cap_day,
        settings.adzuna_cap_week,
        settings.adzuna_cap_month,
    )
    return all(n + units <= cap for n, cap in zip(counts, caps, strict=True))


async def adzuna_windows_ok(
    conn: AsyncConnection, now: datetime, units: int, settings: QuotaSettings
) -> bool:
    """The four rolling Adzuna counts over `created_at` (`COUNTED` attempts), then `adzuna_admits`."""
    minute, day, week, month = (now - span for span in ADZUNA_SPANS)
    params = {"minute": minute, "day": day, "week": week, "month": month, **counted_params()}
    row = (await conn.execute(_ADZUNA_ROLLING, params)).one()
    return adzuna_admits((int(row.minute), int(row.day), int(row.week), int(row.month)), units, settings)


# -- pure cores of the authoritative path
def admission(
    w: Window,
    *,
    provider: Provider,
    request_class: QuotaClass,
    units: int,
    used: int,
    run_normal_count: int,
    hour_ok: bool,
    adzuna_ok: bool,
    now: datetime,
    settings: QuotaSettings,
    interval_s: int,
) -> Admit | Deny:
    """§6.7 `reserve` on the locked window, in place: accrue, `decide`, and for an admission its tokens and
    the probe it starts (`probe_used`, the next probe `PROBE_RETRY` later until it validates)."""
    accrue(w, now, interval_s=interval_s)
    d = decide(
        w,
        provider=provider,
        request_class=request_class,
        units=units,
        used=used,
        run_normal_count=run_normal_count,
        hour_ok=hour_ok,
        adzuna_ok=adzuna_ok,
        now=now,
        per_run_cap=per_run_cap(settings, fair_share(w, interval_s)),
    )
    if isinstance(d, Admit):
        w.tokens -= d.tokens_used
        if d.starts_probe:
            w.probe_used, w.probe_failed, w.next_probe_at = True, False, now + PROBE_RETRY
    return d


def probe_outcome(state: SettleState, http_status: int | None, w: Window) -> ProbeOutcome:
    """How a settled probe ended, given the window after its observation.

    `never_sent`: a connect error before any byte (`not_billed` without a status); `validated`: a billed 2xx
    after which the window is `validated`; anything else (`ambiguous`, a non-2xx status, a 2xx whose headers
    validated nothing and so leaves an unvalidated window unvalidated, an exhausted one exhausted) `failed`,
    so the next probe follows 12 h later (§6.7) instead of never, or only when the window ends.
    """
    if state == "not_billed" and http_status is None:
        return "never_sent"
    if state == "billed" and http_status in _HTTP_OK and w.state == "validated":
        return "validated"
    return "failed"


def _refill_on_reset(w: Window, changes: Sequence[WindowChange], interval_s: int) -> None:
    """A window opened by an observed provider reset starts with a full bucket at its own capacity (T7)."""
    if any(isinstance(change, Insert) for change in changes):
        w.capacity = w.tokens = capacity(w, interval_s)


def settle_window(
    cur: Window,
    obs: Observation | None,
    settings: QuotaSettings,
    *,
    provider: Provider,
    state: SettleState,
    http_status: int | None,
    probe: bool,
    used_units: int,
    entry_units: int,
    interval_s: int,
    now: datetime,
) -> tuple[Window, list[WindowChange], ObservationEffects]:
    """The window side of a settlement, pure, on the current window `cur`: the observation (§6.3 steps 0-4),
    a reset window's full bucket, and the probe's outcome when the settling entry is `cur`'s probe.

    `used_units` is `cur`'s counted usage after the entry's own state change; `entry_units` the entry's
    counted units (0 when it settled `not_billed`), against which a window opened by a provider reset is
    measured.
    """
    changes: list[WindowChange] = []
    if obs is None:
        w, fx = replace(cur), ObservationEffects(0, (), False)
    else:
        w, changes, fx = apply_observation_pure(
            cur, obs, settings, provider=provider, used_units=used_units, entry_units=entry_units
        )
    _refill_on_reset(w, changes, interval_s)
    if probe:
        note_probe_outcome(w, probe_outcome(state, http_status, w), now=now)
    return w, changes, fx


def _describes(cur: Window, obs: Observation) -> bool:
    """Whether `obs` reaches past step 0 and the stale-renewal rule (T7-a) on `cur`: it describes `cur`."""
    fresh = cur.last_observed_at is None or obs.observed_at > cur.last_observed_at
    return fresh and not (obs.reset_at is not None and obs.reset_at <= cur.window_start)


# -- the authoritative path
async def lock_run_owner(conn: AsyncConnection, owner: "RunOwner") -> None:
    """The owner row `FOR SHARE` with its predicate (§3.3.3), else `FencedOut("run")`; and no active `global`
    purge fence (§20.2), else `FencedOut("purge_fence")`. The first statement of every owner transaction."""
    if (await conn.execute(_OWNER, {"run": owner.run_id, "attempt": owner.attempt_no})).first() is None:
        raise FencedOut("run")
    if await global_fence_active(conn):
        raise FencedOut("purge_fence")


async def lock_item_owner(conn: AsyncConnection, owner: ItemOwner) -> None:
    """The owner row of an item's resolution (module docstring, The item owner): a run's through
    `lock_run_owner`; an operation claim's `FOR SHARE` (`lock_operation_owner`, else
    `FencedOut("operation")`), then no active `global` purge fence, else `FencedOut("purge_fence")`."""
    if isinstance(owner, OperationClaim):
        await lock_operation_owner(conn, owner)
        if await global_fence_active(conn):
            raise FencedOut("purge_fence")
        return
    await lock_run_owner(conn, owner)


async def _record_effects(
    conn: AsyncConnection, w: Window, fx: ObservationEffects, obs: Observation | None, *, now: datetime
) -> None:
    """An observation's adjustment (tighten-only, §6.3 step 3) and its alerts, in the caller's transaction."""
    if fx.adjustment_units > 0 and obs is not None:
        left = fx.adjustment_units
        while left > 0:
            units = min(left, _MAX_UNITS)
            await conn.execute(
                _INSERT_ADJUSTMENT,
                {
                    "p": w.provider,
                    "ws": w.window_start,
                    "units": units,
                    "evidence": f"provider_remaining={obs.remaining}",
                    "now": now,
                },
            )
            left -= units
        log.warning(
            "quota_adjustment", provider=w.provider, units=fx.adjustment_units, remaining=obs.remaining
        )
    for code in fx.alerts:
        info = code == "quota.limit_increased"
        await raise_alert(
            conn,
            code=code,
            severity="info" if info else "warn",
            subject=_subject(w.provider),
            message="the provider reports a higher limit than configured"
            if info
            else "the provider reports less remaining than the ledger counts",
            context={
                "provider": w.provider,
                "limit": obs.limit if obs else None,
                "remaining": obs.remaining if obs else None,
            },
        )


async def check_serpapi_renewal(conn: AsyncConnection, w: Window) -> None:
    """Ruling T7-b: raise `quota.serpapi_renewal_stale` (warn) while reads applied more than 48 h after the
    SerpApi window's start have not validated it; resolve it once the window is validated. Call it after every
    account read applied to the current window."""
    if w.provider != "serpapi":
        return
    if w.state != "extrapolated_unvalidated":
        await resolve_alert(conn, code=RENEWAL_STALE_ALERT, subject=_subject("serpapi"))
    elif w.last_observed_at is not None and w.last_observed_at - w.window_start > RENEWAL_STALE_AFTER:
        await raise_alert(
            conn,
            code=RENEWAL_STALE_ALERT,
            severity="warn",
            subject=_subject("serpapi"),
            message="SerpApi account reads still report the previous renewal date",
            context={"window_start": w.window_start.isoformat()},
        )


async def observe_account(
    conn: AsyncConnection,
    w: Window,
    account: AccountObservation,
    *,
    settings: QuotaSettings,
    interval_s: int,
    now: datetime,
) -> Window:
    """Apply a SerpApi account read to the locked current window `w` and save it; returns the current window.

    The read is an observation (§6.3, the stale-renewal rule of T7-a included): its adjustment and alerts,
    then the foreign-usage detection (§6.5) and the renewal check (T7-b). A read that shows a provider reset
    opens the new window with a full bucket. A read older than the window's `hour_observed_at` (a late one,
    step 0) changes nothing at all: neither the window nor the foreign-usage mode and its alert.
    """
    if w.hour_observed_at is not None and account.observed_at < w.hour_observed_at:
        return w
    obs = account_observation(account)
    used = await used_units(conn, "serpapi", w.window_start)
    cur, changes, fx = apply_observation_pure(w, obs, settings, provider="serpapi", used_units=used)
    _refill_on_reset(cur, changes, interval_s)
    await apply_changes(conn, changes)
    await _record_effects(conn, cur, fx, obs, now=now)
    await detect_foreign_usage(conn, cur, account, now)
    await check_serpapi_renewal(conn, cur)
    await save_window(conn, cur)
    return cur


async def _account_window(
    conn: AsyncConnection,
    account: AccountObservation,
    *,
    settings: QuotaSettings,
    interval_s: int,
    now: datetime,
) -> Window | None:
    """The SerpApi window under its lock, rolled forward (or bootstrapped from `account`), with the read
    applied (`observe_account`): what `reserve` and `record_account` both run for an account read."""
    w = await current_window_locked(conn, "serpapi", now, settings, interval_s=interval_s, account=account)
    if w is None:
        return None
    return await observe_account(conn, w, account, settings=settings, interval_s=interval_s, now=now)


async def record_account(
    conn: AsyncConnection, obs: AccountObservation, *, settings: RunSettings, now: datetime
) -> None:
    """Persist a SerpApi account read made outside a reservation (the fallback projection's read, and the read
    after the last search of a traversal, §5.3.2) in the caller's transaction, after its owner row: the window
    lock, the observation, the foreign-usage detection and the renewal check (T7-b), exactly as `reserve`
    applies the read it is given."""
    await _account_window(conn, obs, settings=settings.quota, interval_s=settings.interval_s, now=now)


async def reserve(
    conn: AsyncConnection,
    provider: Provider,
    *,
    owner: "RunOwner",
    request_key: bytes,
    attempt_seq: int,
    request_class: QuotaClass,
    fetch_request_id: int,
    settings: QuotaSettings,
    interval_s: int,
    now: datetime,
    account: AccountObservation | None = None,
    units: int = 1,
) -> Reservation | Denial:
    """Reserve one attempt (§6.2 rule 1, §6.7 `reserve`) in the caller's transaction, or deny it.

    In order: the owner lock (`FencedOut` before anything is written), the window lock with its roll-forward
    or bootstrap (SerpApi bootstraps from `account`; without a window it is denied `window_cap`), the account
    read applied (SerpApi), accrual, and `decide` with the hard caps. An admission inserts the `reserved` row
    (`created_at = now`, `per_run_cap` the §6.4 cap it was admitted under, which I3 checks the run against)
    and saves the window; a denial saves the accrual (and the read) only.
    """
    if not 1 <= attempt_seq <= MAX_ATTEMPT_SEQ:
        raise ValueError(f"attempt_seq must be 1 to {MAX_ATTEMPT_SEQ}, got {attempt_seq}")
    await lock_run_owner(conn, owner)
    if account is not None and provider == "serpapi":
        w = await _account_window(conn, account, settings=settings, interval_s=interval_s, now=now)
    else:
        w = await current_window_locked(conn, provider, now, settings, interval_s=interval_s)
    if w is None:
        return Denial("window_cap")  # SerpApi without an account observation (§6.3)
    used = await used_units(conn, provider, w.window_start)
    d = admission(
        w,
        provider=provider,
        request_class=request_class,
        units=units,
        used=used,
        run_normal_count=await normal_count_for_run(conn, provider, owner.run_id),
        hour_ok=provider != "serpapi" or await serpapi_hour_ok(conn, w, now, units, settings, local_recent=0),
        adzuna_ok=provider != "adzuna" or await adzuna_windows_ok(conn, now, units, settings),
        now=now,
        settings=settings,
        interval_s=interval_s,
    )
    if isinstance(d, Deny):
        await save_window(conn, w)
        _set_gauges(w, used)
        return Denial(d.code)
    params = {
        "p": provider,
        "ws": w.window_start,
        "cls": d.request_class,
        "key": request_key,
        "seq": attempt_seq,
        "run": owner.run_id,
        "attempt": owner.attempt_no,
        "fr": fetch_request_id,
        "units": units,
        # the cap `admission` decided with (accrual and the probe change neither the limit nor the period)
        "cap": min(per_run_cap(settings, fair_share(w, interval_s)), _MAX_RUN_CAP),
        "now": now,
    }
    entry_id = int((await conn.execute(_INSERT_REQUEST, params)).scalar_one())
    await save_window(conn, w)
    _set_gauges(w, used + units)
    return Reservation(entry_id=entry_id, request_class=d.request_class, window_start=w.window_start)


async def mark_dispatched(conn: AsyncConnection, entry_id: int, owner: "RunOwner") -> None:
    """Commit the dispatch marker (§6.2 rule 3) in the caller's transaction, immediately before the send.

    Under the owner's `FOR SHARE`, the owner's own entry moves `reserved -> dispatched`. Anything else (a lost
    predicate, an active global purge fence, another owner's entry, an entry already dispatched or released
    meanwhile) is `FencedOut`, and the caller must abort without sending.
    """
    await lock_run_owner(conn, owner)
    entry = await _entry(conn, entry_id)
    if entry is None or (entry.run_id, entry.run_attempt_no) != (owner.run_id, owner.attempt_no):
        raise FencedOut("quota_ledger")
    try:
        await transition(conn, "quota_ledger", entry_id, ["reserved"], "dispatched")
    except TransitionConflict as exc:
        raise FencedOut("quota_ledger") from exc


async def settle(
    conn: AsyncConnection,
    entry_id: int,
    *,
    state: SettleState,
    evidence: str | None,
    http_status: int | None,
    provider_remaining: int | None,
    observation: Observation | None,
    settings: QuotaSettings,
    interval_s: int,
    now: datetime,
) -> None:
    """Settle a dispatched attempt (§6.2 rule 4) in the caller's transaction, which also persists the response
    and holds the owner's lock.

    The provider's current window is locked (rolled forward) first; then the observation on the current
    window (§6.3, pure), its changes (a window a provider reset opened is inserted), and `dispatched -> state`
    in one `transition()` that also re-attributes the entry to that new window (`window_start` is settable,
    so no write bypasses the registry; else `FencedOut`: the entry was recovered meanwhile, so the late
    response is discarded with the transaction, which the caller rolls back); then the adjustment and the
    effect alerts. A probe's outcome is recorded on its window; a JSearch 429 that exhausts the window raises
    `quota.exhausted`, and a later validated observation with `remaining > 0` resolves it; an `ambiguous`
    settlement raises `quota.ambiguous_attempt`. A SerpApi "out of searches" (`remaining = 0`) describes the
    month its search was reserved in: settled after the rollover, it is not applied to the new month (fix
    wave F4, ledger L175).

    `not_billed` needs refund evidence (`SETTLEMENT_EVIDENCE`), and `rapidapi_gateway_404` is JSearch's alone
    (the RapidAPI gateway fronts no other provider), else `ValueError` before anything is written.
    """
    if state == "not_billed" and evidence not in SETTLEMENT_EVIDENCE:
        raise ValueError(f"not_billed needs refund evidence ({', '.join(sorted(SETTLEMENT_EVIDENCE))})")
    entry = await _entry(conn, entry_id)
    if entry is None:
        raise LookupError(f"no quota ledger request {entry_id}")
    provider = entry.provider
    if evidence == GATEWAY_404_EVIDENCE and provider not in GATEWAY_PROVIDERS:
        raise ValueError(f"{GATEWAY_404_EVIDENCE} is no refund evidence for {provider}: no RapidAPI gateway")
    cur = await current_window_locked(conn, provider, now, settings, interval_s=interval_s)
    if cur is None:
        raise LookupError(f"no current {provider} window")  # the entry's own window exists, so one is current
    own_window = entry.window_start == cur.window_start
    if provider == "serpapi" and observation is not None and observation.remaining == 0 and not own_window:
        observation = None  # the old month's "out of searches", not the rolled window's (ledger L175)
    # `cur`'s counted usage after the entry's own state change: a `not_billed` entry of `cur` leaves it
    used = await used_units(conn, provider, cur.window_start)
    if own_window and state not in COUNTED:
        used -= entry.units
    w, changes, fx = settle_window(
        cur,
        observation,
        settings,
        provider=provider,
        state=state,
        http_status=http_status,
        probe=entry.request_class == "probe" and own_window,
        used_units=used,
        entry_units=entry.units if state in COUNTED else 0,
        interval_s=interval_s,
        now=now,
    )
    await apply_changes(conn, changes)  # first: a re-attributed entry's new window must exist (its FK)
    settled: dict[str, object] = {"http_status": http_status, "provider_remaining": provider_remaining}
    if fx.reattribute:
        settled["window_start"] = w.window_start
    try:
        await transition(conn, "quota_ledger", entry_id, ["dispatched"], state, reason=evidence, set_=settled)
    except TransitionConflict as exc:
        raise FencedOut("quota_ledger") from exc
    await _record_effects(conn, w, fx, observation, now=now)
    if provider == "jsearch" and observation is not None and _describes(cur, observation):
        if observation.status == 429 and w.state == "exhausted_until_reset":
            await raise_alert(
                conn,
                code=EXHAUSTED_ALERT,
                severity="warn",
                subject=_subject(provider),
                message="JSearch is exhausted until its reset",
                context={"window_end": w.window_end.isoformat()},
            )
        elif observation.status != 429 and w.state == "validated" and (observation.remaining or 0) > 0:
            await resolve_alert(conn, code=EXHAUSTED_ALERT, subject=_subject(provider))
    if state == "ambiguous":
        await _raise_ambiguous(conn, provider, entry_id, http_status)
    await save_window(conn, w)
    _set_gauges(w, await used_units(conn, provider, w.window_start))


async def _raise_ambiguous(
    conn: AsyncConnection, provider: Provider, entry_id: int, http_status: int | None
) -> None:
    await raise_alert(
        conn,
        code=AMBIGUOUS_ALERT,
        severity="warn",
        subject=_subject(provider),
        message="a paid attempt settled ambiguous and stays charged",
        context={"entry_id": entry_id, "http_status": http_status},
    )


# -- recovery (§6.2 rule 5), centralised under the window lock
def _awaits_probe(entry: _Entry, w: Window) -> bool:
    """Whether a recovered entry is a probe its window still waits on: a `validated` window waits on none."""
    return entry.request_class == "probe" and w.state != "validated"


async def release_never_dispatched(conn: AsyncConnection, entry_id: int, *, now: datetime) -> bool:
    """`reserved -> not_billed('never_dispatched')` under the lock of the entry's window (current or closed);
    a probe's window records `never_sent`, so the next request probes at once, unless the window is already
    `validated` (it awaits no probe: its probe columns stay as its validation left them, ledger L109).
    `False` when the entry is not `reserved` (nothing written). The caller holds the owner row (the new
    owner's, or `FOR UPDATE` in recovery) and knows the old owner can no longer dispatch it."""
    entry = await _entry(conn, entry_id)
    if entry is None or entry.state != "reserved":
        return False
    w = await window_locked(conn, entry.provider, entry.window_start)
    try:
        await transition(
            conn, "quota_ledger", entry_id, ["reserved"], "not_billed", reason="never_dispatched"
        )
    except TransitionConflict:
        return False
    if _awaits_probe(entry, w):
        note_probe_outcome(w, "never_sent", now=now)
        await save_window(conn, w)
    if w.is_current:
        _set_gauges(w, await used_units(conn, entry.provider, w.window_start))
    return True


async def mark_ambiguous(conn: AsyncConnection, entry_id: int, *, now: datetime) -> bool:
    """`dispatched -> ambiguous` under the lock of the entry's window: it stays charged. A probe's window
    records `failed` (the next probe 12 h later) unless it is already `validated` (no stale `next_probe_at`
    could then admit an early re-probe after a later 429, ledger L109); `quota.ambiguous_attempt` is raised.
    `False` when the entry is not `dispatched` (nothing written)."""
    entry = await _entry(conn, entry_id)
    if entry is None or entry.state != "dispatched":
        return False
    w = await window_locked(conn, entry.provider, entry.window_start)
    try:
        await transition(conn, "quota_ledger", entry_id, ["dispatched"], "ambiguous")
    except TransitionConflict:
        return False
    if _awaits_probe(entry, w):
        note_probe_outcome(w, "failed", now=now)
        await save_window(conn, w)
    await _raise_ambiguous(conn, entry.provider, entry_id, None)
    return True


async def _move_request(
    conn: AsyncConnection,
    fetch_request_id: int | None,
    from_states: list[str],
    to_state: str,
    set_: dict[str, str],
) -> None:
    """The recovered entry's fetch request follows it; one in another state is left as it is (logged)."""
    if fetch_request_id is None:
        return
    try:
        await transition(conn, "fetch_requests", fetch_request_id, from_states, to_state, set_=set_)
    except TransitionConflict:
        log.warning("ledger_fetch_request_unchanged", fetch_request_id=fetch_request_id, to_state=to_state)


async def _recover(conn: AsyncConnection, entry_id: int, *, cutoff: datetime, now: datetime) -> bool:
    first = await _entry(conn, entry_id)
    if first is None:
        return False
    owner_holds = False
    if first.run_id is not None:  # the owner row first (lock order), so a stale dispatch marker finds 0 rows
        run = (await conn.execute(_LOCK_RUN, {"run": first.run_id})).first()
        owner_holds = run is not None and run.status == "running" and run.attempt_no == first.run_attempt_no
    entry = await _entry(conn, entry_id)  # read again under the owner's lock
    if entry is None:
        return False
    if entry.state == "reserved" and entry.created_at < cutoff and not owner_holds:
        if await release_never_dispatched(conn, entry_id, now=now):
            await _move_request(
                conn,
                entry.fetch_request_id,
                ["planned", "reserved"],
                "skipped",
                {"skip_reason": "superseded"},
            )
            log.info("ledger_entry_released", entry_id=entry_id, provider=entry.provider)
            return True
    elif entry.state == "dispatched" and entry.dispatched_at is not None and entry.dispatched_at < cutoff:
        if await mark_ambiguous(conn, entry_id, now=now):
            await _move_request(
                conn, entry.fetch_request_id, ["dispatched"], "ambiguous", {"error_code": "unsettled"}
            )
            log.warning("ledger_entry_ambiguous", entry_id=entry_id, provider=entry.provider)
            return True
    return False


@reconciler("ledger_unsettled", "tick")
async def reconcile_ledger_unsettled(ctx: ReconcileContext) -> int:
    """§17.4 `ledger_unsettled`: `reserved` entries older than `RECOVERY_AFTER` whose owner predicate no
    longer holds -> `not_billed('never_dispatched')` and their fetch request `skipped('superseded')`;
    `dispatched` entries unsettled for longer -> `ambiguous` and their fetch request `ambiguous`. One
    transaction per entry, fenced by the worker epoch first; the owner row is locked `FOR UPDATE` before the
    window. Returns the count. A superseded worker raises `FencedOut` at its first fenced transaction, which
    wrote nothing (`fence_epoch` logged it), so that `run_reconcilers` stops the cadence at this reconciler
    and the worker requests its stop, recording no failure (Ruling FR-r; fix wave F7). That fence is the only
    one a pass can meet: `_recover` locks the run row itself and its recovery writes return `False` on a
    conflict.
    """
    cutoff = ctx.now - RECOVERY_AFTER
    async with ctx.engine.connect() as conn:
        found = await conn.execute(_UNSETTLED, {"cutoff": cutoff, "limit": RECOVERY_BATCH})
        ids = [int(row.id) for row in found]
    recovered = 0
    for entry_id in ids:
        async with ctx.engine.begin() as conn:
            await fence_epoch(conn, ctx.epoch)
            recovered += await _recover(conn, entry_id, cutoff=cutoff, now=ctx.now)
    return recovered


# -- planning (§6.7 `project_allowance`)
async def project_allowance(
    conn: AsyncConnection,
    provider: Provider,
    specs: Sequence[PlannedSpec],
    now: datetime,
    settings: QuotaSettings,
    *,
    interval_s: int,
    account: AccountObservation | None = None,
) -> list[Admit | Deny]:
    """The allowance of `specs` at `now`, simulated on a snapshot: nothing is locked or written (§6.7).

    Bootstrap and rollover happen in memory; a SerpApi account read is applied to the copy first (Ruling T7-b,
    the stale-renewal rule included), so a fresh read that shows the renewal opens the fallback even after a
    month without searches. The simulation carries `used`, the run's normal count and the tokens only, and
    never sets `probe_used`: on an unvalidated window every normal spec projects `Admit(probe)`; the
    reservation decides which one is the probe.
    """
    snapshot = await current_snapshot(conn, provider)
    w, _ = roll_forward(snapshot, provider, now, settings, interval_s=interval_s, account=account)
    if w is None:
        return [Deny("window_cap")] * len(specs)
    used = await used_units(conn, provider, w.window_start)
    if account is not None and provider == "serpapi":
        obs = account_observation(account)
        w, changes, fx = apply_observation_pure(w, obs, settings, provider=provider, used_units=used)
        _refill_on_reset(w, changes, interval_s)
        used = (0 if fx.reattribute else used) + fx.adjustment_units
    accrue(w, now, interval_s=interval_s)
    cap = per_run_cap(settings, fair_share(w, interval_s))
    run_normal, out = 0, list[Admit | Deny]()
    for spec in specs:
        d = decide(
            w,
            provider=provider,
            request_class=spec.request_class,
            units=1,
            used=used,
            run_normal_count=run_normal,
            hour_ok=True,
            adzuna_ok=True,
            now=now,
            per_run_cap=cap,
        )
        if isinstance(d, Admit):
            used, w.tokens = used + 1, w.tokens - d.tokens_used
            run_normal += d.tokens_used
        out.append(d)
    return out
