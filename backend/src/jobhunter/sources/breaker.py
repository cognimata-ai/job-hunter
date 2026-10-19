"""The persisted circuit breaker of a source instance (spec §5.2, §17.6; plan Deviation 2).

`consecutive_failures` counts the failed attempts since the last success. `transient`, `rate_limited` and
`client` failures count toward the threshold (`PAID_THRESHOLD` for paid and quota sources,
`FREE_THRESHOLD` for free ones): below it the instance is `degraded`, at it `breaker_open`. `auth`,
`contract` and `not_found` open at once, each as its own state. Every open state records `breaker_opened_at`
and `half_open_after = now + HALF_OPEN_AFTER`; from then on `available` admits one probe. A failure while
open (a failed probe) keeps the open state and arms it for another `HALF_OPEN_AFTER`; a success returns to
`ok` and clears both timestamps. A `disabled` instance is left as it is by both: only `instances` enables it.

Each failure that leaves the instance open raises (or counts one more occurrence of) the alert of its state,
subject `instance:<id>`; a success resolves all four. Both functions lock the instance row and run in the
caller's transaction; `source_instances` is the last lock of the §3.3.3 order.
"""

from collections.abc import Mapping
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.observability.alerts import Severity, raise_alert, resolve_alert
from jobhunter.sources.base import Billing

if TYPE_CHECKING:
    from jobhunter.sources.instances import InstanceRow

Health = Literal["ok", "degraded", "breaker_open", "auth_failed", "contract_broken", "not_found", "disabled"]
FailureKind = Literal["transient", "rate_limited", "client", "auth", "contract", "not_found"]

PAID_THRESHOLD: Final = 2
FREE_THRESHOLD: Final = 5
HALF_OPEN_AFTER: Final = timedelta(hours=6)

# The failures that open the breaker at once, and the state each one opens.
_OPENS_AT_ONCE: Final[Mapping[str, Health]] = MappingProxyType(
    {"auth": "auth_failed", "contract": "contract_broken", "not_found": "not_found"}
)
# The four open states (each with `breaker_opened_at` and `half_open_after`) and their §17.6 alerts.
ALERTS: Final[Mapping[str, tuple[str, Severity]]] = MappingProxyType(
    {
        "breaker_open": ("source.breaker_open", "warn"),
        "auth_failed": ("source.auth_failed", "critical"),
        "contract_broken": ("source.contract_broken", "critical"),
        "not_found": ("source.not_found", "warn"),
    }
)

_LOCK = q("SELECT source_id, health, consecutive_failures FROM source_instances WHERE id = :id FOR UPDATE")
_FAILED = q(
    "UPDATE source_instances SET health = :health, consecutive_failures = :failures, "
    "last_error_code = :kind, breaker_opened_at = :opened_at, half_open_after = :half_open_after "
    "WHERE id = :id"
)
_SUCCEEDED = q(
    "UPDATE source_instances SET health = 'ok', consecutive_failures = 0, last_success_at = :now, "
    "last_error_code = NULL, breaker_opened_at = NULL, half_open_after = NULL WHERE id = :id"
)


def _subject(instance_id: int) -> str:
    return f"instance:{instance_id}"


async def _lock(conn: AsyncConnection, instance_id: int) -> tuple[str, Health, int]:
    row = (await conn.execute(_LOCK, {"id": instance_id})).first()
    if row is None:
        raise LookupError(f"source instance {instance_id} does not exist")
    return row.source_id, row.health, int(row.consecutive_failures)


def _next_health(current: Health, kind: FailureKind, failures: int, billing: Billing) -> Health:
    if (opened := _OPENS_AT_ONCE.get(kind)) is not None:
        return opened
    if current in ALERTS:
        return current  # a failed probe: still open
    threshold = FREE_THRESHOLD if billing == "free" else PAID_THRESHOLD
    return "breaker_open" if failures >= threshold else "degraded"


async def record_failure(
    conn: AsyncConnection, instance_id: int, kind: FailureKind, *, billing: Billing, now: datetime
) -> Health:
    """Count one failed attempt of the instance; returns its health afterwards."""
    source_id, current, failures = await _lock(conn, instance_id)
    if current == "disabled":
        return current
    failures += 1
    health = _next_health(current, kind, failures, billing)
    alert = ALERTS.get(health)
    opened = alert is not None
    await conn.execute(
        _FAILED,
        {
            "id": instance_id,
            "health": health,
            "failures": failures,
            "kind": kind,
            "opened_at": now if opened else None,
            "half_open_after": now + HALF_OPEN_AFTER if opened else None,
        },
    )
    if alert is not None:
        code, severity = alert
        await raise_alert(
            conn,
            code=code,
            severity=severity,
            subject=_subject(instance_id),
            message=f"source instance {instance_id} ({source_id}) is {health} after a {kind} failure",
            context={"instance_id": instance_id, "source_id": source_id, "consecutive_failures": failures},
        )
    return health


async def record_success(conn: AsyncConnection, instance_id: int, *, now: datetime) -> Health:
    """A successful attempt: `ok`, no failures, `last_success_at = now`; resolves the instance's alerts."""
    _, current, _ = await _lock(conn, instance_id)
    if current == "disabled":
        return current
    await conn.execute(_SUCCEEDED, {"id": instance_id, "now": now})
    for code, _severity in ALERTS.values():
        await resolve_alert(conn, code=code, subject=_subject(instance_id))
    return "ok"


Availability = Literal["available", "half_open", "blocked"]
_AVAILABILITY = q("SELECT health, half_open_after FROM source_instances WHERE id = :id")


def _availability(health: str, half_open_after: datetime | None, now: datetime) -> Availability:
    if health in ("ok", "degraded"):
        return "available"
    if health == "disabled" or half_open_after is None or now < half_open_after:
        return "blocked"
    return "half_open"


def available(instance: "InstanceRow", now: datetime) -> Availability:
    """`available` for `ok`/`degraded`; for an open state `half_open` (one probe) from `half_open_after`
    on and `blocked` before; `blocked` for `disabled`. The planner skips `blocked`."""
    return _availability(instance.health, instance.half_open_after, now)


async def instance_availability(conn: AsyncConnection, instance_id: int, *, now: datetime) -> Availability:
    """`available` for the instance's row as it is now, read without a lock (the acquisition's runtime
    gate, Ruling T14-b: it reads the row inside the transaction that reserves or marks an attempt, whose
    later instance writes keep `source_instances` last in the lock order)."""
    row = (await conn.execute(_AVAILABILITY, {"id": instance_id})).first()
    if row is None:
        raise LookupError(f"source instance {instance_id} does not exist")
    return _availability(row.health, row.half_open_after, now)
