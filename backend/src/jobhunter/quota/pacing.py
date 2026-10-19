"""Pacing (spec §6.4): the fractional token bucket of a provider window, and the admission decision (§6.7).

`N = limit - reserve` units are spread over the window: `fair_share = N * interval_s / window_length_s` per
scheduled run, the bucket holds at most `C = max(1, 2 * fair_share)` and accrues `N * Δt / window_length_s`.
Tokens and capacity are kept at the scale of their `numeric(10,4)` columns, so a window read back equals the
one saved: the capacity rounds to the nearest 4th decimal, the tokens round down (never a token too many).

Everything here is pure: `decide` runs on an in-memory window for the authoritative reservation (Task 8
`reserve`, under the window lock) and for planning and the dry-run (`project_allowance`, on a snapshot), and
it never touches the probe columns: only the reservation that admits a probe records it.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from math import ceil
from typing import Final, Literal

from jobhunter.quota.windows import RESERVE_RELEASE, Provider, Window
from jobhunter.run_settings import QuotaSettings

QuotaClass = Literal["normal", "retry", "probe"]
DenyCode = Literal["paced", "reserve_held", "exhausted", "window_cap", "awaiting_window_validation"]

_SCALE: Final = Decimal("0.0001")  # numeric(10,4)
_MICROSECOND: Final = timedelta(microseconds=1)


@dataclass(frozen=True)
class PlannedSpec:
    """A request the planner or the dry-run hands to `project_allowance` (Task 8)."""

    request_class: QuotaClass


@dataclass(frozen=True)
class Admit:
    request_class: QuotaClass
    tokens_used: int
    starts_probe: bool


@dataclass(frozen=True)
class Deny:
    code: DenyCode


def _length_us(w: Window) -> int:
    return (w.window_end - w.window_start) // _MICROSECOND


def _units(w: Window) -> int:
    return w.limit_units - w.reserve_units


def fair_share(w: Window, interval_s: int) -> float:
    """`N * interval_s / window_length_s`: the units one scheduled run may spend on average."""
    return _units(w) * interval_s * 1_000_000 / _length_us(w)


def capacity(w: Window, interval_s: int) -> Decimal:
    """`C = max(1, 2 * fair_share)`, exact, then rounded to the column scale."""
    twice = Decimal(2 * _units(w) * interval_s * 1_000_000) / Decimal(_length_us(w))
    return max(Decimal(1), twice).quantize(_SCALE, rounding=ROUND_HALF_UP)


def accrue(w: Window, now: datetime, *, interval_s: int) -> None:
    """§6.7 `accrue` in place: the capacity for `interval_s`, then `N * Δt / length` added up to it.

    `Δt` never runs backwards: a `now` before `tokens_at` (a transaction that started before the last save)
    adds nothing and leaves `tokens_at` where it is, so no stretch of time is credited twice.
    """
    w.capacity = capacity(w, interval_s)
    elapsed_us = max(0, (now - w.tokens_at) // _MICROSECOND)
    accrued = Decimal(_units(w) * elapsed_us) / Decimal(_length_us(w))
    w.tokens = min(w.capacity, (w.tokens + accrued).quantize(_SCALE, rounding=ROUND_FLOOR))
    w.tokens_at = max(w.tokens_at, now)


def effective_reserve(w: Window, now: datetime) -> int:
    """The reserve, or 0 in the final 48 h of the window (unused reserve is spent, not wasted)."""
    return 0 if (w.window_end - now) <= RESERVE_RELEASE else w.reserve_units


def per_run_cap(settings: QuotaSettings, fair_share: float) -> int:
    """`paid_max_requests_per_run`; `auto` is `max(10, ceil(2 * fair_share))`."""
    if settings.paid_max_requests_per_run == "auto":
        return max(10, ceil(2 * fair_share))
    return settings.paid_max_requests_per_run


def decide(
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
    per_run_cap: int,
) -> Admit | Deny:
    """The pure admission decision of §6.7, in its order. No I/O, no locks, and `w` is not modified.

    SerpApi first fails closed in the renewal gap (Ruling T7-a): from the local rollover until an account read
    reports a renewal date after `w.window_start`. Its windows are never probed (§6.3), and only such a read
    validates one (step 1; bootstrap validates only from a renewal that is the window's end, and no edge leads
    back to `extrapolated_unvalidated`), so the persisted state is the test: a SerpApi window in
    `extrapolated_unvalidated` has seen no read of its own renewal yet.
    """
    if provider == "serpapi" and w.state == "extrapolated_unvalidated":
        return Deny("window_cap")
    if w.state == "exhausted_until_reset" or (w.state == "extrapolated_unvalidated" and w.probe_failed):
        if w.next_probe_at is None or now < w.next_probe_at:
            return Deny("exhausted" if w.state == "exhausted_until_reset" else "awaiting_window_validation")
        request_class = "probe"  # periodic re-probe
    elif w.state == "extrapolated_unvalidated":
        if w.probe_used:
            return Deny("awaiting_window_validation")  # a probe is in flight
        request_class = "probe"
    if used + units > w.limit_units:
        return Deny("exhausted")
    if request_class == "normal" and used + units > w.limit_units - effective_reserve(w, now):
        return Deny("reserve_held")
    if not hour_ok or not adzuna_ok:
        return Deny("window_cap")  # §6.5; SerpApi fails closed without a fresh observation
    if request_class == "normal":
        if provider != "adzuna" and run_normal_count + units > per_run_cap:
            return Deny("paced")
        if w.tokens < units:
            return Deny("paced")
    return Admit(
        request_class=request_class,
        tokens_used=units if request_class == "normal" else 0,
        starts_probe=request_class == "probe",
    )
