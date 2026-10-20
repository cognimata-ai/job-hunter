"""Provider quota windows (spec §6.3, §7.8): bootstrap, extrapolation, observations and persistence.

Every window is kept: `quota_windows` is keyed by `(provider, window_start)`, with exactly one current row per
provider. The pure functions (`bootstrap_window`, `roll_forward`, `apply_observation_pure`) compute in memory
and return the row changes (`Insert`, `Close`) instead of writing them, so that planning and the dry-run
simulate on a snapshot exactly what the authoritative path (`current_window_locked`, under `FOR UPDATE`)
persists.

A window's `state` follows §7.16.8. The table has no `id`, so `db.transitions.transition()` cannot address
it: `save_window` is the one statement that changes it, checks the edge against `TRANSITIONS["quota_windows"]`
before it runs, and the `quota_windows_assert_transition` trigger checks it again. A closed window never
changes state again.

New windows start with a full bucket (`tokens = capacity`, the initial credit of §6.4) at `tokens_at =
window_start`. Their limit comes from settings and is tightened by what the provider reports; only a window
opened by an observed provider reset keeps the (tighter) limit of the window it replaces, because the same
response's limit is then compared with it (step 2).
"""

import calendar
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, replace
from datetime import datetime, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Final, Literal

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.db.transitions import TRANSITIONS
from jobhunter.run_settings import QuotaSettings
from jobhunter.sources.base import Observation

Provider = Literal["jsearch", "serpapi", "adzuna"]
WindowState = Literal["validated", "extrapolated_unvalidated", "exhausted_until_reset"]
ClosedReason = Literal["rolled_over", "provider_reset_observed"]
ObservationAlert = Literal["quota.limit_increased", "quota.unaccounted_usage"]

# JSearch: 30.96 days anchored to the subscription; Adzuna: the synthetic 30-day window that carries its
# bucket. SerpApi's period is the calendar month ending at `plan_renewal_date` (`serpapi_period`).
NOMINAL_PERIOD: Final[Mapping[Provider, timedelta]] = MappingProxyType(
    {"jsearch": timedelta(seconds=2_675_242), "adzuna": timedelta(days=30)}
)
PROBE_RETRY: Final = timedelta(hours=12)  # the next probe after a failed or exhausted one (§6.3)
RESERVE_RELEASE: Final = timedelta(hours=48)  # the final stretch of a window without a reserve (§6.4)


@dataclass
class Window:
    """One `quota_windows` row (§7.8), column for column."""

    provider: Provider
    window_start: datetime
    window_end: datetime
    is_current: bool
    closed_reason: ClosedReason | None
    limit_units: int
    reserve_units: int
    state: WindowState
    tokens: Decimal
    tokens_at: datetime
    capacity: Decimal
    probe_used: bool = False
    probe_failed: bool = False
    next_probe_at: datetime | None = None
    last_observed_at: datetime | None = None
    observed_remaining: int | None = None
    observed_limit: int | None = None
    observed_this_hour: int | None = None
    observed_hourly_cap: int | None = None
    conservative_until: datetime | None = None
    hour_observed_at: datetime | None = None


@dataclass(frozen=True)
class AccountObservation:
    """A SerpApi `account.json` read (§5.3.2, §6.3): free, made before every search."""

    observed_at: datetime
    plan_renewal_date: datetime
    searches_per_month: int
    plan_searches_left: int
    this_hour_searches: int
    account_rate_limit_per_hour: int


@dataclass(frozen=True)
class Insert:
    """A new current row; `window` is a copy taken when the change was made."""

    window: Window


@dataclass(frozen=True)
class Close:
    """The current row becomes history: `is_current = false`, `closed_reason = reason`, nothing else."""

    window: Window
    reason: ClosedReason


WindowChange = Insert | Close


@dataclass(frozen=True)
class ObservationEffects:
    """What the caller of `apply_observation_pure` still has to do in its transaction (Task 8 `settle`).

    `adjustment_units > 0` is an `adjustment` row in the returned window; `alerts` are raised with subject
    `provider:<name>` (`quota.limit_increased` info, `quota.unaccounted_usage` warn); `reattribute` moves the
    entry whose response carried the observation to the returned (new) window.
    """

    adjustment_units: int
    alerts: tuple[ObservationAlert, ...]
    reattribute: bool


def limit_for(provider: Provider, settings: QuotaSettings) -> int:
    """The settings limit of a new window: 200 / 250 by default, Adzuna's monthly cap."""
    if provider == "jsearch":
        return settings.jsearch_monthly_limit
    if provider == "serpapi":
        return settings.serpapi_monthly_limit
    return settings.adzuna_cap_month


def reserve_for(provider: Provider, limit: int, settings: QuotaSettings) -> int:
    """`ceil(reserve_frac * limit)`; for Adzuna `limit - int(0.9 * limit)`, so that `N = 0.9 * cap_month`.

    Integer arithmetic: a float fraction would make `ceil(0.07 * 100)` 8.
    """
    if provider == "adzuna":
        return limit - limit * 9 // 10
    return -(-settings.reserve_pct * limit // 100)


def _add_months(at: datetime, months: int) -> datetime:
    """`at` moved by whole calendar months, the day clamped to the length of the target month."""
    index = at.month - 1 + months
    year, month = at.year + index // 12, index % 12 + 1
    return at.replace(year=year, month=month, day=min(at.day, calendar.monthrange(year, month)[1]))


def serpapi_period(renewal: datetime) -> tuple[datetime, datetime]:
    """The calendar month ending at `plan_renewal_date`: `[renewal - 1 month, renewal)`."""
    return _add_months(renewal, -1), renewal


def _period(provider: Provider, w: Window) -> timedelta:
    """The nominal length of `w`'s period: SerpApi's is the calendar month ending at its end."""
    if provider == "serpapi":
        start, end = serpapi_period(w.window_end)
        return end - start
    return NOMINAL_PERIOD[provider]


def _full(w: Window, interval_s: int) -> Window:
    """`w` with the bucket at its capacity (the initial credit)."""
    # pacing imports this module (the Window type, RESERVE_RELEASE): imported here to keep the cycle out of
    # module initialisation.
    from jobhunter.quota.pacing import capacity

    cap = capacity(w, interval_s)
    return replace(w, capacity=cap, tokens=cap)


def _new_window(
    provider: Provider,
    start: datetime,
    end: datetime,
    state: WindowState,
    limit: int,
    settings: QuotaSettings,
    interval_s: int,
) -> Window:
    blank = Window(
        provider=provider,
        window_start=start,
        window_end=end,
        is_current=True,
        closed_reason=None,
        limit_units=limit,
        reserve_units=reserve_for(provider, limit, settings),
        state=state,
        tokens=Decimal(0),
        tokens_at=start,
        capacity=Decimal(1),
    )
    return _full(blank, interval_s)


def bootstrap_window(
    provider: Provider,
    now: datetime,
    settings: QuotaSettings,
    *,
    interval_s: int,
    account: AccountObservation | None,
) -> Window | None:
    """The first window of a provider that has none (§6.3), or `None` for SerpApi without an account read.

    JSearch `[now, now + period)` `extrapolated_unvalidated` (its first request is a probe); Adzuna
    `[now, now + 30 d)` `validated`; SerpApi the calendar month ending at the account's renewal date,
    `validated`, with the limit tightened by `searches_per_month` and the account's figures recorded.
    `last_observed_at` stays unset: the same read is still reconciled as an observation (step 0 onwards) by
    its caller.

    A read whose renewal is not after `now` describes a month that has already ended locally (the renewal gap,
    Ruling T7-a): that window keeps its figures and `roll_forward` closes it at once. The read is recorded as
    seen (`last_observed_at`, which the successor carries with the hour figures), so its reconciliation skips
    it and none of its renewal, limit or remaining reaches the current window.
    """
    if provider == "serpapi":
        if account is None:
            return None
        start, end = serpapi_period(account.plan_renewal_date)
        limit = min(limit_for(provider, settings), account.searches_per_month)
        w = _new_window(provider, start, end, "validated", limit, settings, interval_s)
        w.observed_limit, w.observed_remaining = account.searches_per_month, account.plan_searches_left
        w.observed_this_hour, w.observed_hourly_cap = (
            account.this_hour_searches,
            account.account_rate_limit_per_hour,
        )
        w.hour_observed_at = account.observed_at
        if now >= end:
            w.last_observed_at = account.observed_at
        return w
    state: WindowState = "extrapolated_unvalidated" if provider == "jsearch" else "validated"
    end = now + NOMINAL_PERIOD[provider]
    return _new_window(provider, now, end, state, limit_for(provider, settings), settings, interval_s)


def _extrapolated_successor(w: Window, settings: QuotaSettings, interval_s: int) -> Window:
    """`[end, end + period)` with a full bucket; JSearch and SerpApi unvalidated, Adzuna validated.

    The observation clock (`last_observed_at`, so a late response of the old window is still ignored), the
    SerpApi hour figures and the conservative mode (24 h from its detection, not per window) carry over.
    """
    start = w.window_end
    end = _add_months(start, 1) if w.provider == "serpapi" else start + NOMINAL_PERIOD[w.provider]
    state: WindowState = "validated" if w.provider == "adzuna" else "extrapolated_unvalidated"
    nxt = _new_window(w.provider, start, end, state, limit_for(w.provider, settings), settings, interval_s)
    nxt.last_observed_at = w.last_observed_at
    nxt.observed_this_hour, nxt.observed_hourly_cap = w.observed_this_hour, w.observed_hourly_cap
    nxt.hour_observed_at, nxt.conservative_until = w.hour_observed_at, w.conservative_until
    return nxt


def _closed(w: Window, reason: ClosedReason) -> Close:
    return Close(replace(w, is_current=False, closed_reason=reason), reason)


def roll_forward(
    w: Window | None,
    provider: Provider,
    now: datetime,
    settings: QuotaSettings,
    *,
    interval_s: int,
    account: AccountObservation | None = None,
) -> tuple[Window | None, list[WindowChange]]:
    """Pure (§6.7): bootstrap and extrapolate in memory; the current window and the row changes.

    Repeats across long downtime: every elapsed window is closed `rolled_over` and its successor inserted.
    `w` itself is never modified.
    """
    changes: list[WindowChange] = []
    if w is None:
        w = bootstrap_window(provider, now, settings, interval_s=interval_s, account=account)
        if w is not None:
            changes.append(Insert(replace(w)))
    while w is not None and now >= w.window_end:
        nxt = _extrapolated_successor(w, settings, interval_s)
        changes += [_closed(w, "rolled_over"), Insert(replace(nxt))]
        w = nxt
    return w, changes


def apply_observation_pure(
    w: Window,
    obs: Observation,
    settings: QuotaSettings,
    *,
    provider: Provider,
    used_units: int,
    entry_units: int = 0,
) -> tuple[Window, list[WindowChange], ObservationEffects]:
    """§6.3 observation reconciliation, steps 0 to 4 in this order, on a copy of the current window `w`.

    `used_units` is the counted usage of `w` (Task 8 `used_units`). `entry_units` are the units of the ledger
    entry whose response carried the observation (0 for an account read): when the provider is seen to have
    reset already, that entry is re-attributed to the new window and is its only usage (JSearch runs at
    concurrency 1), so step 3 measures the new window against it.

    0. A response observed no later than `w.last_observed_at` is late: nothing changes (the window returned
       is still a copy: `w` is never the caller's result to modify).
       A reset time no later than `w.window_start` describes the previous window (Ruling T7-a): SerpApi's
       renewal date is a date, so the local window rolls over at 00:00 UTC hours before SerpApi renews, and
       until then its reads report the old renewal and the old month's remaining. Only `last_observed_at` and
       the hour figures are recorded; steps 1 to 4 are skipped, so the window stays as it is (a SerpApi
       window stays `extrapolated_unvalidated`, which `pacing.decide` denies as `window_cap`).
    1. A reset time (never from a 429) at least half a period past `w`'s end means the provider has reset:
       `w` is closed `provider_reset_observed` and `[observed_at, reset_at)` opens `validated`, with `w`'s
       limit (tighten-only) and its capacity as the full bucket (the next accrual recomputes it). Otherwise it
       refines `w`: `window_end := reset_at`, `validated`, the probe settled; an exhausted SerpApi window
       stays exhausted (SerpApi is never probed: only its reset or rollover ends the exhaustion, so a lagging
       account read cannot re-open the month). A reset time that is not ahead of the observation identifies no
       window and is ignored.
    2. A lower limit lowers `limit_units` and the reserve with it; a higher one only alerts.
    3. `limit_units - remaining` above the local usage is an adjustment of the difference (tighten-only).
    4. A JSearch 429 with `remaining` 0 or absent exhausts the window until its reset, and so does a SerpApi
       observation with `remaining` 0 (an account read with no searches left, or a search answered "out of
       searches", which the acquisition reports as `remaining = 0`).
    """
    if w.last_observed_at is not None and obs.observed_at <= w.last_observed_at:
        return replace(w), [], ObservationEffects(0, (), False)  # a copy, as on every other path
    cur = replace(w, last_observed_at=obs.observed_at)
    if obs.reset_at is not None and obs.reset_at <= cur.window_start:
        # The previous window's figures (Ruling T7-a), e.g. a SerpApi read before the plan has renewed.
        _record_hour(cur, obs)
        return cur, [], ObservationEffects(0, (), False)
    changes: list[WindowChange] = []
    used = used_units
    reattribute = False
    reset_at = obs.reset_at
    if reset_at is not None and obs.status != 429 and reset_at > obs.observed_at:
        if reset_at >= cur.window_end + _period(provider, cur) / 2:
            changes.append(_closed(cur, "provider_reset_observed"))
            limit = min(cur.limit_units, limit_for(provider, settings))
            cur = replace(
                cur,
                window_start=obs.observed_at,
                window_end=reset_at,
                limit_units=limit,
                reserve_units=reserve_for(provider, limit, settings),
                state="validated",
                tokens=cur.capacity,
                tokens_at=obs.observed_at,
                probe_used=False,
                probe_failed=False,
                next_probe_at=None,
                observed_remaining=None,
                observed_limit=None,
            )
            used, reattribute = entry_units, True
        elif provider == "serpapi" and cur.state == "exhausted_until_reset":
            cur.window_end = reset_at  # out of searches until the renewal: no read re-opens the month
        else:
            cur.window_end, cur.state = reset_at, "validated"
            cur.probe_used, cur.probe_failed, cur.next_probe_at = False, False, None
    alerts: list[ObservationAlert] = []
    if obs.limit is not None:
        if obs.limit < cur.limit_units:
            cur.limit_units, cur.reserve_units = obs.limit, reserve_for(provider, obs.limit, settings)
        elif obs.limit > cur.limit_units:
            alerts.append("quota.limit_increased")
        cur.observed_limit = obs.limit
    adjustment = 0
    if obs.remaining is not None:
        cur.observed_remaining = obs.remaining
        provider_used = cur.limit_units - obs.remaining
        if provider_used > used:
            adjustment = provider_used - used
            alerts.append("quota.unaccounted_usage")
    if obs.status == 429 and provider == "jsearch" and obs.remaining in (None, 0):
        cur.state = "exhausted_until_reset"
    elif provider == "serpapi" and obs.remaining == 0:
        cur.state = "exhausted_until_reset"
    _record_hour(cur, obs)
    if reattribute:
        changes.append(Insert(replace(cur)))
    return cur, changes, ObservationEffects(adjustment, tuple(alerts), reattribute)


def _record_hour(w: Window, obs: Observation) -> None:
    """The provider's own hourly figures (SerpApi `this_hour_searches`), which belong to no monthly window."""
    if obs.this_hour is not None:
        w.observed_this_hour, w.observed_hourly_cap = obs.this_hour, obs.hourly_cap
        w.hour_observed_at = obs.observed_at


def account_observation(account: AccountObservation) -> Observation:
    """A SerpApi account read as the observation it is reconciled as (§6.3): the renewal is the reset."""
    return Observation(
        observed_at=account.observed_at,
        status=200,
        reset_at=account.plan_renewal_date,
        limit=account.searches_per_month,
        remaining=account.plan_searches_left,
        this_hour=account.this_hour_searches,
        hourly_cap=account.account_rate_limit_per_hour,
    )


ProbeOutcome = Literal["validated", "failed", "never_sent"]


def note_probe_outcome(w: Window, outcome: ProbeOutcome, *, now: datetime) -> None:
    """Record how the window's probe ended, in place: the one writer of the probe columns after a reservation.

    - `validated`: the probe is settled (`probe_used = probe_failed = False`, `next_probe_at = None`); step 1
      of the observation has done this already when it saw the reset.
    - `failed` (settled `ambiguous` or with a non-2xx status, a 2xx that validated nothing, or recovered as
      `ambiguous`): `probe_failed = True` and the next probe `PROBE_RETRY` later (§6.3); until then JSearch
      counts as exhausted for the fallback (§6.6).
    - `never_sent` (released `never_dispatched`, or a connect error before any byte): no probe happened, so
      the next request starts a fresh one at once instead of waiting 12 h: `probe_used = False` and no wait.
      An `exhausted_until_reset` window re-probes only from `next_probe_at`, so there the wait ends `now`
      (`None` would deny it until the window ends); elsewhere `next_probe_at = None`.
    """
    if outcome == "validated":
        w.probe_used, w.probe_failed, w.next_probe_at = False, False, None
    elif outcome == "failed":
        w.probe_failed, w.next_probe_at = True, now + PROBE_RETRY
    else:
        w.probe_used = False
        w.next_probe_at = now if w.state == "exhausted_until_reset" else None


# Persistence. Every statement names the columns of §7.8 explicitly; `_COLUMNS` is the dataclass order.
_COLUMNS: Final = tuple(f.name for f in fields(Window))
_SELECT_WINDOWS: Final = (
    "SELECT provider, window_start, window_end, is_current, closed_reason, limit_units, reserve_units, "
    "state, tokens, tokens_at, capacity, probe_used, probe_failed, next_probe_at, last_observed_at, "
    "observed_remaining, observed_limit, observed_this_hour, observed_hourly_cap, conservative_until, "
    "hour_observed_at FROM quota_windows"
)
_SELECT_CURRENT = q(f"{_SELECT_WINDOWS} WHERE provider = :provider AND is_current")
_LOCK_CURRENT = q(f"{_SELECT_CURRENT.text} FOR UPDATE")
_LOCK_BY_KEY = q(f"{_SELECT_WINDOWS} WHERE provider = :provider AND window_start = :window_start FOR UPDATE")
_INSERT_SQL: Final = (
    "INSERT INTO quota_windows (provider, window_start, window_end, is_current, closed_reason, limit_units, "
    "reserve_units, state, tokens, tokens_at, capacity, probe_used, probe_failed, next_probe_at, "
    "last_observed_at, observed_remaining, observed_limit, observed_this_hour, observed_hourly_cap, "
    "conservative_until, hour_observed_at) VALUES (:provider, :window_start, :window_end, :is_current, "
    ":closed_reason, :limit_units, :reserve_units, :state, :tokens, :tokens_at, :capacity, :probe_used, "
    ":probe_failed, :next_probe_at, :last_observed_at, :observed_remaining, :observed_limit, "
    ":observed_this_hour, :observed_hourly_cap, :conservative_until, :hour_observed_at)"
)
_INSERT = q(_INSERT_SQL)
# A concurrent bootstrap may already have inserted the provider's current row (`quota_windows_current`).
_INSERT_IF_ABSENT = q(f"{_INSERT_SQL} ON CONFLICT DO NOTHING")
_CLOSE = q(
    "UPDATE quota_windows SET is_current = false, closed_reason = :reason "
    "WHERE provider = :provider AND window_start = :window_start AND is_current"
)
_LOCK_ROW = q(
    "SELECT state, is_current FROM quota_windows WHERE provider = :provider AND window_start = :window_start "
    "FOR UPDATE"
)
_SAVE = q(
    "UPDATE quota_windows SET window_end = :window_end, limit_units = :limit_units, "
    "reserve_units = :reserve_units, state = :state, tokens = :tokens, tokens_at = :tokens_at, "
    "capacity = :capacity, probe_used = :probe_used, probe_failed = :probe_failed, "
    "next_probe_at = :next_probe_at, last_observed_at = :last_observed_at, "
    "observed_remaining = :observed_remaining, observed_limit = :observed_limit, "
    "observed_this_hour = :observed_this_hour, observed_hourly_cap = :observed_hourly_cap, "
    "conservative_until = :conservative_until, hour_observed_at = :hour_observed_at "
    "WHERE provider = :provider AND window_start = :window_start"
)


def _params(w: Window) -> dict[str, object]:
    return {name: getattr(w, name) for name in _COLUMNS}


async def _current(conn: AsyncConnection, provider: Provider, *, lock: bool) -> Window | None:
    row = (await conn.execute(_LOCK_CURRENT if lock else _SELECT_CURRENT, {"provider": provider})).first()
    if row is None:
        return None
    return Window(**{name: getattr(row, name) for name in _COLUMNS})


async def current_snapshot(conn: AsyncConnection, provider: Provider) -> Window | None:
    """The current window as it is (plain `SELECT`, no lock): planning and the dry-run simulate on it."""
    return await _current(conn, provider, lock=False)


async def window_locked(conn: AsyncConnection, provider: Provider, window_start: datetime) -> Window:
    """The window `(provider, window_start)`, current or closed, under `FOR UPDATE` (the ledger's recovery
    writes lock an entry's own window). Raises `LookupError` when it does not exist."""
    row = (await conn.execute(_LOCK_BY_KEY, {"provider": provider, "window_start": window_start})).first()
    if row is None:
        raise LookupError(f"no {provider} window starts at {window_start}")
    return Window(**{name: getattr(row, name) for name in _COLUMNS})


async def apply_changes(conn: AsyncConnection, changes: Sequence[WindowChange]) -> None:
    """Write `changes` in order: a `Close` turns its current row into history, an `Insert` adds one."""
    for change in changes:
        if isinstance(change, Close):
            closed = await conn.execute(
                _CLOSE,
                {
                    "provider": change.window.provider,
                    "window_start": change.window.window_start,
                    "reason": change.reason,
                },
            )
            if closed.rowcount != 1:
                raise LookupError(
                    f"no current {change.window.provider} window at {change.window.window_start} to close"
                )
        else:
            await conn.execute(_INSERT, _params(change.window))


async def current_window_locked(
    conn: AsyncConnection,
    provider: Provider,
    now: datetime,
    settings: QuotaSettings,
    *,
    interval_s: int,
    account: AccountObservation | None = None,
) -> Window | None:
    """The current window under `FOR UPDATE`, rolled forward to `now` with the bootstrap and rollover written.

    The authoritative path (§6.7), for the ledger's writes; planning reads `current_snapshot` instead. When
    the provider has no current row the bootstrap row is inserted, and that insert holds the new row's lock.

    Two READ COMMITTED races are absorbed here. A rival that rolled the window over while this statement
    waited for the row leaves it nothing: the re-checked row is no longer current and the rival's successor
    is not in its snapshot, so the row is read once more before anything is bootstrapped. A rival bootstrap
    that inserted first makes this one's insert do nothing, and its row is then read and used.
    """
    for _ in range(2):
        found = await _current(conn, provider, lock=True)
        if found is None:  # perhaps hidden by a rival rollover: a new statement sees its committed row
            found = await _current(conn, provider, lock=True)
        if found is None:
            found = bootstrap_window(provider, now, settings, interval_s=interval_s, account=account)
            if found is None:
                return None  # SerpApi before its first account read
            if (await conn.execute(_INSERT_IF_ABSENT, _params(found))).rowcount != 1:
                continue  # a rival bootstrap won: use its window
        w, changes = roll_forward(found, provider, now, settings, interval_s=interval_s, account=account)
        await apply_changes(conn, changes)
        return w
    raise RuntimeError(f"no current {provider} window after a lost bootstrap race")


async def save_window(conn: AsyncConnection, w: Window) -> None:
    """Rewrite the row of `w` (one `UPDATE` by its key); `Close` alone changes `is_current`/`closed_reason`.

    A `state` change must be an edge of `TRANSITIONS["quota_windows"]` (§7.16.8): it is checked here first
    (`ValueError`), and again by the `quota_windows_assert_transition` trigger. A closed window's state never
    changes. The row is locked (the caller normally holds the lock already).
    """
    row = (await conn.execute(_LOCK_ROW, {"provider": w.provider, "window_start": w.window_start})).first()
    if row is None:
        raise LookupError(f"no {w.provider} window starts at {w.window_start}")
    if row.state != w.state:
        if not row.is_current:
            raise ValueError(
                f"the {w.provider} window at {w.window_start} is closed: its state never changes"
            )
        if (row.state, w.state) not in TRANSITIONS["quota_windows"].allowed:
            raise ValueError(f"disallowed transition quota_windows: {row.state} -> {w.state}")
    await conn.execute(_SAVE, _params(w))
